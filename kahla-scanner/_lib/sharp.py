"""Sharp Score math + sharp-side detection — shared by Telegram alerts
(scripts/sharp_alerts.py) and the paper-bet pickers (scripts/paper_bets_picker.py
+ steam logger inside sharp_alerts.py).

Mirrors templates/odds.html exactly so on-card chip, Telegram alert, and
paper-bet pick all agree on score + sharp side.

  ML  — score = |cent_distance(opener, current)| capped 10. Side = team
        whose American odds got more negative.
  SPR — score = |point_diff|×10 (line moved) OR |price_diff_cents| (line
        flat). Never additive. Side = whichever side the line moved
        against (primary), price decreased on (fallback).
  TOT — score same as SPR. Side = OVER when total raised / Over price
        falling, UNDER when total lowered / Over price rising.

THE RULE across all markets: sharp side = the side whose bet got HARDER.
Books move odds to balance action; whichever side they made worse to bet
is where the sharp money is flowing.
"""
from __future__ import annotations

from typing import Any

_OPPOSITE_SIDE = {"home": "away", "away": "home", "over": "under", "under": "over"}


def amer_to_cents(p: Any) -> float | None:
    """American odds → 'cents from even money'. -110 → 10, +110 → -10.
    Lets us compute true cent-distance even if the line crosses 100."""
    if p is None:
        return None
    try:
        p = float(p)
    except (TypeError, ValueError):
        return None
    if p < 0:
        return -p - 100
    if p > 0:
        return -(p - 100)
    return 0


def move_score_ml(opener_amer: Any, current_amer: Any) -> int | None:
    o = amer_to_cents(opener_amer)
    c = amer_to_cents(current_amer)
    if o is None or c is None:
        return None
    return min(10, round(abs(c - o)))


def move_score_spr_tot(opener_line: Any, current_line: Any,
                       opener_price: Any, current_price: Any) -> int | None:
    """LINE moved → score = line move, vig drift IGNORED (rebalance).
       LINE flat  → score = vig move (pure juice signal).
       Two distinct signals; never additive."""
    if opener_price is None or current_price is None:
        return None
    pt = abs((current_line or 0) - (opener_line or 0))
    if pt > 0:
        return min(10, round(pt * 10))
    px = abs(current_price - opener_price)
    return min(10, round(px))


def compute_sharp_score(opener: dict | None, current: dict | None,
                        market_type: str) -> int | None:
    """Direct opener-vs-current scoring when the side is already known."""
    if not opener or not current:
        return None
    if market_type == "moneyline":
        return move_score_ml(opener.get("price_american"),
                             current.get("price_american"))
    return move_score_spr_tot(
        opener.get("line"), current.get("line"),
        opener.get("price_american"), current.get("price_american"),
    )


def sharp_for_ml(market_id: str,
                 openers: dict, pin_current: dict) -> tuple | None:
    """ML sharp = team whose American odds got MORE NEGATIVE since opener.

    When one side's data is missing, we use the available side's
    DIRECTION: if it got more favored (negative diff), sharp = that side.
    If it got LESS favored, sharp = the other side — but we don't have
    that side's snapshots to render, so skip rather than name the wrong
    side. Returns (side, score, opener, current) or None."""
    h_op = openers.get((market_id, "moneyline", "home"))
    h_cu = pin_current.get((market_id, "moneyline", "home"))
    a_op = openers.get((market_id, "moneyline", "away"))
    a_cu = pin_current.get((market_id, "moneyline", "away"))

    h_diff = (h_cu["price_american"] - h_op["price_american"]) if (h_op and h_cu) else None
    a_diff = (a_cu["price_american"] - a_op["price_american"]) if (a_op and a_cu) else None

    if h_diff is not None and a_diff is not None:
        if h_diff == a_diff:
            return None
        if h_diff < a_diff:
            side, op, cu = "home", h_op, h_cu
        else:
            side, op, cu = "away", a_op, a_cu
    elif h_diff is not None:
        if h_diff < 0:
            side, op, cu = "home", h_op, h_cu
        else:
            return None
    elif a_diff is not None:
        if a_diff < 0:
            side, op, cu = "away", a_op, a_cu
        else:
            return None
    else:
        return None

    score = move_score_ml(op["price_american"], cu["price_american"])
    if score is None:
        return None
    return side, score, op, cu


def sharp_for_spread(market_id: str,
                     openers: dict, pin_current: dict) -> tuple | None:
    """SPR sharp = side the LINE moved against. Line is primary; vig
    drift accompanying a line shift is rebalance noise. Falls back to
    pure price move when the line is flat."""
    h_op = openers.get((market_id, "spread", "home"))
    h_cu = pin_current.get((market_id, "spread", "home"))
    a_op = openers.get((market_id, "spread", "away"))
    a_cu = pin_current.get((market_id, "spread", "away"))
    if not (h_op and h_cu and a_op and a_cu):
        return None

    h_pt = (h_cu.get("line") or 0) - (h_op.get("line") or 0)
    a_pt = (a_cu.get("line") or 0) - (a_op.get("line") or 0)

    side = None
    if abs(h_pt - a_pt) >= 0.5:
        side = "home" if h_pt < a_pt else "away"
    else:
        h_px = h_cu["price_american"] - h_op["price_american"]
        a_px = a_cu["price_american"] - a_op["price_american"]
        if abs(h_px - a_px) >= 1:
            side = "home" if h_px < a_px else "away"
    if not side:
        return None

    op, cu = (h_op, h_cu) if side == "home" else (a_op, a_cu)
    score = move_score_spr_tot(op.get("line"), cu.get("line"),
                                op["price_american"], cu["price_american"])
    if score is None:
        return None
    return side, score, op, cu


def sharp_for_total(market_id: str,
                    openers: dict, pin_current: dict) -> tuple | None:
    """TOT sharp = LOWERED total or rising Over price → UNDER.
    Raised total or falling Over price → OVER."""
    o_op = openers.get((market_id, "total", "over"))
    o_cu = pin_current.get((market_id, "total", "over"))
    if not (o_op and o_cu):
        return None
    pt_diff = (o_cu.get("line") or 0) - (o_op.get("line") or 0)
    px_diff = o_cu["price_american"] - o_op["price_american"]

    if pt_diff < 0:
        side = "under"
    elif pt_diff > 0:
        side = "over"
    elif px_diff > 0:
        side = "under"
    elif px_diff < 0:
        side = "over"
    else:
        return None

    score = move_score_spr_tot(o_op.get("line"), o_cu.get("line"),
                                o_op["price_american"], o_cu["price_american"])
    if score is None:
        return None

    if side == "under":
        u_op = openers.get((market_id, "total", "under"))
        u_cu = pin_current.get((market_id, "total", "under"))
        if u_op and u_cu:
            return side, score, u_op, u_cu
    return side, score, o_op, o_cu


def move_sharp_side(market_type: str, raw_side: str,
                    cur_snap: dict, ear_snap: dict) -> str | None:
    """Compute the sharp side directly from a single book's move on a
    given (market_type, raw_side). Used by steam detection where we need
    to translate 'book X moved on home side' into 'sharp = home or away'.

    TOT line direction is the answer regardless of which side reported
    the move (raising a total = sharp OVER even when the over-side row
    triggered detection)."""
    cur_p = cur_snap.get("price_american")
    ear_p = ear_snap.get("price_american")
    cur_l = cur_snap.get("line")
    ear_l = ear_snap.get("line")
    if cur_p is None or ear_p is None:
        return None

    if market_type == "moneyline":
        d = cur_p - ear_p
        if d < 0:
            return raw_side
        if d > 0:
            return _OPPOSITE_SIDE.get(raw_side)
        return None

    if market_type == "spread":
        line_diff = (cur_l or 0) - (ear_l or 0)
        if abs(line_diff) >= 0.5:
            return raw_side if line_diff < 0 else _OPPOSITE_SIDE.get(raw_side)
        d = cur_p - ear_p
        if d < 0:
            return raw_side
        if d > 0:
            return _OPPOSITE_SIDE.get(raw_side)
        return None

    if market_type == "total":
        line_diff = (cur_l or 0) - (ear_l or 0)
        if line_diff > 0:
            return "over"
        if line_diff < 0:
            return "under"
        d = cur_p - ear_p
        if raw_side == "over":
            if d < 0:
                return "over"
            if d > 0:
                return "under"
        else:
            if d < 0:
                return "under"
            if d > 0:
                return "over"
        return None

    return None
