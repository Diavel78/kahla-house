"""Backtest / calibrate the MLB full-game RUN-TOTAL model (Phase 1).

The O/U engine has no independent number today — it just follows the exchange
total's movement, which is why it loses through every tweak (you can't tune an
edge that was never there). This is the foundation of a REAL run-total model:
project a game's total runs from the inputs we already have (starters,
bullpen/staff, team offense, park) and check whether that projection actually
PREDICTS real run totals. If it can't beat "always project league average,"
there's no point wiring it to bets. If it can, Phase 2 grades it against
historical totals LINES (from our pm_snapshots / book_snapshots) for a true
EV test, then we wire it to drive O/U picks.

Reuses the NRFI backtest's MLB Stats API plumbing (free, public, no secrets):
probable starters, team season stats, park factors.

Reports (predicted total vs ACTUAL total runs):
  • n games, actual mean total + stdev (the thing we're predicting)
  • model mean projected total (should sit near the actual mean — bias check)
  • MAE / RMSE vs a league-average baseline (does the model beat "everyone
    scores ~the league mean"?)
  • Pearson correlation proj-vs-actual (is there ANY signal?)
  • directional hit-rate vs the sample-mean pseudo-line (rough "does it pick
    the right side" proxy until Phase 2 brings real lines)
  • a calibration table — proj-total buckets vs realized mean total

LIMITATION (same honesty as nrfi_backtest): MLB Stats API returns
CURRENT-SEASON cumulative stats, not as-of-game-date → mild lookahead leakage;
and there are no betting LINES here, so this validates the projection's FORM /
predictive signal, NOT betting edge. Beating the line is Phase 2.

CLI:
  python -m scripts.mlb_total_backtest                 # last 45 days
  python -m scripts.mlb_total_backtest --days 90
  python -m scripts.mlb_total_backtest --season 2025 --days 120
"""
from __future__ import annotations

import argparse
import logging
import math
from datetime import datetime, timedelta, timezone

# Reuse the proven MLB Stats API plumbing from the NRFI backtest (same dir).
from scripts.nrfi_backtest import (
    _get, _to_float, _starter_ra9, _park, _fetch_date, _BASE, NRFI_LG_RA9,
)

log = logging.getLogger(__name__)

# ── Run-total model constants (Phase-1 v1 — TUNE from the backtest output) ──
LG_RPG_TEAM = 4.40        # league runs per TEAM per game (~2024-26 MLB)
LG_RA9      = NRFI_LG_RA9  # league RA9 anchor (4.30), shared with NRFI
SP_WEIGHT   = 0.55        # starter share of a full game's run prevention
                          # (~5-6 of 9 innings); bullpen/staff gets the rest
RUNS_CLAMP  = (2.0, 8.0)  # sane per-team expected-runs floor/ceiling

_rpg_cache: dict[int, float | None] = {}
_staff_cache: dict[int, float | None] = {}


def _team_rpg(team_id, season: int) -> float | None:
    """Team runs scored per game (offense index numerator)."""
    if not team_id:
        return None
    if team_id in _rpg_cache:
        return _rpg_cache[team_id]
    data = _get(f"{_BASE}/teams/{team_id}/stats",
                params={"stats": "season", "group": "hitting", "season": season})
    val = None
    try:
        s = (((data.get("stats") or [{}])[0].get("splits") or [{}])[0].get("stat") or {})
        runs = _to_float(s.get("runs"))
        gp = _to_float(s.get("gamesPlayed"))
        if runs and gp and gp > 0:
            val = runs / gp
    except Exception:
        val = None
    _rpg_cache[team_id] = val
    return val


def _team_staff_ra9(team_id, season: int) -> float | None:
    """Whole-staff ERA as a bullpen/run-prevention proxy (RA9-ish)."""
    if not team_id:
        return None
    if team_id in _staff_cache:
        return _staff_cache[team_id]
    data = _get(f"{_BASE}/teams/{team_id}/stats",
                params={"stats": "season", "group": "pitching", "season": season})
    val = None
    try:
        s = (((data.get("stats") or [{}])[0].get("splits") or [{}])[0].get("stat") or {})
        val = _to_float(s.get("era"))
    except Exception:
        val = None
    _staff_cache[team_id] = val
    return val


def _exp_runs(off_rpg, opp_sp_ra9, opp_staff_ra9, park) -> float:
    """Expected runs for one team: league RPG scaled by the geometric mean of
    its offense index and the opponent's (starter+staff blended) run-prevention
    index, times the park factor. Geometric blend keeps a strong offense vs a
    strong-pitching opponent from running away in either direction."""
    off_idx = (off_rpg or LG_RPG_TEAM) / LG_RPG_TEAM
    opp_ra9 = SP_WEIGHT * (opp_sp_ra9 or LG_RA9) + (1.0 - SP_WEIGHT) * (opp_staff_ra9 or LG_RA9)
    pitch_idx = opp_ra9 / LG_RA9
    exp = LG_RPG_TEAM * math.sqrt(max(0.05, off_idx) * max(0.05, pitch_idx)) * (park / 100.0)
    return min(RUNS_CLAMP[1], max(RUNS_CLAMP[0], exp))


def _predict_total(game: dict, season: int) -> float | None:
    teams = game.get("teams") or {}
    home, away = teams.get("home") or {}, teams.get("away") or {}
    hid = (home.get("team") or {}).get("id")
    aid = (away.get("team") or {}).get("id")
    home_rpg, away_rpg = _team_rpg(hid, season), _team_rpg(aid, season)
    if home_rpg is None or away_rpg is None:
        return None
    home_sp = _starter_ra9((home.get("probablePitcher") or {}).get("id"), season)
    away_sp = _starter_ra9((away.get("probablePitcher") or {}).get("id"), season)
    home_staff, away_staff = _team_staff_ra9(hid, season), _team_staff_ra9(aid, season)
    park = _park((home.get("team") or {}).get("name") or "")
    # home offense vs away pitching; away offense vs home pitching
    exp_home = _exp_runs(home_rpg, away_sp, away_staff, park)
    exp_away = _exp_runs(away_rpg, home_sp, home_staff, park)
    return exp_home + exp_away


def _actual_total(game: dict) -> float | None:
    teams = game.get("teams") or {}
    h = (teams.get("home") or {}).get("score")
    a = (teams.get("away") or {}).get("score")
    if h is None or a is None:
        return None
    try:
        return float(h) + float(a)
    except (TypeError, ValueError):
        return None


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=45, help="window ending yesterday")
    ap.add_argument("--season", type=int, default=None, help="stats season (default: current year)")
    args = ap.parse_args(argv)

    season = args.season or datetime.now(timezone.utc).year
    today = datetime.now(timezone.utc).date()
    samples: list[tuple[float, float]] = []   # (predicted total, actual total)

    for i in range(1, args.days + 1):
        date_str = (today - timedelta(days=i)).strftime("%Y-%m-%d")
        games = _fetch_date(date_str, season)
        for g in games:
            actual = _actual_total(g)
            if actual is None:
                continue
            p = _predict_total(g, season)
            if p is None:
                continue
            samples.append((p, actual))
        log.info("  %s — %d final games (running n=%d)", date_str, len(games), len(samples))

    n = len(samples)
    if n == 0:
        log.error("No gradable games — widen --days or check the season.")
        return 1

    act = [a for _, a in samples]
    prd = [p for p, _ in samples]
    act_mean = sum(act) / n
    prd_mean = sum(prd) / n
    act_sd = math.sqrt(sum((a - act_mean) ** 2 for a in act) / n)

    # Errors: model vs the league-average baseline (always predict act_mean).
    mae = sum(abs(p - a) for p, a in samples) / n
    rmse = math.sqrt(sum((p - a) ** 2 for p, a in samples) / n)
    base_mae = sum(abs(act_mean - a) for a in act) / n
    base_rmse = math.sqrt(sum((act_mean - a) ** 2 for a in act) / n)

    # Pearson correlation proj-vs-actual (the signal metric).
    cov = sum((p - prd_mean) * (a - act_mean) for p, a in samples) / n
    prd_sd = math.sqrt(sum((p - prd_mean) ** 2 for p in prd) / n)
    corr = cov / (prd_sd * act_sd) if prd_sd > 0 and act_sd > 0 else 0.0

    # Directional hit-rate vs the sample-mean pseudo-line (rough proxy; real
    # lines come in Phase 2). Skip pushes (actual == mean is ~never exact).
    dir_n = dir_hit = 0
    for p, a in samples:
        if abs(a - act_mean) < 1e-9:
            continue
        dir_n += 1
        if (p > prd_mean) == (a > act_mean):
            dir_hit += 1
    dir_rate = (dir_hit / dir_n) if dir_n else 0.0

    print("\n" + "=" * 64)
    print(f"MLB RUN-TOTAL BACKTEST (Phase 1: predictive form, NO lines yet)")
    print(f"  {n} games, season {season}, last {args.days}d")
    print("=" * 64)
    print(f"  actual mean total     : {act_mean:5.2f}  (sd {act_sd:.2f}) — the target")
    print(f"  model mean projection : {prd_mean:5.2f}  (bias {prd_mean-act_mean:+.2f})")
    print(f"  MAE  model / baseline : {mae:.3f} / {base_mae:.3f}  ({'BETTER' if mae < base_mae else 'worse'})")
    print(f"  RMSE model / baseline : {rmse:.3f} / {base_rmse:.3f}  ({'BETTER' if rmse < base_rmse else 'worse'})")
    print(f"  Pearson corr (signal) : {corr:+.3f}   (>0 = some signal; ~.15-.30 is realistic)")
    print(f"  directional hit-rate  : {dir_rate*100:5.1f}%  vs mean-line ({dir_n} games)")
    print("=" * 64)

    # Calibration: bucket by projected total, show realized mean.
    buckets = [(0, 7.5), (7.5, 8.5), (8.5, 9.5), (9.5, 10.5), (10.5, 99)]
    print("  proj-total bucket   n     mean proj   mean ACTUAL")
    for lo, hi in buckets:
        grp = [(p, a) for p, a in samples if lo <= p < hi]
        if not grp:
            continue
        mp = sum(p for p, _ in grp) / len(grp)
        ma = sum(a for _, a in grp) / len(grp)
        print(f"   [{lo:>4}, {hi:<4})      {len(grp):>4}     {mp:6.2f}      {ma:6.2f}")
    print("=" * 64)
    print("READ: MAE/RMSE BETTER than baseline + positive corr + monotonic")
    print("calibration (higher proj -> higher actual) = the form has signal,")
    print("proceed to Phase 2 (grade vs real totals lines for EV). Flat corr /")
    print("worse-than-baseline = rebuild the projection before wiring anything.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
