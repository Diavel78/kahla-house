#!/usr/bin/env python3
"""Gemini Predictions probe + the empty-rung seeding experiment.

Run from the repo root with the daemon's interpreter:
    .venv/bin/python kahla-scanner/scripts/gemini_probe.py <mode> [flags]

Modes
  status          public health; with creds: terms, balances, positions, active orders,
                  the last 7 days of liquidity-reward + rebate payouts per event (answers
                  "what was the $5").
  rungs           the NFL rent list joined to today's books: every pool event, every rung,
                  flagged virgin (no bid, no ask) / no-bid / quoted. Read-only.
  seed            place post-only 10-lot bids on VIRGIN rungs of NOT-YET-STARTED NFL pool
                  events, verify each on the book. --dry by default; --place to spend.
  mine            list our resting orders (clientOrderId prefix kh-) with the live book.
  cancel-started  cancel our resting orders on games whose kickoff has passed.
  cancel-all      cancel every resting order we placed (kh- prefix only; hand orders untouched).

The seed price is the experiment: on a one-sided book the venue scores against OUR OWN
best price, so a lone bid should score at full weight at any price. --price sets it
(default 0.05 → 10 contracts = $0.50 of capital per seat). Read the per-event scores back
with `status` after 5:30pm ET.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except Exception:
    pass

import gemini_pm as g  # noqa: E402

LEDGER = Path(os.path.expanduser("~/.kahla/gemini_orders.json"))
CID_PREFIX = "kh-seed-"
ET = dt.timezone(dt.timedelta(hours=-4))  # EDT; fine for a probe, the lane will use zoneinfo


def _now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _iso(s):
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def _load_ledger() -> list[dict]:
    if LEDGER.exists():
        try:
            return json.loads(LEDGER.read_text())
        except Exception:
            return []
    return []


def _save_ledger(rows: list[dict]) -> None:
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    LEDGER.write_text(json.dumps(rows, indent=1))


# --------------------------------------------------------------------------- rent list
def nfl_pool_rungs() -> list[dict]:
    """Join liquidity pools → sports events → contracts. One row per rung."""
    pools = {p["event_ticker"]: p for p in g.liquidity_events()}
    evs = g.events_all("sports", "active")
    rows = []
    for e in evs:
        t = e.get("ticker") or ""
        if t not in pools or not t.startswith("NFL-"):
            continue
        parts = g.nfl_game_ticker_parts(t)
        if not parts:
            continue  # futures (NFLMVP2026 etc.) — pooled, but no kickoff to manage
        start = _iso(e.get("startTime") or "")
        for c in e.get("contracts") or []:
            pr = c.get("prices") or {}
            bb, ba = g._f(pr.get("bestBid")), g._f(pr.get("bestAsk"))
            rows.append({
                "event": t, "title": e.get("title"), "kind": parts["kind"],
                "start": start, "is_live": e.get("isLive"),
                "symbol": c.get("instrumentSymbol"), "label": c.get("label"),
                "strike": (c.get("strike") or {}).get("value"),
                "bid": bb, "ask": ba,
                "state": ("virgin" if bb is None and ba is None else
                          "no_bid" if bb is None else "no_ask" if ba is None else "quoted"),
                "pool_usd": float(pools[t].get("daily_pool_usd") or 0),
                "makers": pools[t].get("qualifying_maker_count"),
                "pool_ends": pools[t].get("ends_at"),
                "contract_status": c.get("status"),
            })
    return rows


def cmd_rungs(a) -> None:
    rows = nfl_pool_rungs()
    now = _now_utc()
    by_ev: dict[str, list[dict]] = {}
    for r in rows:
        by_ev.setdefault(r["event"], []).append(r)
    print(f"{len(by_ev)} NFL game pool events · {len(rows)} rungs · {now:%Y-%m-%d %H:%M}Z\n")
    tot = {"virgin": 0, "no_bid": 0, "no_ask": 0, "quoted": 0}
    for ev, rs in sorted(by_ev.items(), key=lambda kv: (kv[1][0]["start"] or now, kv[0])):
        r0 = rs[0]
        c = {k: sum(1 for r in rs if r["state"] == k) for k in tot}
        for k in tot:
            tot[k] += c[k]
        started = "LIVE/DONE" if (r0["start"] and r0["start"] <= now) else f"in {(r0['start']-now).total_seconds()/3600:.1f}h" if r0["start"] else "?"
        print(f"{ev:<40} ${r0['pool_usd']:>4.0f}/day makers={r0['makers']:<3} {started:<10} "
              f"rungs={len(rs):<3} virgin={c['virgin']:<3} no_bid={c['no_bid']:<3} quoted={c['quoted']}")
        if a.verbose:
            for r in rs:
                print(f"     {r['state']:<7} {r['symbol']:<48} {str(r['label'])[:42]:<42} bid={r['bid']} ask={r['ask']}")
    print("\nTOTAL", tot)


# --------------------------------------------------------------------------- seeding
def _pick_seats(rows: list[dict], a) -> list[dict]:
    now = _now_utc()
    horizon = now + dt.timedelta(hours=a.horizon_h)
    seats = []
    for r in rows:
        if r["state"] != "virgin":
            continue
        if not r["start"] or r["start"] <= now + dt.timedelta(minutes=a.min_lead_min):
            continue
        if r["start"] > horizon:
            continue
        if a.kinds and r["kind"] not in a.kinds:
            continue
        if r.get("contract_status") not in (None, "active"):
            continue
        seats.append(r)
    # spread the budget across events round-robin so one prop family can't eat it
    by_ev: dict[str, list[dict]] = {}
    for r in seats:
        by_ev.setdefault(r["event"], []).append(r)
    out, i = [], 0
    while len(out) < a.max_seats and any(by_ev.values()):
        for ev in list(by_ev):
            if by_ev[ev]:
                out.append(by_ev[ev].pop(0))
                if len(out) >= a.max_seats:
                    break
        i += 1
    return out


def cmd_seed(a) -> None:
    rows = nfl_pool_rungs()
    seats = _pick_seats(rows, a)
    cost_each = a.size * a.price
    budget_seats = int(a.budget // cost_each) if cost_each > 0 else 0
    seats = seats[:budget_seats]
    print(f"virgin seats chosen: {len(seats)} × {a.size} @ {a.price:.2f} = ${len(seats)*cost_each:.2f} "
          f"(budget ${a.budget:.2f}, cap {a.max_seats}) — {'PLACING' if a.place else 'DRY RUN'}\n")
    for s in seats:
        print(f"  {s['event']:<36} {str(s['label'])[:44]:<44} kicks {s['start']:%a %H:%M}Z pool ${s['pool_usd']:.0f} makers={s['makers']}")
    if not a.place or not seats:
        return
    if not g.has_creds():
        print("\nno GEMINI_API_KEY/SECRET in env — nothing placed"); return
    st = g.terms_status()
    print("\nterms:", st)
    ledger = _load_ledger()
    placed = 0
    for s in seats:
        cid = f"{CID_PREFIX}{int(time.time())}-{s['symbol'][-12:]}"
        try:
            resp = g.place_order(s["symbol"], "buy", "yes", a.size, a.price, post_only=True,
                                 client_order_id=cid)
        except g.GeminiError as ex:
            print("  REJECTED", s["symbol"], ex); continue
        oid = resp.get("orderId") if isinstance(resp, dict) else None
        status = resp.get("status") if isinstance(resp, dict) else None
        # verify on the venue's own book — a create that returns an id is a claim, not a fact
        time.sleep(0.4)
        try:
            bk = g.book(s["symbol"])
            seen = any(abs(float(b["price"]) - a.price) < 1e-9 for b in bk.get("bids", []))
        except Exception:
            seen = None
        row = {"symbol": s["symbol"], "event": s["event"], "label": s["label"], "order_id": oid,
               "client_order_id": cid, "status": status, "price": a.price, "size": a.size,
               "start": s["start"].isoformat() if s["start"] else None,
               "placed_at": _now_utc().isoformat(), "book_seen": seen, "raw": resp}
        ledger.append(row); _save_ledger(ledger); placed += 1
        print(f"  placed {oid} {status} {s['symbol']} book_seen={seen}")
        time.sleep(0.25)
    print(f"\n{placed} placed; ledger {LEDGER}")


# --------------------------------------------------------------------------- ours
def _our_active() -> list[dict]:
    out = []
    off = 0
    while True:
        page = g.active_orders(limit=100, offset=off)
        out.extend(o for o in page if str(o.get("clientOrderId") or "").startswith(CID_PREFIX))
        if len(page) < 100:
            return out
        off += 100


def cmd_mine(a) -> None:
    if not g.has_creds():
        print("no creds"); return
    ours = _our_active()
    print(f"{len(ours)} resting kh- orders")
    for o in ours:
        sym = o.get("symbol")
        try:
            bk = g.book(sym); bb = bk["bids"][0]["price"] if bk.get("bids") else None; ba = bk["asks"][0]["price"] if bk.get("asks") else None
        except Exception:
            bb = ba = "?"
        print(f"  {o.get('orderId')} {sym:<48} {o.get('side')} {o.get('outcome')} {o.get('quantity')}@{o.get('price')} "
              f"filled={o.get('filledQuantity')} book {bb}/{ba}")


def cmd_cancel(a, started_only: bool) -> None:
    if not g.has_creds():
        print("no creds"); return
    ours = _our_active()
    now = _now_utc()
    starts = {r["symbol"]: _iso(r["start"]) for r in _load_ledger() if r.get("start")}
    todo = []
    for o in ours:
        if started_only:
            st = starts.get(o.get("symbol"))
            if not st or st > now:
                continue
        todo.append(o)
    print(f"cancelling {len(todo)} of {len(ours)} kh- orders ({'started games' if started_only else 'ALL'})")
    for o in todo:
        try:
            r = g.cancel_order(o["orderId"]); print("  cancelled", o["orderId"], o.get("symbol"), (r or {}).get("status") if isinstance(r, dict) else r)
        except g.GeminiError as ex:
            print("  FAILED", o["orderId"], ex)
        time.sleep(0.2)


# --------------------------------------------------------------------------- status
def cmd_status(a) -> None:
    cfg = g.liquidity_config()
    pools = g.liquidity_events()
    nfl = [p for p in pools if p.get("category") == "Pro Football"]
    print("public  ok · pools", len(pools), "· NFL pools", len(nfl), "· config", cfg,
          "· sports rebate mult", g.current_rebate_mult("Sports"))
    if not g.has_creds():
        print("\ncreds   MISSING — add GEMINI_API_KEY / GEMINI_API_SECRET to .env (account-scoped key, "
              "Trading permission, time-based nonce ON)")
        return
    print("\nterms  ", g.terms_status())
    try:
        bal = g.balances()
        print("balance", [b for b in (bal or []) if str(b.get("currency", "")).upper() == "USD"] or bal)
    except g.GeminiError as ex:
        print("balance", ex)
    try:
        pos = g.positions()
        print(f"positions {len(pos)}")
        for p in pos[:25]:
            print("  ", json.dumps(p)[:220])
    except g.GeminiError as ex:
        print("positions", ex)
    try:
        ords = g.active_orders()
        print(f"active orders {len(ords)}")
        for o in ords[:25]:
            print("  ", json.dumps(o)[:220])
    except g.GeminiError as ex:
        print("active orders", ex)
    today = dt.datetime.now(ET).date()
    d0 = (today - dt.timedelta(days=a.days)).isoformat(); d1 = today.isoformat()
    print(f"\nLIQUIDITY REWARDS {d0}..{d1}")
    try:
        print(json.dumps(g.liquidity_daily(d0, d1), indent=1)[:6000])
    except g.GeminiError as ex:
        print(" ", ex)
    print("\nLIQUIDITY lifetime")
    try:
        print(" ", g.liquidity_total())
    except g.GeminiError as ex:
        print(" ", ex)
    print("\nMAKER REBATE payouts")
    try:
        print(json.dumps(g.rebate_payouts(limit=20), indent=1)[:3000])
        print(" total", g.rebate_total())
    except g.GeminiError as ex:
        print(" ", ex)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", choices=["status", "rungs", "seed", "mine", "cancel-started", "cancel-all"])
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--days", type=int, default=7, help="status: reward lookback days")
    ap.add_argument("--place", action="store_true", help="seed: actually place (default dry)")
    ap.add_argument("--price", type=float, default=0.05)
    ap.add_argument("--size", type=float, default=10)
    ap.add_argument("--budget", type=float, default=20.0, help="seed: max $ of bids this run")
    ap.add_argument("--max-seats", type=int, default=40)
    ap.add_argument("--horizon-h", type=float, default=48)
    ap.add_argument("--min-lead-min", type=float, default=20, help="skip games kicking off sooner than this")
    ap.add_argument("--kinds", nargs="*", default=None, help="restrict to kinds, e.g. S T TT PPRECY")
    a = ap.parse_args()
    {"status": cmd_status, "rungs": cmd_rungs, "seed": cmd_seed, "mine": cmd_mine,
     "cancel-started": lambda x: cmd_cancel(x, True), "cancel-all": lambda x: cmd_cancel(x, False)}[a.mode](a)


if __name__ == "__main__":
    main()
