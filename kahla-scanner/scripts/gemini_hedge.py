#!/usr/bin/env python3
"""gemini_hedge — the mirror leg. One Polymarket seat, one Gemini twin, kept in sync.

Rob, Sep 14 2026: "a hedge machine that collects rent on both sides… the only way it works is
if you do the repeg on Gemini as well… repeg might have to adjust contract sizes."

Per configured pair (~/.kahla/gemini_hedges.json):
  * READ Polymarket (SDK, 2 light reads per loop): our resting order on the slug (leaves qty,
    price, intent) and our net position. Never writes to Polymarket. The Ferrari owns that leg.
  * READ Gemini: the twin's book, our resting order, our position.
  * COMPUTE (hedge_calc): hedge outcome = opposite of what the Poly leg is/gets long of;
      rest_qty   = Poly leaves (still-resting)  → ONE post-only Gemini bid at Gemini's touch (join)
      urgent_qty = Poly filled − Gemini held    → exposure exists NOW: a POST-ONLY bid leading the
                                                 bid side by a tick, capped at the pair cap. WE NEVER
                                                 CROSS, WE DON'T TAKE (Rob, Sep 14).
  * REPEG: if the resting bid's price ≠ current touch or qty ≠ rest_qty → cancel, verify off
    the book, re-place. One order per symbol per role. Never lead the touch, never cross on
    the rent leg (makerOrCancel).
  * KICKOFF: unfilled Gemini bids cancel at kickoff (same rule as gemini-cancel); filled
    hedge positions ride with the Poly leg.
  * DRY by default: logs every would-do. `--live` places. Budget cap per pair in the config.

FERRARI RULE: separate process, read-only on Polymarket (≤4 REST reads/min across all pairs
at a 30s loop), its own ledger + log. Kill it and nothing else changes.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv  # noqa: E402
load_dotenv(ROOT / ".env")
import gemini_pm as g  # noqa: E402
import hedge_calc as hc  # noqa: E402

log = logging.getLogger("gemini_hedge")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
CFG = Path(os.path.expanduser("~/.kahla/gemini_hedges.json"))
LEDGER = Path(os.path.expanduser("~/.kahla/gemini_hedge_ledger.jsonl"))
STATE = Path(os.path.expanduser("~/.kahla/gemini_hedge_state.json"))   # per pair: last resting Poly price (= fill price for a maker)


def _state_load() -> dict:
    try:
        return json.loads(STATE.read_text()) if STATE.exists() else {}
    except Exception:
        return {}


def _state_save(d: dict) -> None:
    STATE.write_text(json.dumps(d, indent=1, default=str))


def held_cost(pair: dict, ps: dict, state: dict) -> float | None:
    """The Poly leg's cost, in OUR side's terms. ⚠ NEVER the venue's avgPx alone — it is a lifetime
    per-market BLEND (CLAUDE.md, the Braves 74¢ lesson; here GB–NYJ read 0.917 on a 55.5¢ fill and
    the hedge got capped at 9¢). Order of truth: (1) the price we were RESTING at when the leg
    flipped to filled (a maker fills at its own price) — tracked in STATE; (2) config `poly_cost`;
    (3) venue avgPx ONLY if it agrees with (1)/(2) within 3¢. Nothing trustworthy → None → the
    urgent leg refuses to place (no guessing a cap)."""
    st = state.get(pair["poly_slug"], {})
    ref = st.get("last_rest_px_own") or pair.get("poly_cost")
    venue = ps.get("held_avg_own")
    if ref is not None:
        if venue is not None and abs(float(venue) - float(ref)) > 0.03:
            log.warning("%s: venue avgPx %.3f disagrees with our fill price %.3f — using ours", pair["poly_slug"][-28:], venue, float(ref))
        return round(float(ref), 4)
    return None
CID = "kh-hedge-"


def _poly_client():
    from polymarket_us import PolymarketUS
    return PolymarketUS(key_id=os.getenv("POLYMARKET_KEY_ID"), secret_key=os.getenv("POLYMARKET_SECRET_KEY"))


def _iso(s):
    try:
        return dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception:
        return None


def _led(**kw):
    kw["at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    with LEDGER.open("a") as f:
        f.write(json.dumps(kw, default=str) + "\n")


# ------------------------------------------------------------------ Polymarket read
# ⚠ THE SEP 14 2026 BUG (cost a doubled position): the side was read off the resting ORDER's
# intent with "not BUY_SHORT ⇒ yes". The order was SELL_SHORT — the scalp arm's ASK on a NO
# position — so a long-UNDER leg was read as long-OVER and the "hedge" bought 19 more UNDER.
# Rules now: (1) a resting BID is ONLY intent BUY_LONG (yes) / BUY_SHORT (no); SELL_* are exits
# and are ignored; (2) the held side comes from netPosition's SIGN, never from an order;
# (3) avgPx is in OUR side's terms for longs and shorts alike (verified against pick rows); (4) if a resting bid's side disagrees
# with the held side the pair is SKIPPED with an error — never guessed.
def parse_poly(orders: list, position: dict | None, slug: str) -> dict:
    out = {"resting_qty": 0.0, "resting_px_own": None, "resting_side": None, "held_side": None,
           "filled_qty": 0.0, "held_avg_own": None, "side": None, "error": None}
    for o in orders or []:
        if o.get("marketSlug") != slug:
            continue
        if o.get("state") not in ("ORDER_STATE_NEW", "ORDER_STATE_PARTIALLY_FILLED", "ORDER_STATE_REPLACED"):
            continue
        intent = o.get("intent")
        if intent not in ("ORDER_INTENT_BUY_LONG", "ORDER_INTENT_BUY_SHORT"):
            continue                      # SELL_LONG / SELL_SHORT = asks (exits), not seats
        yes_px = float(o["price"]["value"])
        side = "yes" if intent == "ORDER_INTENT_BUY_LONG" else "no"
        if out["resting_side"] not in (None, side):
            out["error"] = "bids on both sides"; return out
        out["resting_side"] = side
        out["resting_px_own"] = yes_px if side == "yes" else round(1 - yes_px, 4)
        out["resting_qty"] += float(o.get("leavesQuantity") or 0)
    if position:
        net = float(position.get("netPosition") or 0)
        if abs(net) >= 1:
            out["filled_qty"] = abs(net)
            out["held_side"] = "yes" if net > 0 else "no"
            try:   # avgPx is ALREADY in our side's terms for longs AND shorts (CIN-HOU: short, avgPx 0.60, pick paid 59.7¢)
                avg = float((position.get("avgPx") or {}).get("value"))
                out["held_avg_own"] = round(avg, 4)
            except (TypeError, ValueError):
                out["held_avg_own"] = None
    if out["held_side"] and out["resting_side"] and out["held_side"] != out["resting_side"]:
        out["error"] = f"held {out['held_side']} but bidding {out['resting_side']} — refusing to mirror"
    out["side"] = out["held_side"] or out["resting_side"]
    return out


def poly_state(pc, slug: str) -> dict:
    r = pc.orders.list(); orders = r.get("orders") if isinstance(r, dict) else r
    r = pc.portfolio.positions(); pos = r.get("positions") if isinstance(r, dict) and "positions" in r else r
    p = pos.get(slug) if isinstance(pos, dict) else None
    if isinstance(pos, list):
        p = next((x for x in pos if (x.get("marketMetadata") or {}).get("slug") == slug), None)
    return parse_poly(orders or [], p, slug)


# ------------------------------------------------------------------ Gemini socket (Rob, Sep 15: "thought we used websocket")
# One authenticated connection: {sym}@bookTicker for every configured pair + orders@account +
# positions@account. The loop WAKES on frames (1s debounce) and REST is only the 5-minute reconcile
# or the fallback when the socket is down/stale. Frame shapes verified live Sep 13-14:
#   bookTicker  {"s":sym(lower),"b":bid,"B":qty,"a":ask,"A":qty}
#   orderUpdate {"e":"orderUpdate","s":SYM,"i":id,"c":clientId,"S":"BUY|SELL","X":status,"p":px,"q":qty,"z":remaining,"O":"YES|NO"}
#   orderSnapshot {"e":"orderSnapshot","orders":[...]}      positionReport {"e":"positionReport","P":[{"s":SYM,"a":[{"t":"position","v":qty}]}]}
import threading
import ssl
import websocket  # noqa: E402

class GemSocket:
    STALE_S = 120

    def __init__(self):
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.books: dict[str, tuple] = {}          # SYM → (bid, ask)
        self.orders: dict[str, dict] = {}          # orderId → {symbol, clientOrderId, side, outcome, price, remainingQuantity, status}
        self.pos: dict[str, float] = {}            # SYM → signed qty (+YES / −NO), from positionReport
        self.snap_ok = False                       # orderSnapshot received this connection
        self.last_rx = 0.0
        self.connected = False
        self.symbols: set[str] = set()
        self.ws = None
        threading.Thread(target=self._run, daemon=True).start()

    # ---- public reads
    def fresh(self) -> bool:
        return self.connected and self.snap_ok and (time.monotonic() - self.last_rx) < self.STALE_S

    def book(self, sym: str):
        with self.lock:
            return self.books.get(sym)

    def our_orders(self, sym: str) -> list[dict]:
        with self.lock:
            return [dict(o) for o in self.orders.values()
                    if o.get("symbol") == sym and str(o.get("clientOrderId") or "").startswith(CID)
                    and o.get("status") in ("open", "NEW", "OPEN", "PARTIALLY_FILLED", "partially_filled")]

    def held(self, sym: str, outcome: str) -> float:
        with self.lock:
            v = self.pos.get(sym, 0.0)
        return max(0.0, v) if outcome == "yes" else max(0.0, -v)

    def want(self, symbols: set[str]):
        new = symbols - self.symbols
        self.symbols |= symbols
        if new and self.connected and self.ws:
            self._sub(new)

    # ---- internals
    def _sub(self, syms):
        try:
            self.ws.send(json.dumps({"id": f"bt{int(time.time())}", "method": "subscribe",
                                     "params": [f"{s.lower()}@bookTicker" for s in syms]}))
        except Exception as ex:
            log.warning("ws subscribe failed: %s", ex)

    def _on_open(self, ws):
        self.connected = True; self.snap_ok = False
        ws.send(json.dumps({"id": "acct", "method": "subscribe", "params": ["orders@account", "positions@account"]}))
        if self.symbols:
            self._sub(self.symbols)
        log.info("gemini socket open (%d symbols)", len(self.symbols))

    def _norm_order(self, o: dict) -> dict | None:
        """Both REST-shaped (orderId/clientOrderId/…) and stream-shaped (i/c/…) orders."""
        oid = o.get("orderId") or o.get("i")
        if oid is None:
            return None
        st = o.get("status") or o.get("X") or ""
        return {"orderId": oid, "clientOrderId": o.get("clientOrderId") or o.get("c") or "",
                "symbol": (o.get("symbol") or o.get("s") or "").upper(),
                "side": (o.get("side") or o.get("S") or "").lower(), "outcome": (o.get("outcome") or o.get("O") or "").lower(),
                "price": o.get("price") or o.get("p"), "quantity": o.get("quantity") or o.get("q"),
                "remainingQuantity": o.get("remainingQuantity") if o.get("remainingQuantity") is not None else o.get("z"),
                "status": st}

    def _on_ping(self, ws, m):
        self.last_rx = time.monotonic()

    def why_not_fresh(self) -> str:
        return (f"connected={self.connected} snap_ok={self.snap_ok} "
                f"age={time.monotonic() - self.last_rx:.0f}s books={sorted(self.books)} subs={sorted(self.symbols)}")

    def _on_msg(self, ws, m):
        self.last_rx = time.monotonic()
        try:
            d = json.loads(m)
        except Exception:
            return
        e = d.get("e")
        woke = False
        with self.lock:
            if e is None and "s" in d and "b" in d and "a" in d:
                sym = str(d["s"]).upper()
                if sym in self.symbols:
                    self.books[sym] = (float(d["b"]) if d.get("b") else None, float(d["a"]) if d.get("a") else None); woke = True
            elif e == "orderSnapshot":
                self.orders = {}
                for o in d.get("orders") or []:
                    n = self._norm_order(o)
                    if n: self.orders[n["orderId"]] = n
                self.snap_ok = True; woke = True
            elif e == "orderUpdate":
                n = self._norm_order(d)
                if n:
                    if n["status"] in ("FILLED", "CANCELED", "CANCELLED", "REJECTED", "EXPIRED", "filled", "cancelled", "canceled"):
                        self.orders.pop(n["orderId"], None)
                    else:
                        self.orders[n["orderId"]] = n
                    woke = True
            elif e == "positionReport":
                for row in d.get("P") or []:
                    sym = str(row.get("s") or "").upper()
                    for a in row.get("a") or []:
                        if a.get("t") == "position":
                            try: self.pos[sym] = float(a.get("v") or 0)
                            except (TypeError, ValueError): pass
                woke = True
            elif d.get("id") and d.get("status") not in (None, 200):
                log.warning("ws error: %s", m[:160])
        if woke:
            self.wake.set()

    def _run(self):
        backoff = 2
        while True:
            try:
                self.ws = websocket.WebSocketApp("wss://ws.gemini.com", header=g.ws_auth_headers(), on_open=self._on_open,
                                                 on_message=self._on_msg, on_error=lambda w, e: log.warning("ws error cb: %s", e),
                                                 on_close=lambda w, a, b: log.info("gemini socket closed %s %s", a, b),
                                                 on_ping=self._on_ping)      # the venue pings every ~10s — that IS liveness
                self.ws.run_forever(sslopt={"cert_reqs": ssl.CERT_NONE})
            except Exception as ex:
                log.warning("ws run: %s", ex)
            self.connected = False
            time.sleep(backoff); backoff = min(backoff * 2, 60)


SOCK: GemSocket | None = None


# ------------------------------------------------------------------ Gemini read/write
_LAST_REST_RECON: dict[str, float] = {}
RECON_S = 300


def gem_state(symbol: str, outcome: str, force_rest: bool = False) -> dict:
    """Socket-fed state; REST only when the socket is down/stale, when a reconcile is due (every
    RECON_S), or when `force_rest` (after our own writes, before anything that deletes/decides)."""
    use_rest = force_rest or SOCK is None or not SOCK.fresh() or (time.monotonic() - _LAST_REST_RECON.get(symbol, 0.0)) > RECON_S
    if use_rest and SOCK is not None and not force_rest and (time.monotonic() - _LAST_REST_RECON.get(symbol, 0.0)) <= RECON_S:
        log.info("socket not trusted → REST: %s", SOCK.why_not_fresh())
    if not use_rest:
        b = SOCK.book(symbol)
        if b is not None:
            held = SOCK.held(symbol, outcome)
            st = {"yes_bid": b[0], "yes_ask": b[1], "orders": SOCK.our_orders(symbol), "held": held,
                  "held_avg": _HELD_AVG_CACHE.get((symbol, outcome)) if held else None, "src": "ws"}
            if held and st["held_avg"] is None:
                use_rest = True                       # need the venue's avg once per new position
            else:
                return st
    bk = g.book(symbol)
    yb = float(bk["bids"][0]["price"]) if bk.get("bids") else None
    ya = float(bk["asks"][0]["price"]) if bk.get("asks") else None
    ours = [o for o in g.active_orders(symbol=symbol) if str(o.get("clientOrderId") or "").startswith(CID)]
    held, held_cost = 0.0, 0.0
    for p in g.positions(limit=100):
        if p.get("symbol") == symbol and (p.get("outcome") or "").lower() == outcome:
            q = float(p.get("totalQuantity") or 0); held += q; held_cost += q * float(p.get("avgPrice") or 0)
    avg = round(held_cost / held, 4) if held else None
    if avg is not None:
        _HELD_AVG_CACHE[(symbol, outcome)] = avg
    _LAST_REST_RECON[symbol] = time.monotonic()
    if SOCK is not None:                               # re-seed the socket's view from truth
        with SOCK.lock:
            SOCK.books[symbol] = (yb, ya)
            for oid in [k for k, v in SOCK.orders.items() if v.get("symbol") == symbol]:
                SOCK.orders.pop(oid, None)               # REST is the truth for this symbol's orders
            for o in ours:
                n = SOCK._norm_order(o)
                if n: SOCK.orders[n["orderId"]] = n
            SOCK.pos[symbol] = held if outcome == "yes" else -held
            SOCK.snap_ok = True                          # the venue does not always send orderSnapshot; REST seeded it
    return {"yes_bid": yb, "yes_ask": ya, "orders": ours, "held": held, "held_avg": avg, "src": "rest"}


_HELD_AVG_CACHE: dict[tuple, float] = {}


def gem_sync(pair: dict, want_rest_qty: float, rest_px, urgent_qty: float, cap_px, live: bool,
             flat_qty: float = 0.0, flat_px=None) -> None:
    sym, out = pair["gemini_symbol"], pair["hedge_outcome"]
    st = pair["_gem"]
    rest_orders = [o for o in st["orders"] if str(o.get("clientOrderId")).startswith(CID + "rest")]
    urg_orders = [o for o in st["orders"] if str(o.get("clientOrderId")).startswith(CID + "urg")]
    flat_orders = [o for o in st["orders"] if str(o.get("clientOrderId")).startswith(CID + "flat")]
    budget = float(pair.get("budget_usd", 15.0))

    def _place(role, qty, px, post_only, side="buy"):
        cost = qty * px if side == "buy" else 0.0
        if cost > budget:
            log.warning("%s %s: $%.2f exceeds pair budget $%.2f — capping qty", sym, role, cost, budget)
            qty = int(budget / px)
        if qty < 1:
            return
        cid = f"{CID}{role}-{int(time.time())}"
        if not live:
            log.info("DRY %s %s: would place %s %s %g @ %.2f (post_only=%s)", sym, role, side.upper(), out.upper(), qty, px, post_only)
            return
        r = g.place_order(sym, side, out, qty, px, post_only=post_only, client_order_id=cid)
        _LAST_REST_RECON[sym] = 0.0                    # verify our own write on REST next loop
        log.info("%s %s: placed %s %s %g @ %.2f → %s", sym, role, side.upper(), out.upper(), qty, px, r.get("status"))
        _led(kind="place", role=role, side=side, symbol=sym, outcome=out, qty=qty, px=px, order_id=r.get("orderId"), status=r.get("status"), pair=pair["poly_slug"])

    def _cancel(o, why):
        if not live:
            log.info("DRY %s: would cancel %s (%s @ %s) — %s", sym, o.get("orderId"), o.get("quantity"), o.get("price"), why); return
        g.cancel_order(o["orderId"])
        _LAST_REST_RECON[sym] = 0.0
        time.sleep(0.6)
        still = [x for x in g.active_orders(symbol=sym) if x.get("orderId") == o["orderId"]]
        log.info("%s: cancelled %s — %s (verified gone=%s)", sym, o.get("orderId"), why, not still)
        _led(kind="cancel", symbol=sym, order_id=o.get("orderId"), why=why, verified=not still)

    # --- rent leg: exactly one resting post-only bid for want_rest_qty at rest_px
    keep = None
    for o in rest_orders:
        if keep is None and rest_px is not None and abs(float(o["price"]) - rest_px) < 1e-9 and abs(float(o["remainingQuantity"]) - want_rest_qty) < 0.5:
            keep = o
        else:
            _cancel(o, "repeg" if rest_px is not None else "no book")
    if keep is None and want_rest_qty >= 1 and rest_px is not None:
        _place("rest", int(want_rest_qty), rest_px, True)
    # --- urgent leg: exposure exists now → one limit at the cap (crosses if it can)
    for o in urg_orders:
        if urgent_qty < 1 or abs(float(o["price"]) - cap_px) > 1e-9:
            _cancel(o, "urgent leg re-price" if urgent_qty >= 1 else "exposure closed")
            urg_orders = []
    if urgent_qty >= 1 and not urg_orders and cap_px is not None and cap_px > 0:
        _place("urg", int(urgent_qty), cap_px, True)          # maker only — never cross
    # --- ask leg: one post-only SELL for everything the Lambo holds (see run() for the price rule).
    # Never touched by the kickoff logic; re-priced / re-sized whenever the rule's answer changes.
    for o in flat_orders:
        if flat_qty < 1 or flat_px is None or abs(float(o["price"]) - flat_px) > 1e-9 or abs(float(o["remainingQuantity"]) - flat_qty) > 0.5:
            _cancel(o, "flat leg re-size" if flat_qty >= 1 else "nothing left to flatten")
            flat_orders = []
    if flat_qty >= 1 and flat_px is not None and not flat_orders:
        _place("flat", int(flat_qty), flat_px, True, side="sell")   # maker only — never cross


PSQL = "/Applications/Postgres.app/Contents/Versions/latest/bin/psql"


def _write_hedge_pair(poly_slug: str, gemini_symbol: str, paired_qty: float, poly_filled: float,
                      gemini_held: float, kickoff) -> None:
    """Upsert when the pair is on (paired ≥ 1) OR Gemini holds a leg (gemini_held ≥ 1); delete
    otherwise. Two Ferrari readers: `_hedged_ask_off` (paired ≥ 1 only — T-60 ask rule) and
    `_hedge_leg_slugs` (ANY leg — the recenter must not rung-jump a Poly seat whose twin is
    resting/held here, Sep 15 2026: the twin is keyed by slug and cannot follow)."""
    import subprocess
    if paired_qty >= 1 or gemini_held >= 1:
        sql = ("insert into hedge_pairs (poly_slug,gemini_symbol,paired_qty,poly_filled,gemini_held,kickoff,updated_at) "
               f"values ('{poly_slug}','{gemini_symbol}',{paired_qty},{poly_filled},{gemini_held},"
               + (f"'{kickoff}'" if kickoff else "null") + ",now()) "
               "on conflict (poly_slug) do update set paired_qty=excluded.paired_qty, poly_filled=excluded.poly_filled, "
               "gemini_held=excluded.gemini_held, gemini_symbol=excluded.gemini_symbol, kickoff=excluded.kickoff, updated_at=now();")
    else:
        sql = f"delete from hedge_pairs where poly_slug='{poly_slug}';"
    out = subprocess.run([PSQL, "kahla", "-v", "ON_ERROR_STOP=1", "-At", "-c", sql], capture_output=True, text=True, timeout=20)
    if out.returncode:
        raise RuntimeError(out.stderr[:200])


def ask_px(cost: float, h_bid, h_ask, at_cost: bool = False):
    """The Lambo's ask: cost + 1 tick when that alone leads the ask side, else cost (exactly
    cost when `at_cost`); one tick over a bid sitting at/above cost; never through the ask."""
    px = round(cost, 2) if at_cost else (round(cost + 0.01, 2) if (h_ask is None or cost + 0.01 < h_ask - 1e-9) else round(cost, 2))
    if h_bid is not None and px <= h_bid + 1e-9:
        px = round(h_bid + 0.01, 2)
    if h_ask is not None and px > h_ask + 1e-9:
        px = round(h_ask, 2)
    return px


def orphan_plan(gs: dict, hedge_outcome: str, t30: bool):
    """THE POLY LEG IS GONE (no bid, no position — a rung jump, a cancel, a sale) but Gemini
    still has orders/inventory on the twin. Rule (Sep 15 2026): every resting Gemini BID comes
    off (nothing left to mirror — a fill here would be a naked bet), and whatever Gemini HOLDS
    gets the standing ask (cost+1/cost; exactly cost from T-30) so it rinses out. Returns
    (flat_qty, flat_px)."""
    if gs["held"] >= 1 and gs["held_avg"]:
        h_bid, h_ask = hc.mirror_book(gs["yes_bid"], gs["yes_ask"], hedge_outcome)
        return round(gs["held"], 4), ask_px(round(gs["held_avg"] + 1e-9, 2), h_bid, h_ask, at_cost=t30)
    return 0.0, None


# ------------------------------------------------------------------ loop
def run(live: bool, once: bool):
    global SOCK
    pc = _poly_client()
    SOCK = GemSocket()
    time.sleep(3)
    _poly_cache: dict = {"at": 0.0, "by_slug": {}}
    while True:
        try:
            pairs = json.loads(CFG.read_text()) if CFG.exists() else []
        except Exception as ex:
            log.error("config unreadable: %s", ex); pairs = []
        now = dt.datetime.now(dt.timezone.utc)
        SOCK.want({p["gemini_symbol"] for p in pairs})
        poly_due = (time.monotonic() - _poly_cache["at"]) > int(os.getenv("GEMINI_HEDGE_POLY_S", "60"))
        for pair in pairs:
            try:
                kick = _iso(pair.get("kickoff"))
                if poly_due or pair["poly_slug"] not in _poly_cache["by_slug"]:
                    _poly_cache["by_slug"][pair["poly_slug"]] = poly_state(pc, pair["poly_slug"])
                ps = _poly_cache["by_slug"][pair["poly_slug"]]
                gs = gem_state(pair["gemini_symbol"], pair["hedge_outcome"])
                pair["_gem"] = gs
                if ps.get("error"):
                    log.error("%s: %s — pair skipped", pair["poly_slug"], ps["error"]); continue
                poly_side = ps["side"]
                if poly_side is None:
                    # ORPHAN (Sep 15 2026 — the rung-jump class: the Ferrari's recenter moved the Poly
                    # seat to another rung and this slug went empty). Before this the loop `continue`d
                    # here, leaving Gemini's rent bid RESTING with nothing to mirror. Now: bids off,
                    # held inventory keeps its cost ask, hedge_pairs still reports the held leg.
                    _t30 = kick is not None and now >= kick - dt.timedelta(minutes=int(pair.get("flatten_min", 30)))
                    fq, fpx = orphan_plan(gs, pair["hedge_outcome"], _t30)
                    log.info("%s: no Poly leg (no bid, no position) — ORPHAN: bids off, ask %g@%s on %g held [%s]",
                             pair["poly_slug"][-28:], fq, fpx, gs["held"], gs.get("src"))
                    try:
                        _write_hedge_pair(pair["poly_slug"], pair["gemini_symbol"], 0.0, 0.0, gs["held"], pair.get("kickoff"))
                    except Exception as ex:
                        log.warning("hedge_pairs write failed: %s", str(ex)[:120])
                    gem_sync(pair, 0.0, None, 0.0, None, live, fq, fpx)
                    continue
                if pair.get("poly_side") and pair["poly_side"] != poly_side:
                    log.error("%s: config says poly_side=%s but the venue says %s — pair skipped", pair["poly_slug"], pair["poly_side"], poly_side); continue
                state = _state_load()
                if ps["resting_px_own"] is not None and ps["resting_qty"] >= 1:
                    state.setdefault(pair["poly_slug"], {})["last_rest_px_own"] = ps["resting_px_own"]
                    state[pair["poly_slug"]]["last_rest_at"] = now.isoformat()
                    _state_save(state)
                cost_own = held_cost(pair, ps, state) if ps["filled_qty"] >= 1 else None
                poly_px = ps["resting_px_own"] if ps["resting_px_own"] is not None else (cost_own if cost_own is not None else float(pair.get("poly_cost", 0.5)))
                # the map's gemini_outcome is the Gemini side that MATCHES the Poly side; hedge = its opposite
                match_out = pair["gemini_match_outcome"] if poly_side == pair.get("poly_side", poly_side) else hc.opposite(pair["gemini_match_outcome"])
                prim = hc.Leg("poly", match_out, poly_px, qty_filled=ps["filled_qty"], qty_resting=ps["resting_qty"])
                pl = hc.plan(prim, gs["yes_bid"], gs["yes_ask"], join=True, mirror_filled=gs["held"])
                assert pl.hedge_outcome == pair["hedge_outcome"], (pl.hedge_outcome, pair["hedge_outcome"])
                if ps["filled_qty"] >= 1 and cost_own is None:
                    log.error("%s: Poly leg filled but its cost is unknown (no tracked resting price, no poly_cost) — urgent leg REFUSED", pair["poly_slug"][-28:])
                    pl.hedge_qty_now = 0.0
                held_px = cost_own if cost_own is not None else poly_px
                cap = round(min(float(pair.get("max_pair_cost", 1.00)) - held_px, 0.99), 2)   # urgent leg prices off OUR fill price, never the venue blend
                started = kick is not None and now >= kick
                t30 = kick is not None and now >= kick - dt.timedelta(minutes=int(pair.get("flatten_min", 30)))
                t60 = kick is not None and now >= kick - dt.timedelta(minutes=int(pair.get("ask_off_min", 60)))
                flat_qty, flat_px = 0.0, None
                if started:
                    want_rest, urgent = 0.0, 0.0     # kickoff: unfilled bids come off; held hedges ride
                elif t30:
                    want_rest, urgent = 0.0, pl.hedge_qty_now   # T-30: rent bid off; a Poly-filled leg still closes
                else:
                    want_rest, urgent = pl.hedge_qty_rest, pl.hedge_qty_now
                # THE ASK LEG (Rob, Sep 14: "Both filled, game hours/days away… sell orders on both. RENT above
                # all."): whenever the Lambo HOLDS its leg, one post-only SELL rests for the whole held qty —
                # cost + 1 tick when that alone leads the ask side, else cost; never below cost, never through
                # the bid — and it stays working through the game (the Poly leg's scalp ask does the same).
                # If it sells, the runner re-bids under the cap next loop: rinse, repeat, rent on every seat.
                # From T-30 the un-paired surplus goes to EXACTLY cost (Rob: "sell it at cost T-30").
                if gs["held"] >= 1 and gs["held_avg"]:
                    h_bid, h_ask = hc.mirror_book(gs["yes_bid"], gs["yes_ask"], pair["hedge_outcome"])
                    cost = round(gs["held_avg"] + 1e-9, 2)
                    surplus = gs["held"] - ps["filled_qty"]
                    paired_qty = min(gs["held"], ps["filled_qty"])
                    if t60 and surplus <= 0.5:
                        # Rob, Sep 14 (T-60 rule): both legs held → the ask comes OFF at T-60 and the hedge rides.
                        # Never sell one side late and wreck the pair. (Only the un-paired surplus keeps an ask.)
                        cost = None
                    else:
                        px = ask_px(cost, h_bid, h_ask, at_cost=(t30 and surplus > 0.5))
                    if cost is not None:
                        # after T-60 only the un-paired surplus may carry an ask; before it, everything held does
                        ask_qty = round(gs["held"] - paired_qty, 4) if t60 else round(gs["held"], 4)
                        if ask_qty >= 1:
                            flat_qty, flat_px = ask_qty, px
                log.info("%s | poly %s@%.3f (cost %s, venue avg %s) rest %g filled %g | gemini %s book %s/%s held %g | rest %g@%s lock %s | urgent %g cap %.2f | ask %g@%s | %s [%s]",
                         pair["poly_slug"][-28:], poly_side, poly_px, cost_own, ps["held_avg_own"], ps["resting_qty"], ps["filled_qty"], pair["hedge_outcome"].upper(),
                         gs["yes_bid"], gs["yes_ask"], gs["held"], want_rest, pl.rest_price, pl.locked_if_rest_fills, urgent, cap, flat_qty, flat_px,
                         "KICKED" if started else ("T-30" if t30 else ("T-60" if t60 else pl.note)), gs.get("src"))
                # urgent leg (Rob, Sep 14: "We never cross… we don't take"): a POST-ONLY bid that LEADS the
                # bid side by one tick (joins on a one-tick book), capped at the pair cap — never at/above the ask.
                h_bid, h_ask = hc.mirror_book(gs["yes_bid"], gs["yes_ask"], pair["hedge_outcome"])
                lead = hc.rest_quote(h_bid, h_ask, join=False)
                urgent_px = None if lead is None else round(min(cap, lead), 2)
                # tell the Ferrari (hedge_pairs): which Poly slugs carry a HELD Gemini leg — its scalp arm
                # drops its ask on those inside T-60 (Rob: both sells off, hedge rides). Box-local psql.
                try:
                    _pq = min(gs["held"], ps["filled_qty"])
                    _write_hedge_pair(pair["poly_slug"], pair["gemini_symbol"], _pq, ps["filled_qty"], gs["held"], pair.get("kickoff"))
                except Exception as ex:
                    log.warning("hedge_pairs write failed: %s", str(ex)[:120])
                gem_sync(pair, want_rest, pl.rest_price, urgent, urgent_px, live, flat_qty, flat_px)
            except Exception as ex:
                log.exception("pair %s failed: %s", pair.get("poly_slug"), str(ex)[:200])
        if poly_due:
            _poly_cache["at"] = time.monotonic()
        if once:
            return
        # wake on a socket frame (1s debounce) or on the heartbeat, whichever first
        SOCK.wake.wait(timeout=int(os.getenv("GEMINI_HEDGE_LOOP_S", "30")))
        time.sleep(1.0)
        SOCK.wake.clear()


def _selftest():
    orders = [{"marketSlug": "tsc-nfl-min-chi-2026-09-20-total-45pt5", "side": "ORDER_SIDE_BUY",
               "intent": "ORDER_INTENT_SELL_SHORT", "price": {"value": "0.45"}, "quantity": 19,
               "leavesQuantity": 19, "state": "ORDER_STATE_NEW"}]
    pos = {"netPosition": "-19", "avgPx": {"value": "0.2920"}, "cost": {"value": "5.6270"}}
    r = parse_poly(orders, pos, "tsc-nfl-min-chi-2026-09-20-total-45pt5")
    assert r["side"] == "no" and r["held_side"] == "no", r            # long UNDER
    assert r["resting_qty"] == 0 and r["resting_side"] is None, r    # the SELL_SHORT ask is NOT a seat
    assert abs(r["held_avg_own"] - 0.292) < 1e-9, r   # short: avgPx as-is (cost 5.627/19 = 0.296 agrees)
    # a real BUY_SHORT bid on the same side is a seat
    orders2 = [dict(orders[0], intent="ORDER_INTENT_BUY_SHORT", price={"value": "0.55"})]
    r2 = parse_poly(orders2, pos, orders[0]["marketSlug"])
    assert r2["resting_side"] == "no" and r2["resting_qty"] == 19 and abs(r2["resting_px_own"] - 0.45) < 1e-9, r2
    # a bid on the OPPOSITE side of a held position is refused
    orders3 = [dict(orders[0], intent="ORDER_INTENT_BUY_LONG")]
    assert parse_poly(orders3, pos, orders[0]["marketSlug"])["error"], "must refuse"
    # ORPHAN: Poly leg gone, Gemini holds 19 NO at 43¢ on a 50/60 YES book (NO 40/50) → bids off, ask cost+1
    gs = {"yes_bid": 0.50, "yes_ask": 0.60, "held": 19.0, "held_avg": 0.43, "orders": []}
    fq, fpx = orphan_plan(gs, "no", False)
    assert fq == 19 and abs(fpx - 0.44) < 1e-9, (fq, fpx)          # 0.44 leads the NO ask (0.50) → cost+1
    fq, fpx = orphan_plan(gs, "no", True)
    assert abs(fpx - 0.43) < 1e-9, fpx                              # T-30: exactly cost
    # NO bid sitting ABOVE our cost (YES 40/46 → NO 54/60): one tick over the bid, never at/under it
    fq, fpx = orphan_plan({"yes_bid": 0.40, "yes_ask": 0.46, "held": 19.0, "held_avg": 0.43, "orders": []}, "no", False)
    assert abs(fpx - 0.55) < 1e-9, fpx
    assert orphan_plan({"yes_bid": 0.4, "yes_ask": 0.46, "held": 0.0, "held_avg": None, "orders": []}, "no", False) == (0.0, None)
    # ask never through the ask, one tick over a bid at/above cost
    assert abs(ask_px(0.43, 0.45, 0.60) - 0.46) < 1e-9
    assert abs(ask_px(0.59, 0.40, 0.60) - 0.59) < 1e-9        # cost+1 would only JOIN the ask → rest at cost
    print("gemini_hedge parse_poly + orphan selftest ok")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest(); sys.exit(0)
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true"); ap.add_argument("--once", action="store_true")
    a = ap.parse_args()
    run(a.live, a.once)
