"""DraftKings scraper.

STATUS: scaffold. Milestone M2 wires in the real endpoint + NFL parser.

Approach:
  - Public JSON endpoint (same one the DK frontend calls).
  - One request per sport event-group ID, returns all offers.
  - Parse moneyline / spread / total for each event.
  - Match to a markets row via signals.matcher.ensure_link().
  - Append rows to book_snapshots.

Rate limiting:
  - 3-minute cadence is conservative; DK tolerates this easily from a single IP.
  - Rotate UA. Keep one httpx.Client across poll runs for connection reuse.
  - On any non-2xx, skip this tick and log. Do not retry hard.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Iterable

import httpx

from config import config
from signals import matcher
from signals.normalize import american_to_prob
from storage import supabase_client as db
from storage.models import BookSnapshot

log = logging.getLogger(__name__)

# Event-group IDs per sport for DK's sportsbook.us-east-1.draftkings.com API.
# Fill in as you test each sport.
SPORT_EVENT_GROUPS: dict[str, int] = {
    "NFL": 88808,
    # 'CBB': 92483,
    # 'MLB': 84240,
    # 'NBA': 42648,
    # 'NHL': 42133,
    # 'UFC': 9034,
}

BASE_URL = (
    "https://sportsbook-nash.draftkings.com/api/sportscontent/dkusnj/v1"
    "/leagues/{group_id}"
)


def _client() -> httpx.Client:
    return httpx.Client(
        headers={"User-Agent": config.dk_user_agent, "Accept": "application/json"},
        timeout=15.0,
    )


def fetch_group(sport: str) -> dict[str, Any] | None:
    group_id = SPORT_EVENT_GROUPS.get(sport)
    if not group_id:
        return None
    url = BASE_URL.format(group_id=group_id)
    with _client() as http:
        resp = http.get(url)
        if resp.status_code != 200:
            log.warning("DK %s fetch %s: %s", sport, resp.status_code, resp.text[:200])
            return None
        return resp.json()


def scrape(sport: str) -> None:
    """TODO(M2): parse real response into VenueEvents + BookSnapshots.

    Sketch of parse flow once the response shape is known:
      payload = fetch_group(sport)
      for event in payload['events']:
          ev = matcher.VenueEvent(
              source='dk',
              source_id=str(event['id']),
              sport=sport,
              home=event['homeTeam']['name'],
              away=event['awayTeam']['name'],
              event_start=parse(event['startDate']),
              raw=event,
          )
          market_id = matcher.ensure_link(ev)
          if not market_id:
              continue

          snaps = list(_parse_offers(market_id, event['offers']))
          db.insert_book_snapshots(snaps)
    """
    log.warning("draftkings.scrape not yet implemented for %s (M2)", sport)


def _parse_offers(market_id: str, offers: Iterable[dict[str, Any]]) -> Iterable[BookSnapshot]:
    """Convert DK offer objects into BookSnapshot rows. Placeholder."""
    return []


def _snap(
    market_id: str,
    market_type: str,
    side: str,
    price_american: int,
    line: float | None = None,
) -> BookSnapshot:
    return BookSnapshot(
        market_id=market_id,
        book="DK",
        market_type=market_type,
        side=side,
        line=line,
        price_american=price_american,
        implied_prob=american_to_prob(price_american),
    )
