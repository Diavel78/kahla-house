"""Opponent-adjusted, recency-weighted power ratings.

The bot's INDEPENDENT number — the whole point of not just echoing
Pinnacle. Raw season averages (points/game, ERA) are useless without
knowing WHO a team played; this turns a season of final scores into
schedule-adjusted strength ratings.

Method: iterative adjusted offense / defense (an SRS variant). Each
team gets an `off` (points it would score vs a league-average defense)
and a `def` (points it would allow vs a league-average offense), solved
by repeatedly adjusting each team's raw scoring for the strength of the
opponents it actually faced. Recent games weigh more (teams change —
trades, slumps, returning injuries) via an exponential half-life decay.

Pure Python, no numpy — runs anywhere (sandbox cron, Vercel). Sport-
agnostic: "points" = runs (MLB), goals (NHL), points (everything else).

Outputs, per team:
  off  — adjusted points-for   (≈ league_avg when average)
  def  — adjusted points-against (≈ league_avg when average)
  net  — off − def = expected margin vs an average team on a neutral field
  gp   — games played in the window
  w    — total recency weight of those games

Projection (`project`) combines two teams' off/def into expected
home/away scores, a margin, and a total. `margin_to_prob` maps the
margin to a win probability via a per-sport logistic scale. The HFA and
scale constants live in SPORT_PARAMS but are deliberately overridable —
Phase 3 will fit them from history instead of using these defaults.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone


# Per-sport home-field advantage (points/runs/goals) + logistic scale
# (margin that moves win-prob ~1 logit) + recency half-life (days). These
# are reasonable defaults from general betting knowledge — Phase 3
# calibration replaces them with values fit to the season's results.
SPORT_PARAMS: dict[str, dict] = {
    "MLB":   {"hfa": 0.20, "scale": 1.6, "half_life_days": 25},
    "NBA":   {"hfa": 2.5,  "scale": 7.0, "half_life_days": 20},
    "CBB":   {"hfa": 3.5,  "scale": 7.5, "half_life_days": 20},
    "NFL":   {"hfa": 2.0,  "scale": 8.0, "half_life_days": 40},
    "NCAAF": {"hfa": 3.0,  "scale": 9.0, "half_life_days": 40},
    "NHL":   {"hfa": 0.20, "scale": 1.6, "half_life_days": 25},
}


def _parse_dt(v) -> datetime | None:
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except Exception:
        return None


def _recency_weight(age_days: float, half_life_days: float | None) -> float:
    """Exponential decay. age 0 → 1.0; one half-life → 0.5. None disables
    weighting (all games weight 1.0). Negative age (future) clamps to 1.0."""
    if half_life_days is None or half_life_days <= 0:
        return 1.0
    if age_days <= 0:
        return 1.0
    return 0.5 ** (age_days / half_life_days)


def compute_ratings(games: list[dict], *,
                    half_life_days: float | None = 25.0,
                    iterations: int = 25,
                    as_of: datetime | None = None) -> dict | None:
    """Adjusted off/def ratings from a list of completed games.

    games: dicts with keys `date`, `home`, `away`, `home_score`,
    `away_score`. Rows missing a score or a team are skipped. Only games
    on or before `as_of` (default now) are used — pass an earlier `as_of`
    to compute ratings "as they were" for backtesting.

    Returns {"league_avg", "teams": {name: {off,def,net,gp,w}}, "n_games",
    "as_of"} or None when there isn't enough data (no valid games).
    """
    now = as_of or datetime.now(timezone.utc)

    # Normalize + filter. Each game contributes two team-game scoring
    # observations (home scored X, away scored Y).
    clean: list[tuple] = []   # (home, away, hs, as_, weight)
    for g in games:
        home = (g.get("home") or "").strip()
        away = (g.get("away") or "").strip()
        hs, as_ = g.get("home_score"), g.get("away_score")
        if not (home and away) or hs is None or as_ is None:
            continue
        try:
            hs, as_ = float(hs), float(as_)
        except (TypeError, ValueError):
            continue
        dt = _parse_dt(g.get("date"))
        if dt and dt > now:
            continue
        age_days = ((now - dt).total_seconds() / 86400.0) if dt else 0.0
        w = _recency_weight(age_days, half_life_days)
        if w <= 0:
            continue
        clean.append((home, away, hs, as_, w))

    if not clean:
        return None

    # Per-team weighted game lists: (opponent, scored, allowed, weight).
    teams: dict[str, list[tuple]] = {}
    total_pts_w = 0.0
    total_w = 0.0
    for home, away, hs, as_, w in clean:
        teams.setdefault(home, []).append((away, hs, as_, w))
        teams.setdefault(away, []).append((home, as_, hs, w))
        total_pts_w += (hs + as_) * w
        total_w += 2.0 * w

    league_avg = total_pts_w / total_w if total_w else 0.0

    def _wavg(pairs: list[tuple[float, float]]) -> float:
        # pairs of (value, weight)
        sw = sum(w for _, w in pairs)
        if sw <= 0:
            return league_avg
        return sum(v * w for v, w in pairs) / sw

    # Initialize with raw weighted offense/defense.
    off = {t: _wavg([(s, w) for (_o, s, _a, w) in gl]) for t, gl in teams.items()}
    deff = {t: _wavg([(a, w) for (_o, _s, a, w) in gl]) for t, gl in teams.items()}

    # Iterate: adjust each team's offense for opponents' defensive
    # strength and vice-versa, relative to league average.
    for _ in range(max(1, iterations)):
        new_off, new_def = {}, {}
        for t, gl in teams.items():
            off_pairs, def_pairs = [], []
            for (opp, scored, allowed, w) in gl:
                opp_def = deff.get(opp, league_avg)
                opp_off = off.get(opp, league_avg)
                # Points scored, adjusted UP if opponent's D was tough.
                off_pairs.append((scored - (opp_def - league_avg), w))
                # Points allowed, adjusted DOWN if opponent's O was potent.
                def_pairs.append((allowed - (opp_off - league_avg), w))
            new_off[t] = _wavg(off_pairs)
            new_def[t] = _wavg(def_pairs)
        off, deff = new_off, new_def

    out_teams = {}
    for t, gl in teams.items():
        out_teams[t] = {
            "off": round(off[t], 4),
            "def": round(deff[t], 4),
            "net": round(off[t] - deff[t], 4),
            "gp":  len(gl),
            "w":   round(sum(w for *_x, w in gl), 4),
        }
    return {
        "league_avg": round(league_avg, 4),
        "teams":      out_teams,
        "n_games":    len(clean),
        "as_of":      now.isoformat(),
    }


def project(ratings: dict, home: str, away: str, *,
            hfa: float = 0.0) -> dict | None:
    """Project a matchup from computed ratings. Returns expected home/away
    scores, margin (home perspective), and total. None if either team is
    missing from the ratings. `home`/`away` match by exact name first,
    then case-insensitive substring (ratings are keyed by whatever name
    the results feed used)."""
    if not ratings:
        return None
    teams = ratings.get("teams") or {}
    league_avg = ratings.get("league_avg") or 0.0

    def _find(name: str):
        if name in teams:
            return teams[name]
        nl = (name or "").lower()
        for k, v in teams.items():
            kl = k.lower()
            if nl and (nl in kl or kl in nl):
                return v
        return None

    h, a = _find(home), _find(away)
    if not (h and a):
        return None
    exp_home = h["off"] + (a["def"] - league_avg) + hfa / 2.0
    exp_away = a["off"] + (h["def"] - league_avg) - hfa / 2.0
    return {
        "exp_home": round(exp_home, 2),
        "exp_away": round(exp_away, 2),
        "margin":   round(exp_home - exp_away, 2),   # home perspective
        "total":    round(exp_home + exp_away, 2),
        "home_net": h["net"],
        "away_net": a["net"],
        "home_gp":  h.get("gp", 0),
        "away_gp":  a.get("gp", 0),
    }


def margin_to_prob(margin: float, scale: float) -> float:
    """Home margin → home win probability via a logistic. `scale` is the
    margin worth ~1 logit (per-sport, from SPORT_PARAMS). Clamped to
    [0.01, 0.99]."""
    if scale <= 0:
        scale = 1.0
    try:
        p = 1.0 / (1.0 + math.exp(-margin / scale))
    except OverflowError:
        p = 1.0 if margin > 0 else 0.0
    return min(max(p, 0.01), 0.99)


def calibrate(games: list[dict], ratings: dict,
              fallback_hfa: float = 0.0,
              fallback_scale: float = 1.0) -> dict | None:
    """Fit HFA + logistic scale from the actual results, replacing the
    eyeballed SPORT_PARAMS guesses.

    HFA = the mean home margin REMAINING AFTER the opponent adjustment —
    i.e. the mean of (actual margin − margin projected at hfa=0). Scale =
    the value that minimizes the Brier score of margin_to_prob(projected
    margin, scale) against actual home wins, grid-searched. Returns {hfa,
    scale, brier, n} or None when too few gradable games. In-sample (one
    grid-searched parameter + a residual mean — negligible overfit) but on
    the same opponent-adjusted ratings the live projection uses, so it's
    the right mapping.

    ⚠ HFA IS A RESIDUAL, NOT THE RAW MEAN HOME MARGIN. This used to be
    `mean(home_score − away_score)`, which is only a valid HFA estimate
    when home and away teams are of equal average quality. NFL schedules
    are balanced, so it looked right there (fitted 1.97 vs the hand-set
    2.0) and nobody checked further. **NCAAF schedules are not** — Power-5
    programs host cupcakes — so the raw mean came out **+8.70**, of which
    roughly six points is team quality the opponent-adjusted ratings had
    ALREADY counted. Double-counted, that put every NCAAF projection
    **5.24 points too favorable to the home team** (measured walk-forward,
    582 games), while win-prob accuracy fell only 70.8% → 69.2% — which is
    why a metric suite built on win probability never saw it. A 5-point
    margin bias is fatal to spread pricing and merely annoying to a
    moneyline, and football is a spread business.
    """
    if not (games and ratings):
        return None
    # Residual HFA: what home still gains once both teams' strength is
    # already in the projection. Falls back to the raw mean only if the
    # projection can't be built for any game.
    resid, raw = [], []
    for g in games:
        hs, as_ = g.get("home_score"), g.get("away_score")
        if hs is None or as_ is None:
            continue
        margin = float(hs) - float(as_)
        raw.append(margin)
        flat = project(ratings, g["home"], g["away"], hfa=0.0)
        if flat:
            resid.append(margin - flat["margin"])
    if not raw:
        return None
    hfa = (sum(resid) / len(resid)) if len(resid) >= 30 \
        else (sum(raw) / len(raw))

    # Build (model_margin, home_won) pairs from the ratings projection.
    pairs: list[tuple[float, int]] = []
    for g in games:
        hs, as_ = g.get("home_score"), g.get("away_score")
        if hs is None or as_ is None or hs == as_:
            continue
        proj = project(ratings, g["home"], g["away"], hfa=hfa)
        if not proj:
            continue
        pairs.append((proj["margin"], 1 if float(hs) > float(as_) else 0))
    if len(pairs) < 30:
        return None

    # Grid-search scale for minimum Brier. Wide grid covers run sports
    # (~1-3) through point sports (~6-12).
    best_scale, best_brier = fallback_scale, None
    s = 0.5
    while s <= 16.0:
        b = sum((margin_to_prob(m, s) - w) ** 2 for m, w in pairs) / len(pairs)
        if best_brier is None or b < best_brier:
            best_brier, best_scale = b, s
        s += 0.25
    return {
        "hfa":   round(hfa, 3),
        "scale": round(best_scale, 2),
        "brier": round(best_brier, 4),
        "n":     len(pairs),
    }
