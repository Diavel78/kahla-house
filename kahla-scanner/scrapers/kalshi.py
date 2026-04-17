"""Kalshi integration — v2 (M10).

Kalshi publishes a REST API. Flow will mirror polymarket.py:
  - Fetch active event tickers for supported sports
  - Store implied YES/NO prices as ticks (treat Kalshi as a second sharp reference)
  - Match tickers to markets rows via matcher.ensure_link()
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def poll_once() -> None:
    log.debug("kalshi.poll_once not yet implemented (M10)")
