"""Gemini Predictions — the WRITE side, step 2 of Gemini replacing Kalshi
(Rob, Nov 8 2026: "IT REPLACES KALSHI… SAME CHASE"; $200 funded that day).

Step 2 starts with the PROOF, the Kalshi pattern (Oct 18 2026): one post-only
1-contract BUY well under the bid on a liquid pre-game NFL contract → verify it
on the venue's own active-orders list → cancel → verify it is gone. Nothing
here decides to bet; it places what it is told, behind the caller's flag.

Venue facts this relies on (docs/gemini-predictions-recon.md, Sep 13 2026):
  * REST POST /v1/prediction-markets/order {symbol, orderType limit, side buy,
    outcome yes|no, quantity, price (that OUTCOME's own terms, dollars),
    timeInForce good-til-cancel, makerOrCancel true = post-only};
    cancel POST …/order/cancel {orderId}; list POST …/orders/active.
  * orderId is a 17-18 digit int — compare as strings, never through JS.
  * NO is a native side: buying NO at q is priced q, not 1 − q.
  * A create that returns is a CLAIM; the active-orders list is the truth.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Callable, Optional

import gemini_pm as _g
import gemini_venue as _gv


def _oid(o: dict) -> Optional[str]:
    for k in ("orderId", "order_id", "id"):
        v = (o or {}).get(k)
        if v not in (None, ""):
            return str(v)
    return None


def find_active(order_id: str, symbol: Optional[str] = None, tries: int = 4, gap_s: float = 1.0) -> dict:
    """Poll the active-orders list for `order_id`. {seen, rows, error}."""
    last_err = None
    rows: list = []
    for i in range(max(1, tries)):
        try:
            rows = _g.active_orders(symbol=symbol, limit=100) or []
            last_err = None
            if any(_oid(r) == str(order_id) for r in rows):
                return {"seen": True, "tries": i + 1, "row": next(r for r in rows if _oid(r) == str(order_id))}
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"[:200]
        time.sleep(gap_s)
    return {"seen": False, "tries": tries, "error": last_err, "n_rows": len(rows)}


def pick_probe_contract(min_bid_c: float = 20.0, max_bid_c: float = 80.0, lead_s: float = 3600.0) -> Optional[dict]:
    """A liquid PRE-GAME NFL spread/total contract (two-sided, bid in band,
    start ≥ lead_s away) — the safest place to rest a 1-lot far under the bid."""
    evs, _ = _gv.sports_events()
    now = time.time()
    best = None
    for (pre, t1, t2, code), g in _gv.game_index(evs).items():
        if pre != "NFL" or g["start"] - now < lead_s:
            continue
        for kind in ("T", "S"):
            for key, q in g[kind].items():
                sym, b, a = q
                if b is None or a is None or not (min_bid_c <= b <= max_bid_c):
                    continue
                spread = a - b
                if best is None or spread < best["spread"]:
                    best = {"symbol": sym, "bid": b, "ask": a, "spread": spread, "start": g["start"]}
    return best


def probe(place: bool = False, symbol: Optional[str] = None, contracts: int = 1, off_c: int = 10,
          cancel: bool = True, log_fn: Optional[Callable] = None) -> dict:
    """The proof. Reads always (creds, balance, terms, active orders,
    positions); with `place`, ONE post-only BUY YES of `contracts` at
    bid − off_c on `symbol` (else a liquid pre-game NFL contract), verified on
    the active list, cancelled, verified gone. Returns every step + a verdict."""
    out: dict[str, Any] = {"at": datetime.now(timezone.utc).isoformat(), "place": bool(place)}
    log = log_fn or (lambda *a, **k: None)
    out["creds"] = _g.has_creds()
    if not out["creds"]:
        out["verdict"] = "no_creds"
        return out
    for name, fn in (("balances", _g.balances), ("terms", _g.terms_status)):
        try:
            v = fn()
            if name == "balances" and isinstance(v, list):
                v = [r for r in v if str(r.get("currency", "")).upper() == "USD"] or v[:3]
            out[name] = v
        except Exception as e:
            out[name] = {"error": f"{type(e).__name__}: {e}"[:300]}
    try:
        act = _g.active_orders(limit=50) or []
        out["active_orders"] = {"n": len(act), "sample": act[:2]}
    except Exception as e:
        out["active_orders"] = {"error": f"{type(e).__name__}: {e}"[:300]}
    try:
        pos = _g.positions(limit=50) or []
        out["positions"] = {"n": len(pos), "sample": pos[:2]}
    except Exception as e:
        out["positions"] = {"error": f"{type(e).__name__}: {e}"[:300]}
    try:
        out["body_mode"] = _g.detect_body_mode()
    except Exception as e:
        out["body_mode"] = {"error": f"{type(e).__name__}: {e}"[:300]}
    if not place:
        out["verdict"] = "reads_only"
        return out
    tgt = None
    if symbol:
        ob = _gv.orderbook(symbol)
        bb = (ob.get("bids") or [(None, 0)])[0][0]
        tgt = {"symbol": symbol, "bid": bb, "ask": (ob.get("asks") or [(None, 0)])[0][0]}
    else:
        tgt = pick_probe_contract()
    out["target"] = tgt
    if not tgt or tgt.get("bid") is None:
        out["verdict"] = "no_target"
        return out
    price_c = max(1, int(tgt["bid"]) - int(off_c))
    cid = f"kh-probe-{int(time.time())}"
    t0 = time.monotonic()
    try:
        res = _g.place_order(tgt["symbol"], "buy", "yes", contracts, price_c / 100.0,
                             post_only=True, client_order_id=cid)
        out["create"] = {"ms": int((time.monotonic() - t0) * 1000), "raw": res, "price_c": price_c, "cid": cid}
    except Exception as e:
        out["create"] = {"ms": int((time.monotonic() - t0) * 1000), "error": f"{type(e).__name__}: {e}"[:400],
                         "price_c": price_c, "cid": cid}
        out["verdict"] = "create_failed"
        return out
    oid = _oid(res if isinstance(res, dict) else {})
    out["order_id"] = oid
    log("gemini_probe create %s %s", oid, res)
    if not oid:
        out["verdict"] = "create_no_id"
        return out
    out["verify"] = find_active(oid, tgt["symbol"])
    if not cancel:
        out["verdict"] = "placed_left_resting" if out["verify"].get("seen") else "placed_unverified"
        return out
    t1 = time.monotonic()
    gone, cdet = cancel_verified(oid, tgt["symbol"])
    out["cancel"] = {**cdet, "ms": int((time.monotonic() - t1) * 1000)}
    out["gone"] = gone is True
    if out["verify"].get("seen") and out["gone"]:
        out["verdict"] = "ok"
    elif out["gone"]:
        out["verdict"] = "ok_unverified_rest"
    else:
        out["verdict"] = "cancel_unverified"
    return out


# ───────────────────────── the v0 lane's write + read helpers ─────────────────────────
# Field names on Gemini's order / position rows were not all measured in September;
# every reader below tries the likely spellings and the probe stamp's `sample` rows
# are the ground truth to tighten them against.

def _f(x) -> Optional[float]:
    try:
        return float(x) if x not in (None, "") else None
    except (TypeError, ValueError):
        return None


def parse_order(o: dict) -> dict:
    """One active-orders row → {id, symbol, outcome, side, price_c, qty, filled, leaves, status}."""
    qty = None
    for k in ("quantity", "originalQuantity", "orderQuantity", "size"):
        qty = _f(o.get(k)) if qty is None else qty
    filled = None
    for k in ("filledQuantity", "executedQuantity", "cumQuantity", "filled"):
        filled = _f(o.get(k)) if filled is None else filled
    leaves = None
    for k in ("remainingQuantity", "leavesQuantity", "remaining"):
        leaves = _f(o.get(k)) if leaves is None else leaves
    if leaves is None and qty is not None:
        leaves = max(0.0, qty - (filled or 0.0))
    px = _f(o.get("price") if o.get("price") is not None else o.get("limitPrice"))
    return {"id": _oid(o), "symbol": o.get("symbol") or o.get("instrumentSymbol"),
            "outcome": str(o.get("outcome") or o.get("eventOutcome") or "").lower(),
            "side": str(o.get("side") or "").lower(),
            "price_c": round(px * 100.0, 2) if px is not None else None,
            "qty": qty, "filled": filled or 0.0, "leaves": leaves,
            "status": str(o.get("status") or o.get("orderStatus") or "").lower()}


def orders_by_id() -> Optional[dict]:
    """Every active order, parsed, by id. None = the read failed (never 'no orders')."""
    try:
        rows = []
        off = 0
        while True:
            page = _g.active_orders(limit=100, offset=off) or []
            rows += page
            if len(page) < 100 or off > 2000:
                break
            off += 100
    except Exception:
        return None
    return {p["id"]: p for p in (parse_order(r) for r in rows) if p.get("id")}


def positions_map() -> Optional[dict]:
    """{(symbol, outcome yes|no): contracts held}. None = the read failed.
    A NO holding may come back as a NEGATIVE quantity (Sep 13 2026 socket read)."""
    try:
        rows = []
        off = 0
        while True:
            page = _g.positions(limit=100, offset=off) or []
            rows += page
            if len(page) < 100 or off > 2000:
                break
            off += 100
    except Exception:
        return None
    out: dict = {}
    parsed = 0
    for r in rows:
        sym = r.get("symbol") or r.get("instrumentSymbol")
        q = None
        # `totalQuantity` + `outcome` = what the Sep 2026 hedge daemon read off live positions
        for k in ("totalQuantity", "quantity", "netQuantity", "size", "position"):
            q = _f(r.get(k)) if q is None else q
        if sym and q is not None:
            parsed += 1
        if not sym or q is None or q == 0:
            continue
        oc = str(r.get("outcome") or r.get("eventOutcome") or "").lower()
        if not oc:
            oc = "no" if q < 0 else "yes"
        out[(sym, oc)] = out.get((sym, oc), 0.0) + abs(q)
    if rows and not parsed:
        return None        # rows we cannot read are UNREADABLE — never "nothing held" (a filled lot would read cancelled)
    return out


def place_rest(symbol: str, buy_yes: bool, price_c: float, qty: int, cid: Optional[str] = None) -> tuple:
    """ONE post-only GTC BUY on our side (YES, or NO priced in NO's own terms),
    then the active list must show it. → (order_id or None, detail)."""
    det: dict = {"symbol": symbol, "outcome": "yes" if buy_yes else "no", "price_c": price_c, "qty": qty}
    t0 = time.monotonic()
    try:
        res = _g.place_order(symbol, "buy", "yes" if buy_yes else "no", int(qty),
                             round(float(price_c) / 100.0, 2), post_only=True, client_order_id=cid)
    except Exception as e:
        det["error"] = f"{type(e).__name__}: {e}"[:300]
        return None, det
    det["ms"] = int((time.monotonic() - t0) * 1000)
    det["raw"] = res
    oid = _oid(res if isinstance(res, dict) else {})
    if not oid:
        det["error"] = "create returned no order id"
        return None, det
    v = find_active(oid, symbol, tries=4, gap_s=0.75)
    det["verified"] = bool(v.get("seen"))
    det["verify"] = {k: v.get(k) for k in ("tries", "error", "n_rows")}
    return oid, det


def cancel_verified(order_id: str, symbol: Optional[str] = None) -> tuple:
    """Cancel, then the active list must NOT show it. → (gone True/False/None, detail)
    None = unreadable (never treat as gone)."""
    det: dict = {"order_id": order_id}
    try:
        det["raw"] = _g.cancel_order(order_id)
    except Exception as e:
        det["error"] = f"{type(e).__name__}: {e}"[:300]
    last_err = None
    for _ in range(4):
        time.sleep(0.75)
        try:
            rows = _g.active_orders(symbol=symbol, limit=100) or []
            last_err = None
            if not any(_oid(r) == str(order_id) for r in rows):
                return True, det
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"[:200]
    det["read_error"] = last_err
    return (None if last_err else False), det
