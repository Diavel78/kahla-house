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
    "NFL":   200,    # weekly — need most of the season for enough games
    "NCAAF": 200,
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
        if _write_snapshot(sb, sport, ratings, params):
            log.info("compute %s: %d teams from %d games (league_avg %.2f)",
                     sport, len(ratings["teams"]), ratings["n_games"],
                     ratings["league_avg"])
            done += 1

    log.info("compute_power_ratings done: %d sports rated", done)
    return 0


if __name__ == "__main__":
    sys.exit(main())
