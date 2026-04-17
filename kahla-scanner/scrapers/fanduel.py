"""FanDuel scraper.

STATUS: scaffold. Milestone M6.

Approach mirrors DK:
  - Public JSON endpoint used by sportsbook.fanduel.com
  - Parse ML / spread / total per event
  - Match via matcher.ensure_link()
  - Append to book_snapshots
"""
from __future__ import annotations

import logging
from typing import Any, Iterable

import httpx

from config import config
from signals.normalize import american_to_prob
from storage.models import BookSnapshot

log = logging.getLogger(__name__)

# FanDuel uses competition-id queries. Fill in once probed.
SPORT_COMPETITIONS: dict[str, str] = {
    # 'NFL': '12282733',
}


def _client() -> httpx.Client:
    return httpx.Client(
        headers={"User-Agent": config.fd_user_agent, "Accept": "application/json"},
        timeout=15.0,
    )


def scrape(sport: str) -> None:
    log.warning("fanduel.scrape not yet implemented for %s (M6)", sport)


def _parse_offers(market_id: str, offers: Iterable[dict[str, Any]]) -> Iterable[BookSnapshot]:
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
        book="FD",
        market_type=market_type,
        side=side,
        line=line,
        price_american=price_american,
        implied_prob=american_to_prob(price_american),
    )
