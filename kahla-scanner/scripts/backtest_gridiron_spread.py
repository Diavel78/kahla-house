"""Backtest the Gridiron IQ margin → COVER conversion.

THE QUESTION THIS ANSWERS, and the one it does not.

Answers: given a projected margin, is our probability that a team beats a
LINE calibrated? That is the object we lack — Gridiron IQ's validated
accuracy (NFL 66.5% / NCAAF 71.2%) is measured on the win probability,
which is the margin squeezed through a logistic. Betting a spread needs the
other projection.

Does NOT answer: do we beat the market? That needs historical closing
spreads, which we do not store for football — `book_snapshots` froze at the
June cutover and `pm_snapshots` only began carrying football recently. So
this is gate 1 of the standard earn-in, exactly as NRFI and Diamond IQ ran
it. Gate 2 (market lines) accrues forward from pm_snapshots once football
lists; gate 3 is CLV on shadow rows.

WALK-FORWARD, TWO LAYERS, NO LOOKAHEAD. At each game date the ratings come
from prior games only (the existing harness), AND the residual distribution
is fit from (projection, actual) pairs observed on STRICTLY EARLIER dates —
accumulated as the walk proceeds. A residual distribution fit on the games
it is scoring would grade itself.

GRADED EVENTS ARE NOT INDEPENDENT. Each game is scored at a grid of lines
spanning its projection, which is what exercises the probability range —
but the events inside one game share one outcome. So `events` is the Brier
denominator and `games` is the honest sample size; both are reported and
you should read the second one.

CLI:
  python -m scripts.backtest_gridiron_spread
  python -m scripts.backtest_gridiron_spread --sport NFL --resid-warmup 80
"""
from __future__ import annotations

import argparse
import logging
import sys
from collections import defaultdict
from datetime import datetime, timezone

from _lib import gridiron_spread as gsp
from _lib import power_ratings as pr

log = logging.getLogger(__name__)

_WINDOW_DAYS = {"NFL": 250, "NCAAF": 250}
_SPORTS = ("NFL", "NCAAF")

# Offsets (points) from the projection at which to price a line. Spans the
# range a real book posts around a projection, and exercises probabilities
# from roughly 10% to 90%.
_OFFSETS = (-14, -10, -7, -3, -1, 0, 1, 3, 7, 10, 14)

_DISTS = ("blend", "empirical", "normal")


def _rows_to_games(rows: list[dict]) -> list[dict]:
    out = []
    for r in rows or []:
        dt = pr._parse_dt(r.get("event_start"))
        if not dt or r.get("home_score") is None or r.get("away_score") is None:
            continue
        out.append({"home": r["home"], "away": r["away"],
                    "home_score": float(r["home_score"]),
                    "away_score": float(r["away_score"]),
                    "date": dt})
    return out


def _fetch_json(path: str, sport: str) -> list[dict]:
    """Games from a JSON dump of game_results rows.

    Exists because the Claude Code sandbox cannot import the supabase client
    (a cffi/cryptography load bug that panics at import), so without this the
    model could only ever be run from CI. Dump with run_sql.sh, iterate here,
    and the same code path runs in the workflow against the live DB.
    """
    import json
    with open(path) as fh:
        rows = json.load(fh)
    return _rows_to_games([r for r in rows
                           if not sport or r.get("sport") == sport])


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
    return _rows_to_games(rows)


def backtest_sport(sb, sport: str, warmup: int, min_gp: int,
                   resid_warmup: int, json_path: str | None = None,
                   params_mode: str = "fitted",
                   market: str = "spread") -> dict | None:
    games = (_fetch_json(json_path, sport) if json_path
             else _fetch_games(sb, sport))
    if len(games) < warmup + 40:
        log.info("%s: only %d games — need > %d, skipping",
                 sport, len(games), warmup + 40)
        return None

    params = pr.SPORT_PARAMS.get(sport, {})
    static_hfa = params.get("hfa", 0.0)
    half_life = params.get("half_life_days")
    window_days = _WINDOW_DAYS.get(sport, 250)

    by_date: dict = defaultdict(list)
    for g in games:
        by_date[g["date"].date()].append(g)
    dates = sorted(by_date)

    pairs: list[tuple[float, float]] = []      # (proj, actual), past only
    n_games = 0
    stats = {d: {"brier": 0.0, "n": 0, "logloss": 0.0,
                 "calib": defaultdict(lambda: [0, 0])} for d in _DISTS}
    abs_err, err_n = 0.0, 0

    for d in dates:
        prior = [g for g in games if g["date"].date() < d]
        if len(prior) < warmup:
            continue
        cutoff = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
        ratings = pr.compute_ratings(
            [g for g in prior if (cutoff - g["date"]).days <= window_days],
            half_life_days=half_life, as_of=cutoff)
        if not ratings:
            continue

        # PARAMETERS PRODUCTION WOULD ACTUALLY HOLD.
        #
        # `compute_power_ratings` writes calibrate()'s fitted hfa/scale into
        # the snapshot, and `_gridiron_ml` prices off the snapshot -- while
        # this harness used to grade at the hand-set SPORT_PARAMS values.
        # Validated-at-one-number, deployed-at-another is how NCAAF ran a
        # +8.70 HFA in production for months while its published accuracy
        # was measured at 3.0. Fit here, from the same prior window, so the
        # thing under test is the thing that ships. --params static restores
        # the old behaviour to measure the gap on purpose.
        hfa = static_hfa
        if params_mode == "fitted":
            cal = pr.calibrate(
                [g for g in prior if (cutoff - g["date"]).days <= window_days],
                ratings, static_hfa, params.get("scale", 1.0))
            if cal:
                hfa = cal["hfa"]

        # Residual state from pairs seen on strictly earlier dates only.
        state = gsp.fit(pairs) if len(pairs) >= resid_warmup else None
        todays: list[tuple[float, float]] = []

        for g in by_date[d]:
            proj = pr.project(ratings, g["home"], g["away"], hfa=hfa)
            if not proj:
                continue
            h = ratings["teams"].get(g["home"]) or {}
            a = ratings["teams"].get(g["away"]) or {}
            if h.get("gp", 0) < min_gp or a.get("gp", 0) < min_gp:
                continue
            actual = (g["home_score"] + g["away_score"] if market == "total"
                      else g["home_score"] - g["away_score"])
            projected = proj["total"] if market == "total" else proj["margin"]
            todays.append((projected, actual))
            if state is None:
                continue

            n_games += 1
            abs_err += abs(projected - actual); err_n += 1
            centre = int(round(projected))
            for off in _OFFSETS:
                # Half-point threshold => no push, the venue's own convention.
                thr = centre + off + 0.5
                # spread: `line` is the home side's own spread, home covers
                # when margin > -line. total: OVER covers when the total
                # clears the number, which is the same tail with line = -thr.
                line = -thr
                covered = 1.0 if actual > thr else 0.0
                for dist in _DISTS:
                    p = gsp.cover_prob(state, projected, line, dist=dist)
                    if p is None:
                        continue
                    s = stats[dist]
                    s["brier"] += (p - covered) ** 2
                    s["logloss"] -= (covered * _log(p)
                                     + (1 - covered) * _log(1 - p))
                    s["n"] += 1
                    b = int(min(p, 0.9999) * 10) / 10.0
                    s["calib"][b][0] += covered
                    s["calib"][b][1] += 1

        pairs.extend(todays)

    if not n_games:
        return None
    out = {"sport": sport, "games": n_games, "pairs": len(pairs),
           "params_mode": params_mode, "market": market,
           "margin_mae": abs_err / max(err_n, 1), "dists": {}}
    for dist in _DISTS:
        s = stats[dist]
        if not s["n"]:
            continue
        out["dists"][dist] = {
            "events": s["n"],
            "brier": s["brier"] / s["n"],
            "logloss": s["logloss"] / s["n"],
            "calibration": {f"{k:.1f}-{k + 0.1:.1f}":
                            {"pred_mid": round(k + 0.05, 2),
                             "actual": round(v[0] / v[1], 3), "n": v[1]}
                            for k, v in sorted(s["calib"].items()) if v[1] >= 25},
        }
    return out


def _log(x: float) -> float:
    import math
    return math.log(max(x, 1e-9))


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--sport", default=None, choices=[*_SPORTS, None])
    ap.add_argument("--warmup", type=int, default=60,
                    help="prior games required before ratings are trusted")
    ap.add_argument("--min-gp", type=int, default=4,
                    help="min games played per team before a game counts")
    ap.add_argument("--resid-warmup", type=int, default=80,
                    help="prior (proj, actual) pairs before grading begins")
    ap.add_argument("--market", default="spread",
                    choices=("spread", "total"),
                    help="spread = margin vs a line; total = projected total "
                         "vs a line (NFL totals pay rent in all three "
                         "periods, so they are a real lane, but MLB's totals "
                         "history says prove it, do not assume it)")
    ap.add_argument("--params", default="fitted",
                    choices=("fitted", "static"),
                    help="fitted = the hfa production actually holds "
                         "(calibrate(), walk-forward); static = SPORT_PARAMS")
    ap.add_argument("--json", default=None,
                    help="read game_results rows from a JSON dump instead of "
                         "Supabase (the sandbox cannot import the client)")
    args = ap.parse_args(argv)

    sports = [args.sport] if args.sport else list(_SPORTS)
    sb = None
    if not args.json:
        from storage import supabase_client as db
        sb = db.client()

    log.info("=" * 68)
    log.info("GRIDIRON BACKTEST — projection → cover, walk-forward")
    log.info("Gate 1 only: is the cover probability CALIBRATED?")
    log.info("Beating the market is gate 2 and needs historical lines.")
    log.info("=" * 68)

    any_run = False
    for sport in sports:
        res = backtest_sport(sb, sport, args.warmup, args.min_gp,
                             args.resid_warmup, args.json, args.params,
                             args.market)
        if not res:
            continue
        any_run = True
        log.info("")
        log.info("%s %s — %d games graded (%d residual pairs), MAE %.2f "
                 "[params: %s]",
                 res["sport"], res["market"].upper(), res["games"],
                 res["pairs"], res["margin_mae"], res["params_mode"])
        log.info("  %-10s %8s %8s %9s", "dist", "brier", "logloss", "events")
        for dist, m in res["dists"].items():
            log.info("  %-10s %8.4f %8.4f %9d",
                     dist, m["brier"], m["logloss"], m["events"])
        log.info("  (coinflip baseline: brier 0.2500, logloss 0.6931)")
        best = min(res["dists"], key=lambda d: res["dists"][d]["brier"])
        log.info("  best by brier: %s", best)
        log.info("  calibration (%s):", best)
        for bucket, v in res["dists"][best]["calibration"].items():
            log.info("    p %s  predicted %.2f  actual %.3f  (n=%d)",
                     bucket, v["pred_mid"], v["actual"], v["n"])
    if not any_run:
        log.info("nothing graded — no sport had enough history")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
