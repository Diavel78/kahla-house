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
    try:
        out["cancel"] = {"raw": _g.cancel_order(oid), "ms": int((time.monotonic() - t1) * 1000)}
    except Exception as e:
        out["cancel"] = {"error": f"{type(e).__name__}: {e}"[:400], "ms": int((time.monotonic() - t1) * 1000)}
    gone = find_active(oid, tgt["symbol"], tries=4)
    out["gone"] = not gone.get("seen") and not gone.get("error")
    out["gone_read"] = gone
    if out["verify"].get("seen") and out["gone"]:
        out["verdict"] = "ok"
    elif out["gone"]:
        out["verdict"] = "ok_unverified_rest"
    else:
        out["verdict"] = "cancel_unverified"
    return out
