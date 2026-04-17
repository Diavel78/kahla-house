"""Divergence engine: compares DK/FD consensus vs Polymarket sharp price.

Per matched market, emits a Signal row when:
  - Fresh DK + FD moneyline snapshots exist (within 10 min)
  - Fresh Polymarket mid exists
  - |edge| >= MIN_EDGE_PCT_GLOBAL
  - Poly liquidity at mid >= MIN_LIQUIDITY_GLOBAL
  - Event is >= MIN_MINUTES_TO_EVENT minutes out
  - No signal for this market in the last DEDUP_WINDOW_MIN

Edge is computed on the fade side: positive edge => public underprices the
side Polymarket thinks is more likely. fade_side is the side the sharp market
favors (the side we'd take).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from config import config
from signals.normalize import devig_two_way
from storage import supabase_client as db
from storage.models import Signal

log = logging.getLogger(__name__)


@dataclass
class BookProbs:
    home: float
    away: float


# Injection point for live Polymarket book depth — set by poly poller at runtime.
# Takes (market_id, price_target) and returns available USD depth at/near that price.
poly_book_depth: Callable[[str, float], float] | None = None


def _prob_for_side(snapshots: list[dict[str, Any]], side: str) -> float | None:
    for s in snapshots:
        if s["market_type"] == "moneyline" and s["side"] == side:
            return float(s["implied_prob"]) if s.get("implied_prob") is not None else None
    return None


def _book_probs(snapshots: list[dict[str, Any]]) -> BookProbs | None:
    home = _prob_for_side(snapshots, "home")
    away = _prob_for_side(snapshots, "away")
    if home is None or away is None:
        return None
    return BookProbs(home=home, away=away)


def _poly_home_prob(market_id: str) -> float | None:
    """Latest Polymarket implied prob of HOME side.

    Poly markets are stored by outcome string. Convention: 'HOME' for home team,
    'AWAY' for away team. If your poly scraper stores actual team names, map
    them to HOME/AWAY at insert time.
    """
    latest = db.latest_poly_tick(market_id, outcome="HOME")
    if not latest:
        return None
    return float(latest["price"])


def _minutes_to_event(event_start_iso: str) -> float:
    start = datetime.fromisoformat(event_start_iso.replace("Z", "+00:00"))
    delta = start - datetime.now(timezone.utc)
    return delta.total_seconds() / 60


def compute_divergence(market: dict[str, Any]) -> Signal | None:
    market_id = market["id"]

    if _minutes_to_event(market["event_start"]) < config.min_minutes_to_event:
        return None

    dk = db.latest_book_snapshots(market_id, "DK", within_minutes=10)
    fd = db.latest_book_snapshots(market_id, "FD", within_minutes=10)
    dk_probs = _book_probs(dk)
    fd_probs = _book_probs(fd)
    if not dk_probs or not fd_probs:
        return None

    dk_fair_home = devig_two_way(dk_probs.home, dk_probs.away)
    fd_fair_home = devig_two_way(fd_probs.home, fd_probs.away)
    public_home = (dk_fair_home + fd_fair_home) / 2

    sharp_home = _poly_home_prob(market_id)
    if sharp_home is None:
        return None

    edge_home = sharp_home - public_home
    edge_pct = abs(edge_home) * 100
    if edge_pct < config.min_edge_pct_global:
        return None

    fade_side = "home" if edge_home > 0 else "away"
    public_prob = public_home if fade_side == "home" else 1 - public_home
    sharp_prob = sharp_home if fade_side == "home" else 1 - sharp_home

    liquidity: float | None = None
    if poly_book_depth is not None:
        try:
            liquidity = poly_book_depth(market_id, sharp_prob)
        except Exception as e:
            log.warning("poly_book_depth failed for %s: %s", market_id, e)
        if liquidity is not None and liquidity < config.min_liquidity_global:
            return None

    if db.recent_signal_exists(market_id, window_minutes=config.dedup_window_min):
        return None

    return Signal(
        market_id=market_id,
        signal_type="divergence",
        fade_side=fade_side,
        public_prob=round(public_prob, 4),
        sharp_prob=round(sharp_prob, 4),
        edge_pct=round(edge_pct, 2),
        liquidity_usd=liquidity,
        notes={
            "dk_home_prob": dk_probs.home,
            "dk_away_prob": dk_probs.away,
            "fd_home_prob": fd_probs.home,
            "fd_away_prob": fd_probs.away,
            "public_home_fair": round(public_home, 4),
            "sharp_home": round(sharp_home, 4),
        },
    )


def scan_all() -> list[Signal]:
    """Run divergence check over every active market. Returns emitted signals."""
    out: list[Signal] = []
    for sport in config.sports_enabled:
        for market in db.list_active_markets(sport):
            try:
                sig = compute_divergence(market)
            except Exception as e:
                log.exception("divergence compute failed for %s: %s", market.get("id"), e)
                continue
            if sig:
                row = db.insert_signal(sig)
                sig_id = row.get("id")
                log.info(
                    "signal %s %s %s edge=%.2f%% (%s fade)",
                    sig_id,
                    market.get("sport"),
                    market.get("event_name"),
                    sig.edge_pct,
                    sig.fade_side,
                )
                out.append(sig)
    return out
