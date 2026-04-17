"""Event matching across venues (Polymarket <-> DK/FD/Kalshi).

Strategy:
1. Use team_aliases table to canonicalize team names per sport.
2. Match on sport + event_start (±30 min) + canonical team set equality.
3. Persist match on markets.{dk_event_id,fd_event_id,kalshi_ticker} so fuzzy
   matching only runs once per event.
4. Unmatched events get logged to unmatched_markets for manual review.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from rapidfuzz import fuzz

from storage import supabase_client as db

log = logging.getLogger(__name__)

MATCH_WINDOW = timedelta(minutes=30)
# Below this fuzzy score, we refuse to auto-link; market goes to unmatched.
FUZZY_THRESHOLD = 88


@dataclass
class VenueEvent:
    source: str              # 'poly','dk','fd','kalshi'
    source_id: str
    sport: str
    home: str
    away: str
    event_start: datetime
    raw: dict[str, Any]


def _normalize(name: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace."""
    s = re.sub(r"[^\w\s]", " ", name.lower())
    return re.sub(r"\s+", " ", s).strip()


def canonicalize(name: str, aliases: dict[str, str]) -> str:
    """Apply alias map, fall back to normalized form."""
    norm = _normalize(name)
    return aliases.get(norm, norm)


def _teams_key(home: str, away: str, aliases: dict[str, str]) -> frozenset[str]:
    return frozenset({canonicalize(home, aliases), canonicalize(away, aliases)})


def _fuzzy_teams_match(
    a_home: str, a_away: str, b_home: str, b_away: str, aliases: dict[str, str]
) -> int:
    """Max score across the two possible pairings."""
    ah, aa = canonicalize(a_home, aliases), canonicalize(a_away, aliases)
    bh, ba = canonicalize(b_home, aliases), canonicalize(b_away, aliases)
    same = min(fuzz.ratio(ah, bh), fuzz.ratio(aa, ba))
    swap = min(fuzz.ratio(ah, ba), fuzz.ratio(aa, bh))
    return max(same, swap)


def link_venue_event(
    ev: VenueEvent, sport_markets: list[dict[str, Any]], aliases: dict[str, str]
) -> dict[str, Any] | None:
    """Find a markets row that matches `ev`. Returns the row or None."""
    ev_key = _teams_key(ev.home, ev.away, aliases)

    for row in sport_markets:
        row_start = _parse_ts(row["event_start"])
        if abs(row_start - ev.event_start) > MATCH_WINDOW:
            continue

        # Exact canonical match
        row_home, row_away = _split_event_name(row["event_name"])
        if row_home and row_away:
            if _teams_key(row_home, row_away, aliases) == ev_key:
                return row
            score = _fuzzy_teams_match(
                ev.home, ev.away, row_home, row_away, aliases
            )
            if score >= FUZZY_THRESHOLD:
                return row
    return None


def _parse_ts(ts: str | datetime) -> datetime:
    if isinstance(ts, datetime):
        return ts
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _split_event_name(name: str) -> tuple[str | None, str | None]:
    """event_name convention: 'Away @ Home' or 'A vs B'."""
    for sep in [" @ ", " vs ", " v. ", " vs. "]:
        if sep in name:
            parts = name.split(sep, 1)
            if len(parts) == 2:
                return parts[0].strip(), parts[1].strip()
    return None, None


def ensure_link(ev: VenueEvent) -> str | None:
    """Link a venue event to a markets row. Returns market_id or None.

    If no match, logs to unmatched_markets.
    """
    aliases = db.list_team_aliases(ev.sport)
    markets = db.list_active_markets(ev.sport)
    match = link_venue_event(ev, markets, aliases)
    if not match:
        db.log_unmatched(
            ev.source,
            ev.source_id,
            sport=ev.sport,
            event_name=f"{ev.away} @ {ev.home}",
            event_start=ev.event_start,
            payload=ev.raw,
        )
        log.info("unmatched %s event: %s @ %s", ev.source, ev.away, ev.home)
        return None

    patch: dict[str, Any] = {}
    if ev.source == "dk" and not match.get("dk_event_id"):
        patch["dk_event_id"] = ev.source_id
    elif ev.source == "fd" and not match.get("fd_event_id"):
        patch["fd_event_id"] = ev.source_id
    elif ev.source == "kalshi" and not match.get("kalshi_ticker"):
        patch["kalshi_ticker"] = ev.source_id
    if patch:
        db.update_market_link(match["id"], **patch)
    return match["id"]
