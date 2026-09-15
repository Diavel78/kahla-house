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
      urgent_qty = Poly filled − Gemini held    → exposure exists NOW: a limit at the pair cap
                                                 (crosses if the ask allows, else rests there)
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


# ------------------------------------------------------------------ Gemini read/write
def gem_state(symbol: str, outcome: str) -> dict:
    bk = g.book(symbol)
    yb = float(bk["bids"][0]["price"]) if bk.get("bids") else None
    ya = float(bk["asks"][0]["price"]) if bk.get("asks") else None
    ours = [o for o in g.active_orders(symbol=symbol) if str(o.get("clientOrderId") or "").startswith(CID)]
    held = 0.0
    for p in g.positions(limit=100):
        if p.get("symbol") == symbol:
            q = float(p.get("totalQuantity") or 0)
            if (p.get("outcome") or "").lower() == outcome:
                held += q
    return {"yes_bid": yb, "yes_ask": ya, "orders": ours, "held": held}


def gem_sync(pair: dict, want_rest_qty: float, rest_px, urgent_qty: float, cap_px, live: bool) -> None:
    sym, out = pair["gemini_symbol"], pair["hedge_outcome"]
    st = pair["_gem"]
    rest_orders = [o for o in st["orders"] if str(o.get("clientOrderId")).startswith(CID + "rest")]
    urg_orders = [o for o in st["orders"] if str(o.get("clientOrderId")).startswith(CID + "urg")]
    budget = float(pair.get("budget_usd", 15.0))

    def _place(role, qty, px, post_only):
        cost = qty * px
        if cost > budget:
            log.warning("%s %s: $%.2f exceeds pair budget $%.2f — capping qty", sym, role, cost, budget)
            qty = int(budget / px)
        if qty < 1:
            return
        cid = f"{CID}{role}-{int(time.time())}"
        if not live:
            log.info("DRY %s %s: would place BUY %s %g @ %.2f (post_only=%s)", sym, role, out.upper(), qty, px, post_only)
            return
        r = g.place_order(sym, "buy", out, qty, px, post_only=post_only, client_order_id=cid)
        log.info("%s %s: placed %s %g @ %.2f → %s", sym, role, out.upper(), qty, px, r.get("status"))
        _led(kind="place", role=role, symbol=sym, outcome=out, qty=qty, px=px, order_id=r.get("orderId"), status=r.get("status"), pair=pair["poly_slug"])

    def _cancel(o, why):
        if not live:
            log.info("DRY %s: would cancel %s (%s @ %s) — %s", sym, o.get("orderId"), o.get("quantity"), o.get("price"), why); return
        g.cancel_order(o["orderId"])
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
        _place("urg", int(urgent_qty), cap_px, False)


# ------------------------------------------------------------------ loop
def run(live: bool, once: bool):
    pc = _poly_client()
    while True:
        try:
            pairs = json.loads(CFG.read_text()) if CFG.exists() else []
        except Exception as ex:
            log.error("config unreadable: %s", ex); pairs = []
        now = dt.datetime.now(dt.timezone.utc)
        for pair in pairs:
            try:
                kick = _iso(pair.get("kickoff"))
                ps = poly_state(pc, pair["poly_slug"])
                gs = gem_state(pair["gemini_symbol"], pair["hedge_outcome"])
                pair["_gem"] = gs
                if ps.get("error"):
                    log.error("%s: %s — pair skipped", pair["poly_slug"], ps["error"]); continue
                poly_side = ps["side"]
                if poly_side is None:
                    log.info("%s: no Poly leg (no bid, no position) — nothing to mirror", pair["poly_slug"]); continue
                if pair.get("poly_side") and pair["poly_side"] != poly_side:
                    log.error("%s: config says poly_side=%s but the venue says %s — pair skipped", pair["poly_slug"], pair["poly_side"], poly_side); continue
                poly_px = ps["resting_px_own"] if ps["resting_px_own"] is not None else float(pair.get("poly_cost", 0.5))
                # the map's gemini_outcome is the Gemini side that MATCHES the Poly side; hedge = its opposite
                match_out = pair["gemini_match_outcome"] if poly_side == pair.get("poly_side", poly_side) else hc.opposite(pair["gemini_match_outcome"])
                prim = hc.Leg("poly", match_out, poly_px, qty_filled=ps["filled_qty"], qty_resting=ps["resting_qty"])
                pl = hc.plan(prim, gs["yes_bid"], gs["yes_ask"], join=True, mirror_filled=gs["held"])
                assert pl.hedge_outcome == pair["hedge_outcome"], (pl.hedge_outcome, pair["hedge_outcome"])
                held_px = ps["held_avg_own"] if ps["held_avg_own"] is not None else poly_px
                cap = round(min(float(pair.get("max_pair_cost", 1.00)) - held_px, 0.99), 2)   # urgent leg prices off the HELD cost
                started = kick is not None and now >= kick
                if started:
                    want_rest, urgent = 0.0, 0.0     # kickoff: unfilled bids come off; held hedges ride
                else:
                    want_rest, urgent = pl.hedge_qty_rest, pl.hedge_qty_now
                if not pair.get("rent_leg", False):
                    want_rest = 0.0   # Rob, Sep 14: hedge what is FILLED. Mirroring a parked Poly bid is a new bet, not a hedge.
                elif want_rest >= 1 and gs["yes_bid"] is not None and gs["yes_ask"] is not None and ps["resting_px_own"] is not None:
                    # at-the-money guard: the Poly bid (in Gemini-YES terms) must sit near Gemini's mid, else it is a
                    # parked seat and its mirror would be a naked bet on the other venue
                    poly_yes_equiv = ps["resting_px_own"] if match_out == "yes" else 1 - ps["resting_px_own"]
                    gap = abs(poly_yes_equiv - (gs["yes_bid"] + gs["yes_ask"]) / 2)
                    if gap > float(pair.get("max_rest_gap", 0.03)):
                        log.info("%s: Poly bid is %.3f off Gemini's mid — parked seat, no rent leg", pair["poly_slug"][-28:], gap)
                        want_rest = 0.0
                log.info("%s | poly %s@%.3f (held avg %s) rest %g filled %g | gemini %s book %s/%s held %g | rest %g@%s lock %s | urgent %g cap %.2f take %s | %s",
                         pair["poly_slug"][-28:], poly_side, poly_px, ps["held_avg_own"], ps["resting_qty"], ps["filled_qty"], pair["hedge_outcome"].upper(),
                         gs["yes_bid"], gs["yes_ask"], gs["held"], want_rest, pl.rest_price, pl.locked_if_rest_fills, urgent, cap, pl.take_price, "KICKED" if started else pl.note)
                # urgent leg: take at the ask if the ask is under the cap; otherwise rest AT the cap
                # only if the cap is not above the touch — never rest above the market (a giveaway).
                if pl.take_price is not None and pl.take_price <= cap:
                    urgent_px = pl.take_price
                elif pl.rest_price is not None:
                    urgent_px = min(cap, pl.rest_price)
                else:
                    urgent_px = None
                gem_sync(pair, want_rest, pl.rest_price, urgent, urgent_px, live)
            except Exception as ex:
                log.exception("pair %s failed: %s", pair.get("poly_slug"), str(ex)[:200])
        if once:
            return
        time.sleep(int(os.getenv("GEMINI_HEDGE_LOOP_S", "30")))


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
    print("gemini_hedge parse_poly selftest ok")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest(); sys.exit(0)
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true"); ap.add_argument("--once", action="store_true")
    a = ap.parse_args()
    run(a.live, a.once)
