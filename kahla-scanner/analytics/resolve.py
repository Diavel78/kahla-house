"""Auto-populate market_outcomes by polling ESPN's scoreboard API.

Runs on a schedule (default: hourly). For each enabled sport, fetches
yesterday + today's ESPN scoreboard, finds completed events, matches them
to markets rows by (sport, event_start ±30min, team names via matcher),
and upserts the winning side into market_outcomes.

ESPN scoreboard API is free, unauthenticated, and reasonably stable.
If ESPN ever changes it, fall back to a different scores provider.

Usage (CLI):
    python -m analytics.resolve --sport NFL
    python -m analytics.resolve              # all enabled sports
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from config import config
from signals import matcher
from storage import supabase_client as db

log = logging.getLogger(__name__)

# Sport key -> ESPN (sport, league) path pair.
ESPN_SPORT: dict[str, tuple[str, str]] = {
    "NFL": ("football", "nfl"),
    "CBB": ("basketball", "mens-college-basketball"),
    "NBA": ("basketball", "nba"),
    "MLB": ("baseball", "mlb"),
    "NHL": ("hockey", "nhl"),
    "NCAAF": ("football", "college-football"),
    # UFC/MMA isn't a standard scoreboard — skip for now.
}

BASE = "https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/scoreboard"


def _fetch_day(sport_key: str, date: datetime) -> list[dict[str, Any]]:
    pair = ESPN_SPORT.get(sport_key)
    if not pair:
        return []
    sport, league = pair
    url = BASE.format(sport=sport, league=league)
    params = {"dates": date.strftime("%Y%m%d")}
    try:
        resp = httpx.get(url, params=params, timeout=15.0)
        if resp.status_code != 200:
            log.warning("ESPN %s %s: %s", sport_key, resp.status_code, resp.text[:200])
            return []
        return resp.json().get("events") or []
    except Exception as e:
        log.warning("ESPN %s fetch exception: %s", sport_key, e)
        return []


def _parse_event(sport: str, ev: dict[str, Any]) -> tuple[str, str, datetime, str | None] | None:
    """Returns (home, away, start_utc, winning_side) if the event is final.

    winning_side is 'home', 'away', or 'void' (tie/cancelled).
    Returns None for in-progress games.
    """
    comps = (ev.get("competitions") or [])
    if not comps:
        return None
    c = comps[0]
    status = ((c.get("status") or {}).get("type") or {})
    if not status.get("completed"):
        return None
    state = status.get("state")
    if state and state != "post":
        return None

    competitors = c.get("competitors") or []
    if len(competitors) != 2:
        return None
    home_c = next((x for x in competitors if x.get("homeAway") == "home"), None)
    away_c = next((x for x in competitors if x.get("homeAway") == "away"), None)
    if not home_c or not away_c:
        return None
    home_name = ((home_c.get("team") or {}).get("displayName") or "").strip()
    away_name = ((away_c.get("team") or {}).get("displayName") or "").strip()
    try:
        home_score = int(home_c.get("score") or 0)
        away_score = int(away_c.get("score") or 0)
    except (TypeError, ValueError):
        return None
    start_raw = ev.get("date") or c.get("date")
    if not start_raw:
        return None
    start = datetime.fromisoformat(start_raw.replace("Z", "+00:00"))

    if home_score > away_score:
        winner = "home"
    elif away_score > home_score:
        winner = "away"
    else:
        winner = "void"  # tie (rare in major leagues)
    return home_name, away_name, start, winner


def resolve_sport(sport: str, days_back: int = 2) -> int:
    """Resolve completed events for `sport` over the last `days_back` days.

    Returns number of outcomes upserted.
    """
    n = 0
    today = datetime.now(timezone.utc)
    for offset in range(days_back + 1):
        day = today - timedelta(days=offset)
        events = _fetch_day(sport, day)
        for ev in events:
            parsed = _parse_event(sport, ev)
            if not parsed:
                continue
            home, away, start, winner = parsed
            if winner is None:
                continue

            venue_ev = matcher.VenueEvent(
                source="espn",
                source_id=str(ev.get("id") or ""),
                sport=sport,
                home=home,
                away=away,
                event_start=start,
                raw={"home_score": ev, "espn_event_id": ev.get("id")},
            )
            market_id = matcher.ensure_link(venue_ev)
            if not market_id:
                continue

            try:
                db.upsert_outcome(market_id, winner, source="espn")
                n += 1
                log.info(
                    "outcome %s %s vs %s -> %s", sport, away, home, winner.upper()
                )
            except Exception as e:
                log.warning("upsert_outcome failed %s: %s", market_id, e)
    return n


def resolve_all() -> dict[str, int]:
    out: dict[str, int] = {}
    for sport in config.sports_enabled:
        if sport not in ESPN_SPORT:
            continue
        try:
            out[sport] = resolve_sport(sport)
        except Exception as e:
            log.exception("resolve_sport %s failed: %s", sport, e)
            out[sport] = 0
    return out


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    p = argparse.ArgumentParser(prog="resolve")
    p.add_argument("--sport", help="One sport (NFL, MLB, ...). Default: all enabled.")
    p.add_argument("--days", type=int, default=2, help="Days back to scan (default 2)")
    args = p.parse_args(argv)

    if args.sport:
        n = resolve_sport(args.sport, days_back=args.days)
        log.info("%s: upserted %d outcomes", args.sport, n)
    else:
        totals = resolve_all()
        log.info("resolved: %s", totals)
    return 0


if __name__ == "__main__":
    sys.exit(main())
