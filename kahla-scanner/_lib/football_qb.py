"""Football QB adjustment — the one roster term Gridiron IQ carries.

Pure functions; no I/O. Spec: docs/football-qb-adjust-spec.md.

The ratings rate a team as a bundle of past points. This module answers
one question per team: is the quarterback who will throw the next game
the same quality as the one who threw the games the rating was built on?
The difference, in ANY/A, times a fitted points-per-ANY/A `k`, moves the
team's expected points. Nothing else.

MIRRORED in app.py? No — app.py only READS the per-team adjustment the
compute script writes (`football_qb_adj`); the math lives here only.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

QB_HALF_LIFE_DAYS = 400.0     # a QB's skill is a career trait, not a form line
QB_PRIOR_ATT = 200.0          # attempts of shrinkage toward replacement
REPLACEMENT_BELOW_LG = 0.8    # replacement = league ANY/A − this
LEAGUE_WINDOW_DAYS = 365.0
MIN_DROPBACKS = 8             # a game counts for a passer past this
ADJ_CAP_PTS = 10.0

# Points per unit of ANY/A. FIT by scripts/backtest_football_qb.py; these
# are the fallbacks the compute script uses when machine_flags carries no
# override. Literature scale: ~35 dropbacks × ~1/15 pt per yard ≈ 2.3.
K_DEFAULT = {"NFL": 4.8, "NCAAF": 4.5}   # fits Sep 13 2026: NFL 4.84 ± 1.08 (t 4.5, 3 seasons); NCAAF 4.54 ± 1.67 (t 2.7, one season)


def parse_dt(v) -> datetime | None:
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    if not v:
        return None
    try:
        s = str(v)
        if len(s) == 10:
            return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def anya(row: dict) -> tuple[float, float] | None:
    """(ANY/A, dropbacks) for one passer-game, or None below the floor."""
    att = row.get("pass_att") or 0
    sacks = row.get("sacks") or 0
    db = float(att) + float(sacks)
    if db < MIN_DROPBACKS:
        return None
    yds = float(row.get("pass_yds") or 0)
    td = float(row.get("pass_td") or 0)
    ints = float(row.get("pass_int") or 0)
    syds = float(row.get("sack_yds") or 0)
    return (yds + 20.0 * td - 45.0 * ints - syds) / db, db


def _w(age_days: float, half_life: float) -> float:
    if age_days < 0:
        age_days = 0.0
    return 0.5 ** (age_days / half_life)


def league_anya(rows: list[dict], as_of: datetime) -> float | None:
    """Attempt-weighted league ANY/A over the trailing window."""
    num = den = 0.0
    for r in rows:
        d = parse_dt(r.get("game_date") or r.get("event_start"))
        if not d or d >= as_of or (as_of - d).days > LEAGUE_WINDOW_DAYS:
            continue
        got = anya(r)
        if not got:
            continue
        v, db = got
        num += v * db
        den += db
    return (num / den) if den > 0 else None


def qb_quality(games: list[dict], as_of: datetime, replacement: float,
               half_life: float = QB_HALF_LIFE_DAYS,
               prior_att: float = QB_PRIOR_ATT) -> tuple[float, float]:
    """(quality, effective dropbacks) for one passer from his games before
    `as_of`. Empty history → (replacement, 0)."""
    num = den = 0.0
    for r in games:
        d = parse_dt(r.get("game_date") or r.get("event_start"))
        if not d or d >= as_of:
            continue
        got = anya(r)
        if not got:
            continue
        v, db = got
        w = db * _w((as_of - d).days, half_life)
        num += v * w
        den += w
    q = (num + replacement * prior_att) / (den + prior_att)
    return q, den


def primary_passer(team_rows: list[dict]) -> dict | None:
    """The passer with the most attempts among one team's rows for one game."""
    best = None
    for r in team_rows:
        if (r.get("pass_att") or 0) <= 0:
            continue
        if best is None or (r.get("pass_att") or 0) > (best.get("pass_att") or 0):
            best = r
    return best


def team_baseline(rated_games: list[dict], quality_of, as_of: datetime,
                  rating_half_life: float, window_days: float) -> dict | None:
    """The QB the rating was built on.

    `rated_games`: [{date, passer_id, passer_name}] — one per team game,
    the primary passer of that game. `quality_of(pid)` → float. Weighted
    exactly as the ratings weight the games. Returns
    {q, top_id, top_name, n} or None with no games."""
    num = den = 0.0
    share: dict[str, float] = {}
    names: dict[str, str] = {}
    for g in rated_games:
        d = parse_dt(g.get("date"))
        if not d or d >= as_of or (as_of - d).days > window_days:
            continue
        pid = g.get("passer_id")
        if not pid:
            continue
        w = _w((as_of - d).days, rating_half_life)
        num += quality_of(pid) * w
        den += w
        share[pid] = share.get(pid, 0.0) + w
        names[pid] = g.get("passer_name") or pid
    if den <= 0:
        return None
    top = max(share, key=share.get)
    return {"q": num / den, "top_id": top, "top_name": names[top],
            "top_share": share[top] / den, "n": len(share)}


def adjust_pts(starter_q: float, baseline_q: float, k: float,
               cap: float = ADJ_CAP_PTS) -> float:
    a = k * (starter_q - baseline_q)
    return max(-cap, min(cap, a))


def ols_slope(xs: list[float], ys: list[float]) -> dict | None:
    """Slope of y on x through the origin-free OLS, with SE and t. The fit
    for k: x = QB delta (home − away, ANY/A), y = margin residual."""
    n = len(xs)
    if n < 30 or n != len(ys):
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx < 1e-9:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    b = sxy / sxx
    a = my - b * mx
    rss = sum((y - (a + b * x)) ** 2 for x, y in zip(xs, ys))
    se = math.sqrt(rss / (n - 2) / sxx) if n > 2 else float("inf")
    return {"k": b, "alpha": a, "se": se, "t": (b / se) if se > 0 else 0.0,
            "n": n, "x_sd": math.sqrt(sxx / n)}
