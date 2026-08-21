"""Compute opponent-adjusted power ratings from game_results.

Reads the last ~N days of finals per sport, runs the adjusted off/def
solver (_lib/power_ratings), and writes one snapshot row per sport into
power_ratings. Flask's dossier reads the latest snapshot per sport.

Cheap (a few Supabase reads + pure-Python math) — runs as a cron step
after ingest_results. Idempotent: each run just appends a fresh snapshot;
Flask always reads the most recent.

CLI:
  python -m scripts.compute_power_ratings
  python -m scripts.compute_power_ratings --sport MLB --window-days 90
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timedelta, timezone

from storage import supabase_client as db
from _lib import power_ratings as pr

log = logging.getLogger(__name__)

# Season-ish lookback per sport — long enough for stable ratings, short
# enough to stay current-season. Recency weighting (half-life) inside the
# solver handles within-window staleness.
_WINDOW_DAYS = {
    "MLB":   100,
    "NBA":   120,
    "CBB":   120,
    # Football is weekly AND has a 7-month gap between seasons, so the
    # window must reach ALL of last season or early-season ratings solve
    # from ~a dozen playoff games (a 200-day window in September only sees
    # back to February). 365 keeps last season in view as a cold-start
    # prior; the 40-day half-life decays it to near-nothing by midseason,
    # so this never bleeds stale form into a mature season.
    "NFL":   365,
    "NCAAF": 365,
    "NHL":   120,
}


def _fetch_games(sb, sport: str, since_iso: str) -> list[dict]:
    try:
        rows = (sb.table("game_results")
                .select("home,away,home_score,away_score,event_start")
                .eq("sport", sport)
                .gte("event_start", since_iso)
                .order("event_start", desc=True)
                .limit(5000)
                .execute().data) or []
    except Exception as e:
        log.error("game_results fetch failed for %s: %s", sport, e)
        return []
    return [{"home": r["home"], "away": r["away"],
             "home_score": r["home_score"], "away_score": r["away_score"],
             "date": r.get("event_start")} for r in rows]


_SPREAD_FIT_SPORTS = ("NFL", "NCAAF")


def _spread_fit(games: list[dict], params: dict) -> dict | None:
    """Walk-forward (alpha, beta, sd) for the margin → cover conversion.

    WALK-FORWARD ON PURPOSE, even though hfa/scale beside it are fit
    in-sample. Those two are one parameter each and shrug off the overfit;
    the residual SD does not. Projecting past games with ratings that
    already contain their results understates the spread, and an
    understated SD makes every cover probability MORE confident than the
    model has earned — the one direction that costs money. So each date is
    projected from ratings built only from earlier games, exactly as the
    backtest grades it.

    Costs a ratings solve per game date (about a minute for a full NCAAF
    season) in a daily batch job. Returns None rather than raising: a
    missing spread_fit degrades to "no cover probability", never to a
    wrong one.
    """
    try:
        from collections import defaultdict

        from _lib import gridiron_spread as gsp

        hl = params.get("half_life_days")
        # `_fetch_games` leaves `date` as the raw ISO string (compute_ratings
        # parses it itself), so parse once here and key off the real date.
        parsed = []
        for g in games:
            dt = pr._parse_dt(g.get("date"))
            if not dt or g.get("home_score") is None or g.get("away_score") is None:
                continue
            parsed.append({**g, "_dt": dt,
                           "home_score": float(g["home_score"]),
                           "away_score": float(g["away_score"])})
        by_date = defaultdict(list)
        for g in parsed:
            by_date[g["_dt"].date()].append(g)
        dates = sorted(by_date)
        pairs: list[tuple[float, float]] = []
        for d in dates:
            prior = [g for g in parsed if g["_dt"].date() < d]
            if len(prior) < 40:
                continue
            cut = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
            R = pr.compute_ratings(prior, half_life_days=hl, as_of=cut)
            if not R:
                continue
            for g in by_date[d]:
                proj = pr.project(R, g["home"], g["away"],
                                  hfa=params.get("hfa", 0.0))
                if not proj:
                    continue
                pairs.append((proj["margin"],
                              g["home_score"] - g["away_score"]))
        st = gsp.fit(pairs)
        if not st:
            return None
        # Only the three numbers Flask needs for the normal tail. The
        # empirical PMF is deliberately NOT shipped: it graded level with
        # the normal on NFL (brier 0.2027 vs 0.2028) and is 120 entries
        # instead of 3, so the mirror in app.py stays small enough to be
        # obviously correct.
        return {"alpha": st["alpha"], "beta": st["beta"], "sd": st["sd"],
                "mean": st["mean"], "n": st["n"]}
    except Exception as e:
        log.warning("spread fit failed: %s", e)
        return None


def _write_snapshot(sb, sport: str, ratings: dict, params: dict) -> bool:
    try:
        sb.table("power_ratings").insert({
            "sport":          sport,
            "as_of":          ratings.get("as_of"),
            "league_avg":     ratings.get("league_avg"),
            "n_games":        ratings.get("n_games"),
            "half_life_days": params.get("half_life_days"),
            "ratings":        ratings.get("teams"),
            "params":         params,
        }).execute()
        return True
    except Exception as e:
        log.error("power_ratings insert failed for %s: %s", sport, e)
        return False


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--sport", default=None, help="limit to one sport")
    ap.add_argument("--window-days", type=int, default=None,
                    help="override the per-sport lookback window")
    args = ap.parse_args(argv)

    sports = [args.sport] if args.sport else list(pr.SPORT_PARAMS)
    sb = db.client()
    now = datetime.now(timezone.utc)

    done = 0
    for sport in sports:
        if sport not in pr.SPORT_PARAMS:
            log.warning("no params for %s — skipping", sport)
            continue
        params = dict(pr.SPORT_PARAMS[sport])
        window = args.window_days or _WINDOW_DAYS.get(sport, 120)
        since = (now - timedelta(days=window)).isoformat()
        games = _fetch_games(sb, sport, since)
        if not games:
            log.info("compute %s: no games in window — skipping", sport)
            continue
        ratings = pr.compute_ratings(
            games,
            half_life_days=params.get("half_life_days"),
            as_of=now,
        )
        if not ratings:
            log.info("compute %s: ratings came back empty — skipping", sport)
            continue
        # Calibrate HFA + scale from the actual results, overriding the
        # eyeballed SPORT_PARAMS defaults. v2 reads hfa/scale from params.
        cal = pr.calibrate(games, ratings,
                           fallback_hfa=params.get("hfa", 0.0),
                           fallback_scale=params.get("scale", 1.0))
        if cal:
            params = {**params, "hfa": cal["hfa"], "scale": cal["scale"],
                      "calibrated": True, "fit_brier": cal["brier"],
                      "fit_n": cal["n"]}
        # SPREAD FIT (football only) — the shrinkage + residual spread that
        # turn a projected margin into a cover probability. Football is the
        # only place we can make a pre-game game market, so it is the only
        # place this is worth the walk.
        if sport in _SPREAD_FIT_SPORTS:
            fit = _spread_fit(games, params)
            if fit:
                params = {**params, "spread_fit": fit}
        if _write_snapshot(sb, sport, ratings, params):
            cal_frag = (f" · fit hfa={cal['hfa']} scale={cal['scale']} "
                        f"brier={cal['brier']}") if cal else " · uncalibrated"
            log.info("compute %s: %d teams from %d games (league_avg %.2f)%s",
                     sport, len(ratings["teams"]), ratings["n_games"],
                     ratings["league_avg"], cal_frag)
            done += 1

    log.info("compute_power_ratings done: %d sports rated", done)
    return 0


if __name__ == "__main__":
    sys.exit(main())
