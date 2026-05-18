"""Push-rate tables + half-point projection for Pick Bot.

When PIN's line is on a whole number (e.g., NBA total 204, NFL spread
-7) but Polymarket offers only half-point lines (204.5 / -7.5), we need
to translate PIN's devigged fair probability to a fair probability at
the half-point. The shift equals the push probability — the fraction
of games that land exactly on PIN's whole-number line.

Tables below are from public sports-betting math literature:
  • NFL spreads: Stanford Wong's "Sharper" + Sports Insights archives.
    The 3 and 7 are the dominant key numbers (margins of victory cluster
    there); off-key whole numbers have very low push rates.
  • NFL totals: BoydsBets / pinnacle-derived archival data.
  • NBA spreads/totals: lower push rates than NFL because scoring is
    finer-grained; rates are roughly flat across lines with small
    bumps at round numbers (200, 210, 220).
  • MLB totals: low push rates; the 7, 8, 9 are mildly key.
  • NHL totals: 5 and 6 are key (modal final totals); push rates spike there.
  • NCAAF/CBB: NCAAF mirrors NFL but with slightly lower 3/7 spikes;
    CBB totals mirror NBA but at lower magnitudes.

Numbers are rounded to the nearest 0.5%. They're approximations — the
actual push rate at a given line varies by season and matchup, but the
order-of-magnitude shift is what matters for limit-order targeting.

Output: a `project_fair_to_half_point` helper that takes PIN's line +
fair_prob for one side + the PMM half-point line and returns the
projected fair_prob at PMM's line.
"""
from __future__ import annotations

from typing import Any


# Per-line push rate (as a probability, 0–1). Looked up by exact line.
# `_default` is the fallback for lines not in the table — applied to
# any whole-number line. Half-point lines never need a projection
# (push impossible), so we return 0 for those.
_PUSH_RATES: dict[str, dict[str, dict[float | str, float]]] = {
    "NFL": {
        "spread": {
            3:  0.095,   # the 3 — by far the biggest key in NFL
            7:  0.055,
            6:  0.035,
            10: 0.030,
            14: 0.025,
            "_default": 0.015,
        },
        "total": {
            41: 0.030,
            44: 0.030,
            47: 0.030,
            51: 0.025,
            "_default": 0.015,
        },
    },
    "NCAAF": {
        "spread": {
            3:  0.075,
            7:  0.045,
            10: 0.025,
            14: 0.020,
            "_default": 0.015,
        },
        "total": {
            "_default": 0.020,
        },
    },
    "NBA": {
        "spread": {
            3: 0.040,
            5: 0.030,
            7: 0.030,
            "_default": 0.025,
        },
        "total": {
            200: 0.030,
            210: 0.030,
            220: 0.030,
            "_default": 0.025,
        },
    },
    "CBB": {
        "spread": {
            3: 0.045,
            7: 0.035,
            "_default": 0.025,
        },
        "total": {
            "_default": 0.025,
        },
    },
    "NHL": {
        "spread": {
            # NHL "puck line" is almost always ±1.5 (half-point already),
            # so spread projection is rarely needed. Default low.
            "_default": 0.015,
        },
        "total": {
            5: 0.060,
            6: 0.050,
            "_default": 0.020,
        },
    },
    "MLB": {
        "spread": {
            # MLB "run line" almost always ±1.5 (half-point already).
            "_default": 0.015,
        },
        "total": {
            7:  0.025,
            8:  0.030,
            9:  0.025,
            10: 0.020,
            "_default": 0.020,
        },
    },
    "UFC": {
        # UFC is binary outcomes — no spread or total in the
        # conventional sense. Method-of-victory props exist but aren't
        # in our scope.
        "spread": {"_default": 0.0},
        "total":  {"_default": 0.0},
    },
}


def _lookup_push_rate(sport: str, market_type: str, line: float) -> float:
    """Return the push probability for a whole-number line. Returns 0
    for half-point lines (no push possible) or unknown sport/market."""
    if line is None:
        return 0.0
    # Half-point line — push impossible.
    if abs(line - round(line)) > 0.001:
        return 0.0
    table = (_PUSH_RATES.get(sport) or {}).get(market_type)
    if not table:
        return 0.0
    whole = int(round(line))
    # Try absolute value too (spread -7 and +7 share the same push
    # probability — the favorite winning by exactly 7 IS the push).
    if whole in table:
        return table[whole]
    if abs(whole) in table:
        return table[abs(whole)]
    return table.get("_default", 0.0)


def project_fair_to_half_point(
    sport: str,
    market_type: str,
    pin_line: float | None,
    pmm_line: float | None,
    side: str,
    pin_fair_prob: float | None,
) -> tuple[float | None, dict[str, Any]]:
    """Translate PIN's devigged fair probability at `pin_line` to the
    PMM-equivalent fair probability at `pmm_line`. Returns
    (projected_fair_prob, meta). `meta` carries diagnostic context for
    the dossier UI (push_rate_used, line_delta, direction).

    Math:
        Each whole-number line has some push probability p.
        Moving to line + 0.5: that push% becomes losses on the
        higher-side bet and wins on the lower-side bet.

        TOTAL:
          line 204 → 204.5: pushes (games totaling 204) are now UNDER
            (204 < 204.5). So p_under_at_204.5 = p_under_at_204 + push%
            and p_over_at_204.5 = p_over_at_204 − push%.

        SPREAD:
          home -7 → -7.5: home now has to cover -7.5 instead of -7,
            so games where home won by exactly 7 (which were pushes
            at -7) are now losses on home -7.5. So
            p_home_at_-7.5 = p_home_at_-7 − push% and
            p_away_at_+7.5 = p_away_at_+7 + push%.

    Supports half-point moves of ±0.5 (the common case). For larger
    moves we'd need to know the push at each whole-number line crossed,
    which isn't reliable enough to project — return None.

    Returns (None, meta) when projection isn't possible (PIN line
    missing, prob missing, half-point delta is wrong direction or > 0.5).
    """
    meta: dict[str, Any] = {
        "pin_line":      pin_line,
        "pmm_line":      pmm_line,
        "side":          side,
        "applicable":    False,
        "push_rate":     None,
        "line_delta":    None,
        "direction":     None,
        "note":          None,
    }
    if pin_fair_prob is None or pin_line is None or pmm_line is None:
        meta["note"] = "missing input"
        return None, meta
    delta = pmm_line - pin_line
    meta["line_delta"] = round(delta, 3)
    # No translation needed — same line.
    if abs(delta) < 0.001:
        meta["applicable"] = True
        meta["note"] = "same line — no projection"
        return pin_fair_prob, meta
    # Only support a half-point hop. Larger gaps would need per-whole-
    # number push lookups, which isn't reliable.
    if abs(abs(delta) - 0.5) > 0.001:
        meta["note"] = f"delta {delta:+.2f} not a half-point hop"
        return None, meta
    push = _lookup_push_rate(sport, market_type, pin_line)
    meta["push_rate"] = push
    if push <= 0:
        meta["applicable"] = True
        meta["note"] = "push rate 0 — no shift needed"
        return pin_fair_prob, meta

    # Determine direction of shift for THIS side at THIS line move.
    #   Direction "+" means this side gets MORE wins under the new line
    #     (probability goes UP).
    #   Direction "-" means this side gets FEWER wins (probability DOWN).
    sign = 1 if delta > 0 else -1
    if market_type == "total":
        # delta > 0 (line raised, e.g. 204 → 204.5): pushes become unders
        if side == "under":
            shift_sign = sign            # under gains when line goes up
        elif side == "over":
            shift_sign = -sign           # over loses when line goes up
        else:
            meta["note"] = f"unknown total side {side}"
            return None, meta
    elif market_type == "spread":
        # IMPORTANT: pin_line is stored from THIS side's perspective.
        # Home line = -7, away line = +7 (they're mirrors). If a game
        # moves from a -7/+7 PIN line to a -7.5/+7.5 PMM line, then:
        #   delta_home = -7.5 - (-7) = -0.5 (negative)
        #   delta_away = +7.5 - +7   = +0.5 (positive)
        # Both sides get HARDER on the +0.5/-0.5 hop respectively, and
        # the math works out the same way: a NEGATIVE delta on a side's
        # own line = harder for that side = prob shifts DOWN.
        # So shift_sign = sign for BOTH spread sides — no mirror flip.
        if side in ("home", "away"):
            shift_sign = sign
        else:
            meta["note"] = f"unknown spread side {side}"
            return None, meta
    else:
        meta["note"] = f"market_type {market_type} not projectable"
        return None, meta

    projected = pin_fair_prob + shift_sign * push
    # Guard against floating-point overshoot near 0/1.
    if projected <= 0 or projected >= 1:
        meta["note"] = "projection went out of (0,1)"
        return None, meta
    meta["applicable"] = True
    meta["direction"] = "+" if shift_sign > 0 else "-"
    return projected, meta
