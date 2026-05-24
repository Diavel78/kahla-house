"""Ingest completed game final scores from ESPN into game_results.

Free public ESPN scoreboard, one call per sport per date. Idempotent —
upserts on (sport, espn_id), so re-running over the same dates just
refreshes scores (e.g. a game that flipped from in-progress to final).

This is the raw material the power-ratings engine solves from. Runs as a
cron step (once a day is plenty — finals don't change) and supports a
backfill via --days N to seed history at the start of a season.

CLI:
  python -m scripts.ingest_results               # last 2 days, all sports
  python -m scripts.ingest_results --days 120     # backfill a season
  python -m scripts.ingest_results --sport MLB --days 30
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from storage import supabase_client as db

log = logging.getLogger(__name__)

# Same sport → ESPN (group, league) map as the resolvers. UFC excluded —
# no team scores to rate.
_ESPN_PATH: dict[str, tuple[str, str]] = {
    "MLB":   ("baseball",   "mlb"),
    "NBA":   ("basketball", "nba"),
    "NHL":   ("hockey",     "nhl"),
    "NFL":   ("football",   "nfl"),
    "CBB":   ("basketball", "mens-college-basketball"),
    "NCAAF": ("football",   "college-football"),
}

_EASTERN = ZoneInfo("America/New_York")


def _fetch_scoreboard(sport: str, yyyymmdd: str) -> list[dict[str, Any]]:
    pair = _ESPN_PATH.get(sport)
    if not pair:
        return []
    grp, lg = pair
    url = (f"https://site.api.espn.com/apis/site/v2/sports/{grp}/{lg}/scoreboard")
    try:
        r = httpx.get(url, params={"dates": yyyymmdd}, timeout=15)
        if r.status_code != 200:
            log.warning("ESPN %s %s -> %s", sport, yyyymmdd, r.status_code)
            return []
        return (r.json() or {}).get("events", []) or []
    except Exception as e:
        log.warning("ESPN %s %s exception: %s", sport, yyyymmdd, e)
        return []


def _extract(sport: str, event: dict) -> dict | None:
    """Pull a finished game's final score into a game_results row, or None
    if the game isn't final / can't be parsed."""
    comp = (event.get("competitions") or [{}])[0]
    state = ((comp.get("status") or {}).get("type") or {}).get("state", "")
    if state != "post":
        return None
    cs = comp.get("competitors") or []
    if len(cs) != 2:
        return None
    h = next((c for c in cs if c.get("homeAway") == "home"), None)
    a = next((c for c in cs if c.get("homeAway") == "away"), None)
    if not (h and a):
        return None

    def _name(c):
        return ((c.get("team") or {}).get("displayName") or "").strip()

    def _score(c):
        v = c.get("score")
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    home, away = _name(h), _name(a)
    hs, as_ = _score(h), _score(a)
    if not (home and away) or hs is None or as_ is None:
        return None

    start_iso = comp.get("date") or event.get("date")
    game_date = None
    if start_iso:
        try:
            dt = datetime.fromisoformat(str(start_iso).replace("Z", "+00:00"))
            game_date = dt.astimezone(_EASTERN).date().isoformat()
        except Exception:
            pass
    return {
        "sport":       sport,
        "espn_id":     str(event.get("id") or comp.get("id") or ""),
        "event_start": start_iso,
        "game_date":   game_date,
        "home":        home,
        "away":        away,
        "home_score":  hs,
        "away_score":  as_,
    }


def _upsert(sb, rows: list[dict]) -> int:
    if not rows:
        return 0
    # Drop rows with no espn_id (can't dedup) — rare.
    rows = [r for r in rows if r.get("espn_id")]
    if not rows:
        return 0
    try:
        sb.table("game_results").upsert(rows, on_conflict="sport,espn_id").execute()
        return len(rows)
    except Exception as e:
        log.error("game_results upsert failed (%d rows): %s", len(rows), e)
        return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=2,
                    help="how many days back to scan (default 2; use a big "
                         "number once to backfill a season)")
    ap.add_argument("--sport", default=None,
                    help="limit to one sport (default: all)")
    args = ap.parse_args(argv)

    sports = [args.sport] if args.sport else list(_ESPN_PATH)
    sb = db.client()
    today_e = datetime.now(timezone.utc).astimezone(_EASTERN).date()

    total = 0
    for sport in sports:
        if sport not in _ESPN_PATH:
            log.warning("unknown sport %s — skipping", sport)
            continue
        sport_rows = 0
        for d in range(args.days + 1):
            day = today_e - timedelta(days=d)
            events = _fetch_scoreboard(sport, day.strftime("%Y%m%d"))
            rows = [r for r in (_extract(sport, e) for e in events) if r]
            sport_rows += _upsert(sb, rows)
        log.info("ingest %s: %d final games over %d days", sport, sport_rows, args.days + 1)
        total += sport_rows

    log.info("ingest_results done: %d game rows upserted", total)
    return 0


if __name__ == "__main__":
    sys.exit(main())
