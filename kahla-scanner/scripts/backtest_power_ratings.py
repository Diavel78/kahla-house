"""Backtest the power-ratings model against actual game results.

Walk-forward (no lookahead): for each game date, compute ratings from
ONLY the games before it, project every game on that date, and grade the
projection against the final score. Reports whether the model actually
predicts outcomes — the evidence that licenses trusting it (and widening
its sizing leash) instead of believing one good-looking dossier.

Metrics per sport:
  • ML accuracy  — model's favorite win rate, vs the home-team baseline
  • Brier score  — win-prob calibration (lower better; 0.25 = coinflip)
  • Margin MAE   — |projected margin − actual margin|
  • Total MAE    — |projected total − actual total|
  • Calibration  — empirical win rate inside model prob buckets

LIMITATION: game_results stores only final scores, so this validates the
opponent-adjusted TEAM ratings core — NOT the MLB starting-pitcher layer
(historical probables aren't stored) and NOT closing-line value (CLV
needs historical closing lines; book_snapshots only retains 15 days, so
the model-vs-close test accrues going forward via bot_picks.clv_pp,
bucketed by model-agree vs model-disagree). This answers "does the
ratings model predict results?" — the necessary first gate.

CLI:
  python -m scripts.backtest_power_ratings
  python -m scripts.backtest_power_ratings --sport MLB --warmup 80
"""
from __future__ import annotations

import argparse
import logging
import sys
from collections import defaultdict
from datetime import datetime, timezone

from storage import supabase_client as db
from _lib import power_ratings as pr

log = logging.getLogger(__name__)

_WINDOW_DAYS = {"MLB": 120, "NBA": 150, "CBB": 150,
                "NFL": 250, "NCAAF": 250, "NHL": 150}


def _fetch_games(sb, sport: str) -> list[dict]:
    try:
        rows = (sb.table("game_results")
                .select("home,away,home_score,away_score,event_start")
                .eq("sport", sport)
                .order("event_start", desc=False)
                .limit(20000)
                .execute().data) or []
    except Exception as e:
        log.error("fetch failed for %s: %s", sport, e)
        return []
    out = []
    for r in rows:
        dt = pr._parse_dt(r.get("event_start"))
        if not dt or r.get("home_score") is None or r.get("away_score") is None:
            continue
        out.append({"home": r["home"], "away": r["away"],
                    "home_score": float(r["home_score"]),
                    "away_score": float(r["away_score"]),
                    "date": dt})
    return out


def backtest_sport(sb, sport: str, warmup: int, min_gp: int) -> dict | None:
    games = _fetch_games(sb, sport)
    if len(games) < warmup + 20:
        log.info("%s: only %d games — need > %d, skipping",
                 sport, len(games), warmup + 20)
        return None

    params = pr.SPORT_PARAMS.get(sport, {})
    hfa = params.get("hfa", 0.0)
    scale = params.get("scale", 1.0)
    half_life = params.get("half_life_days")
    window_days = _WINDOW_DAYS.get(sport, 150)

    # Group by calendar date so we compute ratings once per date (all
    # games that day share the same pre-date ratings — no lookahead).
    by_date: dict = defaultdict(list)
    for g in games:
        by_date[g["date"].date()].append(g)
    dates = sorted(by_date)

    graded = 0
    model_correct = 0
    home_wins = 0          # baseline: always pick home
    brier_sum = 0.0
    margin_ae = 0.0
    total_ae = 0.0
    margin_n = 0
    total_n = 0
    # Calibration: prob bucket → [wins, n] for the side the model favored.
    calib: dict = defaultdict(lambda: [0, 0])

    for di, d in enumerate(dates):
        # Warmup: skip until enough prior games exist.
        prior = [g for g in games if g["date"].date() < d]
        if len(prior) < warmup:
            continue
        cutoff = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
        ratings = pr.compute_ratings(
            [g for g in prior
             if (cutoff - g["date"]).days <= window_days],
            half_life_days=half_life, as_of=cutoff)
        if not ratings:
            continue

        for g in by_date[d]:
            proj = pr.project(ratings, g["home"], g["away"], hfa=hfa)
            if not proj:
                continue
            h_rating = (ratings["teams"].get(g["home"]) or {})
            a_rating = (ratings["teams"].get(g["away"]) or {})
            if (h_rating.get("gp", 0) < min_gp or
                    a_rating.get("gp", 0) < min_gp):
                continue

            actual_margin = g["home_score"] - g["away_score"]
            actual_total = g["home_score"] + g["away_score"]
            if actual_margin == 0:
                continue   # ties (rare) — skip ML grading
            home_won = actual_margin > 0

            p_home = pr.margin_to_prob(proj["margin"], scale)
            graded += 1
            if home_won:
                home_wins += 1
            # Model favorite.
            model_home = p_home >= 0.5
            if model_home == home_won:
                model_correct += 1
            # Brier on home-win probability.
            brier_sum += (p_home - (1.0 if home_won else 0.0)) ** 2
            # Margin / total error.
            margin_ae += abs(proj["margin"] - actual_margin); margin_n += 1
            total_ae += abs(proj["total"] - actual_total); total_n += 1
            # Calibration bucket on the favored side's prob.
            fav_p = p_home if model_home else (1.0 - p_home)
            fav_won = (model_home == home_won)
            b = ("50-55" if fav_p < 0.55 else "55-60" if fav_p < 0.60
                 else "60-70" if fav_p < 0.70 else "70+")
            calib[b][1] += 1
            calib[b][0] += 1 if fav_won else 0

    if graded == 0:
        log.info("%s: no gradable games after warmup", sport)
        return None

    res = {
        "sport": sport,
        "graded": graded,
        "model_acc": round(model_correct / graded, 4),
        "home_baseline": round(home_wins / graded, 4),
        "brier": round(brier_sum / graded, 4),
        "margin_mae": round(margin_ae / margin_n, 3) if margin_n else None,
        "total_mae": round(total_ae / total_n, 3) if total_n else None,
        "calibration": {k: {"win_rate": round(v[0] / v[1], 3), "n": v[1]}
                        for k, v in sorted(calib.items()) if v[1]},
    }
    return res


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--sport", default=None)
    ap.add_argument("--warmup", type=int, default=60,
                    help="prior games required before grading begins")
    ap.add_argument("--min-gp", type=int, default=5,
                    help="min games played per team before a game counts")
    args = ap.parse_args(argv)

    sports = [args.sport] if args.sport else list(pr.SPORT_PARAMS)
    sb = db.client()

    log.info("=" * 64)
    log.info("POWER-RATINGS BACKTEST (walk-forward, team-ratings only)")
    log.info("=" * 64)
    for sport in sports:
        res = backtest_sport(sb, sport, args.warmup, args.min_gp)
        if not res:
            continue
        log.info("")
        log.info("%s — %d games graded", res["sport"], res["graded"])
        log.info("  ML accuracy : %.1f%%   (home baseline %.1f%%)",
                 res["model_acc"] * 100, res["home_baseline"] * 100)
        log.info("  Brier score : %.4f   (0.2500 = coinflip; lower better)",
                 res["brier"])
        log.info("  Margin MAE  : %s runs/pts", res["margin_mae"])
        log.info("  Total MAE   : %s", res["total_mae"])
        log.info("  Calibration (favored side):")
        for bucket, v in res["calibration"].items():
            log.info("     %-6s  win %.1f%%  (n=%d)",
                     bucket, v["win_rate"] * 100, v["n"])
    log.info("")
    log.info("Read: ML accuracy meaningfully ABOVE the home baseline + a")
    log.info("Brier well under 0.25 = the model has real signal. Calibration")
    log.info("win-rates should rise with the prob bucket if it's honest.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
