"""Gridiron IQ — margin → COVER probability.

The gap this fills, stated plainly: Gridiron IQ's native output is a
projected MARGIN (off/def cross + fitted HFA). Its published accuracy —
NFL 66.5% / NCAAF 71.2% walk-forward — is measured on the WIN probability,
which is that margin squeezed through a logistic and is the least useful
projection of it. To bet a spread we need the other projection: given a
projected margin, how often does this team beat a LINE.

That is one missing object: the distribution of (actual margin − projected
margin). Everything else already exists.

WHY NOT A NORMAL (the thing to not casually "simplify" this into).
Football margins are integers that pile up on key numbers — 3 and 7 above
all, then 10, 14, 6. A normal spreads that mass smoothly and therefore
misprices exactly the lines books post most often. So the residual is kept
on the INTEGERS:

    r = actual_margin − round(projected_margin)

`r` is an integer, so its empirical distribution keeps the key-number lumps
instead of averaging them away, and the projection's fractional part is the
only thing discarded.

WHY HALF-POINT LINES MAKE THIS EASIER THAN IT LOOKS. Polymarket posts
football spreads on half points (`asc-nfl-atl-ind-…` at −21.5). A half-point
line cannot push, so `cover_prob` never has to split mass sitting exactly on
the line — the hardest part of spread pricing is absent at the venue we
actually bet. Key numbers still matter for the DENSITY either side of them,
which is the whole reason for keeping the integer PMF.

SHRINKAGE FIRST, DISTRIBUTION SECOND — and the shrinkage is the bigger
term. Measured walk-forward on the 2025 seasons, the raw projection is
systematically TOO EXTREME at both tails:

    NCAAF, mean (actual − projected) by projected margin
      proj ≤ −14   +6.92      proj ≥ +14   −5.69
    NFL
      proj ≤  −7   +2.92      proj ≥ +14   −4.64

i.e. a projected blowout under-delivers by five to seven points, in both
directions, in both sports. That is opponent-adjusted ratings overfitting a
partial season, and it is the same disease the Diamond totals work hit
(there the cure was the line-anchored form at β=+0.37). So `fit()` regresses
actual on projected and prices the SHRUNK projection:

    proj_cal = alpha + beta * proj        NFL β≈0.66, NCAAF β≈0.70

which collapses the tail bias to under ±2 points and drops the residual SD
as well. Pricing a spread off the raw margin would sell the tails at prices
the model itself cannot support.

THREE DISTRIBUTIONS, DELIBERATELY. `fit()` builds all three and the backtest
picks by measured calibration rather than by argument:
  "empirical" — the smoothed integer PMF. Faithful, hungry for data.
  "normal"    — fitted mean/SD. Smooth, wrong at key numbers.
  "blend"     — empirical where the sample supports it, normal in the tails.
One NFL season is 286 games; that is thin for a tail, which is exactly what
"blend" exists to survive.

NOT WIRED TO ANYTHING. This prices a line; it does not decide to bet one.
Promotion runs the standard earn-in: backtest → shadow rows → CLV.
"""
from __future__ import annotations

import math

# Residual support. NFL margins run to ~±50; beyond that the empirical tail
# is a handful of blowouts and the normal carries it.
_R_MIN, _R_MAX = -60, 60

# Triangular kernel over neighbouring integers. Wide enough to fill the gaps
# a 286-game sample leaves, narrow enough that a 3 does not smear into a 5.
_KERNEL = {-2: 0.5, -1: 1.0, 0: 2.0, 1: 1.0, 2: 0.5}

# Below this many observations the empirical PMF is not trusted on its own.
_BLEND_FLOOR_N = 400


def _norm_cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def fit(pairs: list[tuple[float, float]]) -> dict | None:
    """Fit the residual distribution from (projected_margin, actual_margin).

    Returns a state dict carrying all three distributions, or None when
    there is nothing to fit. Never raises on odd input — a pair that will
    not become two floats is skipped.
    """
    clean: list[tuple[float, float]] = []
    for proj, actual in pairs or []:
        try:
            clean.append((float(proj), float(actual)))
        except (TypeError, ValueError):
            continue
    n = len(clean)
    if n < 30:
        return None

    # Shrink the projection toward the mean before anything else. Ordinary
    # least squares of actual on projected; beta < 1 is the over-extremity
    # measured above. Guard the degenerate case where every projection is
    # identical (no variance to regress on) by falling through to beta=1.
    mx = sum(x for x, _ in clean) / n
    my = sum(y for _, y in clean) / n
    sxx = sum((x - mx) ** 2 for x, _ in clean)
    if sxx > 1e-9:
        beta = sum((x - mx) * (y - my) for x, y in clean) / sxx
        alpha = my - beta * mx
    else:
        beta, alpha = 1.0, 0.0
    # A beta outside this range is a fitting accident on thin data, not a
    # discovery. Clamp rather than price off it.
    beta = min(max(beta, 0.2), 1.2)

    res = [int(round(y - round(alpha + beta * x))) for x, y in clean]

    mean = sum(res) / n
    var = sum((r - mean) ** 2 for r in res) / max(n - 1, 1)
    sd = math.sqrt(var) if var > 0 else 1.0

    counts: dict[int, float] = {}
    for r in res:
        if r < _R_MIN or r > _R_MAX:
            r = max(_R_MIN, min(_R_MAX, r))
        for k, w in _KERNEL.items():
            rk = r + k
            if _R_MIN <= rk <= _R_MAX:
                counts[rk] = counts.get(rk, 0.0) + w
    tot = sum(counts.values()) or 1.0
    emp = {k: v / tot for k, v in counts.items()}

    # Empirical gets the weight its sample earns; the normal covers the rest.
    w_emp = min(1.0, n / float(_BLEND_FLOOR_N))
    return {"n": n, "mean": round(mean, 3), "sd": round(sd, 3),
            "alpha": round(alpha, 3), "beta": round(beta, 4),
            "emp": emp, "w_emp": round(w_emp, 3)}


def _emp_tail(state: dict, threshold: float, centre: int) -> float:
    """P(centre + r > threshold) under the empirical PMF."""
    need = threshold - centre
    return sum(p for r, p in state["emp"].items() if r > need)


def cover_prob(state: dict, proj_margin: float, line: float,
               dist: str = "blend") -> float | None:
    """P(the HOME team covers `line`).

    `line` is the home team's own spread in the usual sign convention:
    −3.5 means home laid 3.5, +7.5 means home got 7.5. Home covers when
    `actual_margin + line > 0`, i.e. margin > −line.

    Half-point lines cannot push, so this is a clean two-way probability.
    Whole-point lines are accepted, but the mass sitting exactly ON the
    number becomes a push that this function does NOT model — it counts
    that mass as a loss for the home side. Do not price whole numbers with
    it without adding push handling.
    """
    if not state:
        return None
    try:
        proj = float(proj_margin)
        threshold = -float(line)
    except (TypeError, ValueError):
        return None

    # THE SHRUNK PROJECTION IS THE ONE WE PRICE. See the header.
    proj = state.get("alpha", 0.0) + state.get("beta", 1.0) * proj
    centre = int(round(proj))
    frac = proj - centre        # the part round() discarded

    if dist == "normal":
        p = 1.0 - _norm_cdf((threshold - (proj + state["mean"])) / state["sd"])
    else:
        # Shift the threshold by the discarded fraction and the residual
        # mean so the integer PMF still sits where the projection says.
        p_emp = _emp_tail(state, threshold - frac - state["mean"], centre)
        if dist == "empirical":
            p = p_emp
        else:
            p_norm = 1.0 - _norm_cdf(
                (threshold - (proj + state["mean"])) / state["sd"])
            w = state.get("w_emp", 1.0)
            p = w * p_emp + (1.0 - w) * p_norm
    return min(max(p, 0.005), 0.995)


def american(p: float) -> int | None:
    """Fair American odds for a probability. The card's language."""
    try:
        p = float(p)
    except (TypeError, ValueError):
        return None
    if not 0.0 < p < 1.0:
        return None
    return int(round(-100.0 * p / (1.0 - p))) if p >= 0.5 \
        else int(round(100.0 * (1.0 - p) / p))
