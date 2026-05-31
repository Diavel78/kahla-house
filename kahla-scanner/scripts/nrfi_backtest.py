"""Backtest / calibrate the NRFI-YRFI first-inning model.

Pulls completed MLB games over a date window from the MLB Stats API
(which hydrates BOTH the actual 1st-inning linescore AND the probable
starters in one call per date), reconstructs the model's inputs, predicts
P(NRFI), and grades it against what actually happened in the 1st inning.

Reports:
  • n games, actual NRFI base rate (the anchor target ≈ 52-53%)
  • model mean P(NRFI)        — should sit near the base rate if the
                                 logistic is anchored right
  • Brier score               — calibration of the probability (lower
                                 better; 0.25 = a coin flip)
  • accuracy vs majority-class baseline — does the model discriminate at
                                 all beyond "always pick the common side"?
  • a calibration table        — predicted-prob buckets vs realized NRFI%

USE IT TO: (a) confirm the anchor — if model-mean drifts far from the
base rate, nudge NRFI_Q_BASE; (b) tune NRFI_Q_SLOPE — too flat = no
discrimination (Brier ≈ baseline), too steep = overconfident (Brier worse
than baseline despite "accuracy"). Re-run after editing the constants in
BOTH this file and handicapper_web.py (kept in sync — see the MIRROR note).

LIMITATION (be honest, same posture as handicapper_backtest.py): the MLB
Stats API returns CURRENT-SEASON cumulative pitcher/team stats, not
as-of-game-date, so there's mild lookahead leakage in the inputs (a
pitcher's ERA reflects starts AFTER the game we're grading). This is a
CALIBRATION check on the model's FORM + anchor, not a clean walk-forward
edge proof. Lineup/top-of-order OBP isn't reconstructed historically
either — we use team OBP × boost for every game (the `team` fallback
path), so this validates the pitcher + anchor core, not the
posted-lineup refinement. Treat a Brier meaningfully below the
majority-class baseline as "the form is sound, ship it as reference and
let live CLV prove the edge."

CLI:
  python -m scripts.nrfi_backtest                 # last 45 days
  python -m scripts.nrfi_backtest --days 90
  python -m scripts.nrfi_backtest --season 2025 --days 120

No Supabase / no secrets needed — MLB Stats API is free + public.
"""
from __future__ import annotations

import argparse
import logging
import math
import sys
from datetime import datetime, timedelta, timezone

import httpx

log = logging.getLogger(__name__)

# ── MIRROR of handicapper_web.py NRFI_* constants + the logistic. Keep in
#    sync — if you tune one, tune both. (Cross-importing the repo-root
#    Flask module from the kahla-scanner sandbox is fragile, and the math
#    is tiny, so it's duplicated on purpose — same pattern as the sharp
#    helpers mirrored between sharp_alerts.py and _lib/sharp.py.)
NRFI_XR0       = 0.53
NRFI_LG_OBP    = 0.315
NRFI_LG_RA9    = 4.30
NRFI_Q_BASE    = 0.725
NRFI_Q_SLOPE   = 2.0
NRFI_TOP_BOOST = 1.10
_FIP_CONSTANT  = 3.15
_FIP_WEIGHT    = 0.6
_SP_IP_REGRESS = 45.0
_SP_ERA_CLAMP  = (1.5, 8.0)

# MIRROR of handicapper_web._MLB_PARK_FACTORS (keyed by home mascot).
_MLB_PARK_FACTORS = {
    "rockies": 112, "red sox": 104, "reds": 104, "yankees": 103,
    "phillies": 102, "orioles": 101, "rangers": 101, "cubs": 101,
    "diamondbacks": 101, "royals": 101, "angels": 100, "braves": 100,
    "blue jays": 100, "nationals": 100, "twins": 100, "white sox": 100,
    "astros": 99, "dodgers": 99, "cardinals": 99, "brewers": 99,
    "pirates": 98, "mets": 98, "guardians": 98, "marlins": 97,
    "rays": 97, "tigers": 97, "athletics": 97, "padres": 96,
    "giants": 95, "mariners": 94,
}

_BASE = "https://statsapi.mlb.com/api/v1"
_pitcher_cache: dict[int, float | None] = {}
_team_obp_cache: dict[int, float | None] = {}


_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124 Safari/537.36")


def _get(url: str, params: dict | None = None) -> dict | None:
    try:
        r = httpx.get(url, params=params or {}, headers={"User-Agent": _UA}, timeout=15)
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:
        return None


def _to_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _ip_to_float(ip):
    if ip is None:
        return None
    try:
        s = str(ip)
        if "." in s:
            whole, frac = s.split(".", 1)
            outs = int(frac[0]) if frac and frac[0] in "012" else 0
            return float(whole or 0) + outs / 3.0
        return float(s)
    except (TypeError, ValueError):
        return None


def _half_scoreless(xr: float) -> float:
    k0 = math.log(NRFI_Q_BASE / (1.0 - NRFI_Q_BASE)) + NRFI_Q_SLOPE * NRFI_XR0
    z = k0 - NRFI_Q_SLOPE * max(0.0, xr)
    return min(0.92, max(0.40, 1.0 / (1.0 + math.exp(-z))))


def _park(home_name: str) -> float:
    hl = (home_name or "").lower()
    for key, pf in _MLB_PARK_FACTORS.items():
        if key in hl:
            return float(pf)
    return 100.0


def _starter_ra9(pid, season: int) -> float | None:
    """FIP/ERA talent blend, regressed by IP — mirror of
    handicapper_web._starter_runs over the season-stat peripherals."""
    if not pid:
        return None
    if pid in _pitcher_cache:
        return _pitcher_cache[pid]
    data = _get(f"{_BASE}/people/{pid}",
                params={"hydrate": f"stats(group=[pitching],type=[season],season={season})"})
    val = None
    try:
        people = (data or {}).get("people") or []
        splits = (((people[0].get("stats") or [{}])[0].get("splits")) or []) if people else []
        s = (splits[0].get("stat") or {}) if splits else {}
        era = _to_float(s.get("era"))
        if era is not None:
            clamp = lambda v: min(max(v, _SP_ERA_CLAMP[0]), _SP_ERA_CLAMP[1])
            era = clamp(era)
            k9 = _to_float(s.get("strikeoutsPer9Inn"))
            bb9 = _to_float(s.get("walksPer9Inn"))
            hr9 = _to_float(s.get("homeRunsPer9"))
            if None not in (k9, bb9, hr9):
                fip = clamp((13.0 * hr9 + 3.0 * bb9 - 2.0 * k9) / 9.0 + _FIP_CONSTANT)
                talent = clamp(_FIP_WEIGHT * fip + (1.0 - _FIP_WEIGHT) * era)
            else:
                talent = era
            ip = _ip_to_float(s.get("inningsPitched")) or 0.0
            rel = ip / (ip + _SP_IP_REGRESS)
            val = rel * talent + (1.0 - rel) * NRFI_LG_RA9
    except Exception:
        val = None
    _pitcher_cache[pid] = val
    return val


def _team_obp(team_id, season: int) -> float | None:
    if not team_id:
        return None
    if team_id in _team_obp_cache:
        return _team_obp_cache[team_id]
    data = _get(f"{_BASE}/teams/{team_id}/stats",
                params={"stats": "season", "group": "hitting", "season": season})
    val = None
    try:
        sp = ((data.get("stats") or [{}])[0].get("splits") or [{}])
        val = _to_float((sp[0].get("stat") or {}).get("obp")) if sp else None
    except Exception:
        val = None
    _team_obp_cache[team_id] = val
    return val


def _predict_nrfi(game: dict, season: int) -> float | None:
    teams = game.get("teams") or {}
    home = teams.get("home") or {}
    away = teams.get("away") or {}
    home_name = ((home.get("team") or {}).get("name") or "")
    home_sp = _starter_ra9(((home.get("probablePitcher") or {}).get("id")), season)
    away_sp = _starter_ra9(((away.get("probablePitcher") or {}).get("id")), season)
    home_sp = home_sp if home_sp is not None else NRFI_LG_RA9
    away_sp = away_sp if away_sp is not None else NRFI_LG_RA9
    home_obp = _team_obp(((home.get("team") or {}).get("id")), season)
    away_obp = _team_obp(((away.get("team") or {}).get("id")), season)
    if home_obp is None or away_obp is None:
        return None
    home_obp *= NRFI_TOP_BOOST
    away_obp *= NRFI_TOP_BOOST
    park = _park(home_name) / 100.0
    xr_top = NRFI_XR0 * (away_obp / NRFI_LG_OBP) * (home_sp / NRFI_LG_RA9) * park
    xr_bot = NRFI_XR0 * (home_obp / NRFI_LG_OBP) * (away_sp / NRFI_LG_RA9) * park
    return _half_scoreless(xr_top) * _half_scoreless(xr_bot)


def _actual_nrfi(game: dict) -> bool | None:
    """True = NRFI (no run in the 1st), False = YRFI, None = unusable."""
    innings = ((game.get("linescore") or {}).get("innings") or [])
    if not innings:
        return None
    first = innings[0] or {}
    a = ((first.get("away") or {}).get("runs"))
    h = ((first.get("home") or {}).get("runs"))
    if a is None and h is None:
        return None
    return ((a or 0) + (h or 0)) == 0


def _fetch_date(date_str: str, season: int) -> list[dict]:
    data = _get(f"{_BASE}/schedule",
                params={"sportId": 1, "date": date_str,
                        "hydrate": "probablePitcher,linescore,team"})
    out = []
    for d in (data or {}).get("dates", []) or []:
        for g in d.get("games", []) or []:
            code = ((g.get("status") or {}).get("codedGameState") or "")
            if code not in ("F", "O"):   # Final / Game Over only
                continue
            out.append(g)
    return out


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=45, help="window ending yesterday")
    ap.add_argument("--season", type=int, default=None, help="stats season (default: current year)")
    args = ap.parse_args(argv)

    season = args.season or datetime.now(timezone.utc).year
    today = datetime.now(timezone.utc).date()
    samples: list[tuple[float, bool]] = []   # (predicted P(NRFI), actual NRFI)

    for i in range(1, args.days + 1):
        date_str = (today - timedelta(days=i)).strftime("%Y-%m-%d")
        games = _fetch_date(date_str, season)
        for g in games:
            actual = _actual_nrfi(g)
            if actual is None:
                continue
            p = _predict_nrfi(g, season)
            if p is None:
                continue
            samples.append((p, actual))
        log.info("  %s — %d gradable games (running n=%d)", date_str, len(games), len(samples))

    n = len(samples)
    if n == 0:
        log.error("No gradable games — widen --days or check the season.")
        return 1

    base_rate = sum(1 for _, a in samples if a) / n         # actual NRFI%
    model_mean = sum(p for p, _ in samples) / n             # mean predicted NRFI%
    brier = sum((p - (1.0 if a else 0.0)) ** 2 for p, a in samples) / n
    # Majority-class baseline Brier: always predict the base rate.
    base_brier = sum((base_rate - (1.0 if a else 0.0)) ** 2 for _, a in samples) / n
    # Discrimination: predict NRFI when p>0.5; accuracy vs always-majority.
    correct = sum(1 for p, a in samples if (p >= 0.5) == a)
    acc = correct / n
    maj_acc = max(base_rate, 1.0 - base_rate)

    print("\n" + "=" * 60)
    print(f"NRFI MODEL BACKTEST — {n} games, season {season}, last {args.days}d")
    print("=" * 60)
    print(f"  actual NRFI base rate : {base_rate*100:5.1f}%   (anchor target ~52-53%)")
    print(f"  model mean P(NRFI)    : {model_mean*100:5.1f}%   (should sit near base rate)")
    print(f"  Brier (model)         : {brier:.4f}")
    print(f"  Brier (base-rate)     : {base_brier:.4f}   ({'BETTER' if brier < base_brier else 'worse'} than baseline)")
    print(f"  accuracy @ p>0.5      : {acc*100:5.1f}%   vs majority-class {maj_acc*100:5.1f}%")
    print("\n  calibration (predicted bucket → realized NRFI%):")
    for lo in (0.30, 0.40, 0.45, 0.50, 0.55, 0.60):
        hi = lo + (0.10 if lo in (0.30, 0.40) else 0.05)
        bucket = [a for p, a in samples if lo <= p < hi]
        if not bucket:
            print(f"    {lo:.2f}–{hi:.2f}:   (none)")
            continue
        realized = sum(1 for a in bucket if a) / len(bucket)
        print(f"    {lo:.2f}–{hi:.2f}: {realized*100:5.1f}%  (n={len(bucket)})")
    print("=" * 60)
    print("Note: current-season stats = mild lookahead leakage; this is a")
    print("calibration/anchor check, not a clean walk-forward edge proof.")
    print("=" * 60 + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
