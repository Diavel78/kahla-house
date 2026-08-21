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

# The residual fit gets its OWN lookback, wider than the ratings window.
# One season of pairs is what made the walk-forward NFL totals calibration
# fail gate 1 (grading started in November on 80-220 pairs while monthly
# scoring wobbled ±4 points around the fitted intercept); the same code on
# three seasons calibrates within ±2.6pp at every bucket. Ratings stay on
# _WINDOW_DAYS — team form is current-season; the residual DISTRIBUTION is
# stable across seasons (per-season beta 0.645/0.648 in 2024/2025).
_FIT_LOOKBACK_DAYS = 1100

# Inside the fit's walk, ratings for each date come from a bounded prior
# window — the SAME 250 days the backtest harness uses. An unbounded prior
# here would be the validated-at-one-number, deployed-at-another disease
# (the NCAAF +8.70 HFA lesson) in miniature, and it triples the solve cost.
_FIT_RATINGS_WINDOW_DAYS = 250


def _resid_fits(games: list[dict], params: dict) -> dict | None:
    """Walk-forward (alpha, beta, sd) for margin→cover AND total→over.

    WALK-FORWARD ON PURPOSE, even though hfa/scale beside it are fit
    in-sample. Those two are one parameter each and shrug off the overfit;
    the residual SD does not. Projecting past games with ratings that
    already contain their results understates the spread, and an
    understated SD makes every cover probability MORE confident than the
    model has earned — the one direction that costs money. So each date is
    projected from ratings built only from earlier games, exactly as the
    backtest grades it.

    One walk prices both markets — project() returns margin and total from
    the same solve, so the total fit is free. `total_fit` ships in the
    snapshot for the (future) totals lane; nothing consumes it yet.

    Costs a ratings solve per game date (a few minutes for three NCAAF
    seasons) in a daily batch job. Returns None rather than raising: a
    missing fit degrades to "no cover probability", never to a wrong one.
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
        m_pairs: list[tuple[float, float]] = []
        t_pairs: list[tuple[float, float]] = []
        for d in dates:
            cut = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
            prior = [g for g in parsed if g["_dt"].date() < d
                     and (cut - g["_dt"]).days <= _FIT_RATINGS_WINDOW_DAYS]
            # Warmup + per-team gp floor MATCH THE BACKTEST HARNESS
            # (warmup=60, min_gp=4). Without them the fit ingests pairs the
            # harness would never grade — cold-start projections whose noise
            # attenuates beta (measured: 0.43 unguarded vs 0.56 guarded on
            # the same three NFL seasons) — and the thing tested stops being
            # the thing that ships.
            if len(prior) < 60:
                continue
            R = pr.compute_ratings(prior, half_life_days=hl, as_of=cut)
            if not R:
                continue
            for g in by_date[d]:
                h = R["teams"].get(g["home"]) or {}
                a = R["teams"].get(g["away"]) or {}
                if h.get("gp", 0) < 4 or a.get("gp", 0) < 4:
                    continue
                proj = pr.project(R, g["home"], g["away"],
                                  hfa=params.get("hfa", 0.0))
                if not proj:
                    continue
                m_pairs.append((proj["margin"],
                                g["home_score"] - g["away_score"]))
                t_pairs.append((proj["total"],
                                g["home_score"] + g["away_score"]))

        # Only the numbers Flask needs for the normal tail. The empirical
        # PMF is deliberately NOT shipped: it graded level with the normal
        # (NFL spread 0.2027 vs 0.2028; NFL total 0.1918 vs 0.1914 on three
        # seasons) and is 120 entries instead of 3, so the mirror in app.py
        # stays small enough to be obviously correct.
        def _slim(st):
            return {"alpha": st["alpha"], "beta": st["beta"], "sd": st["sd"],
                    "mean": st["mean"], "n": st["n"]} if st else None

        out = {}
        sf, tf = _slim(gsp.fit(m_pairs)), _slim(gsp.fit(t_pairs))
        if sf:
            out["spread_fit"] = sf
        if tf:
            out["total_fit"] = tf
        return out or None
    except Exception as e:
        log.warning("resid fit failed: %s", e)
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
            # Wider pull than the ratings window — see _FIT_LOOKBACK_DAYS.
            fit_since = (now - timedelta(days=_FIT_LOOKBACK_DAYS)).isoformat()
            fit_games = _fetch_games(sb, sport, fit_since) or games
            fits = _resid_fits(fit_games, params)
            if fits:
                params = {**params, **fits}
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
