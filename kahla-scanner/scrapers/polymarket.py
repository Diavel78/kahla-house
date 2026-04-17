"""Polymarket US poller.

STATUS: scaffold. Milestone M1 is to port the existing Poly tracker code here.

Responsibilities:
  - Load active sports markets from Polymarket US API
  - Upsert into markets table (one row per Poly market)
  - Fetch recent trades for each market, append to poly_ticks
  - Fetch order book, expose depth(price) via a closure that divergence.py imports
  - Track last_seen_trade_id per market (in-memory for now)
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from config import config
from signals import divergence
from storage import supabase_client as db
from storage.models import Market, PolyTick

log = logging.getLogger(__name__)

# In-memory state, survives for the life of the process only.
_last_trade_id: dict[str, str] = {}
_book_depth_cache: dict[str, dict[float, float]] = {}


def _book_depth(market_id: str, price: float) -> float:
    """Look up USD depth at or near a target price. Returns 0 if unknown."""
    per_market = _book_depth_cache.get(market_id) or {}
    if not per_market:
        return 0.0
    # nearest price within 2 cents gets the depth
    best = min(per_market.keys(), key=lambda p: abs(p - price))
    if abs(best - price) > 0.02:
        return 0.0
    return per_market[best]


# Register with divergence engine
divergence.poly_book_depth = _book_depth


def poll_once() -> None:
    """TODO(M1): replace with real Polymarket US client calls.

    Pseudocode sketch — keep so the real port has a target shape:
      client = PolymarketUSClient(
          key_id=config.poly_api_key_id,
          secret=config.poly_api_secret,
          passphrase=config.poly_api_passphrase,
      )
      for sport in config.sports_enabled:
          for m in client.list_markets(sport=sport, status='active'):
              market = Market(
                  sport=sport,
                  event_name=m['title'],
                  event_start=parse(m['start_time']),
                  poly_market_id=m['id'],
              )
              row = db.upsert_market(market)
              market_id = row['id']

              trades = client.get_trades(m['id'],
                                         since_id=_last_trade_id.get(m['id']))
              ticks = [PolyTick(market_id=market_id,
                                outcome=t['outcome'],
                                price=t['price'],
                                size=t['size_usd'],
                                side=t.get('side'),
                                tick_ts=parse(t['ts']))
                        for t in trades]
              db.insert_poly_ticks(ticks)
              if trades:
                  _last_trade_id[m['id']] = trades[-1]['id']

              book = client.get_book(m['id'])
              _book_depth_cache[market_id] = _flatten_book(book)
    """
    log.warning("polymarket.poll_once not yet implemented (M1)")


def _flatten_book(book: Any) -> dict[float, float]:
    """Convert a Polymarket order book into {price -> cumulative USD depth}."""
    # Placeholder — actual shape depends on Polymarket US SDK response.
    return {}
