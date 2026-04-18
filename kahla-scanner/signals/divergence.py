"""Divergence engine — Phase 1: sharp consensus vs public lag.

Emits a Signal row when:
  1. event_start is in [now(), now() + LOOKAHEAD_HOURS]        (actionable)
  2. POLY and PIN both have fresh moneyline snapshots and agree within
     SHARP_CONSENSUS_AGREE_MAX_PCT                              (sharp confirm)
  3. At least 2 of {DK, FD, MGM} have fresh moneyline snapshots
                                                               (public sample)
  4. |sharp_consensus - public_avg| >= SHARP_CONSENSUS_EDGE_MIN_PCT
                                                               (meaningful edge)
  5. No prior signal emitted for this market in DEDUP_WINDOW_MIN

All book data comes from book_snapshots (written by scrapers/owls.py).
No dependency on the legacy poly_ticks table.

CLI:
    python -m signals.divergence                   # scan all active markets
    python -m signals.divergence --dry-run         # print what would fire,
                                                   # do not insert
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from config import config
from signals.normalize import devig_two_way
from storage import supabase_client as db
from storage.models import Signal

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Thresholds (env-overridable)
# ---------------------------------------------------------------------------

def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        return float(v) if v else default
    except (TypeError, ValueError):
        return default


SHARP_BOOKS: tuple[str, ...] = ("POLY", "PIN")
PUBLIC_BOOKS: tuple[str, ...] = ("DK", "FD", "MGM")

# POLY and PIN must agree within this many percentage points to count as
# "sharp consensus". Prevents firing on one-sided Poly liquidity spikes.
AGREE_MAX_PCT = _env_float("SHARP_CONSENSUS_AGREE_MAX_PCT", 1.5)

# Minimum edge (sharp - public) in percentage points to fire a signal.
EDGE_MIN_PCT = _env_float("SHARP_CONSENSUS_EDGE_MIN_PCT", 3.0)

# Only fire on games starting within this many hours.
LOOKAHEAD_HOURS = _env_float("SHARP_CONSENSUS_LOOKAHEAD_HOURS", 2.0)

# Snapshot freshness window. 10 min covers two 5-min cron cycles of slack.
FRESHNESS_MIN = int(_env_float("SHARP_CONSENSUS_FRESHNESS_MIN", 10))

# Dedup window. 30 min is conservative for 5-min cron (6 cycles of silence
# before the same signal can re-fire).
DEDUP_WINDOW_MIN = int(_env_float("SHARP_CONSENSUS_DEDUP_MIN", 30))

# Need at least this many public books to form a public baseline.
MIN_PUBLIC_BOOKS = 2


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@dataclass
class BookHomeProb:
    book: str
    prob: float             # devig'd home-win probability


def _book_home_prob(snaps: list[dict[str, Any]]) -> float | None:
    home = away = None
    for s in snaps:
        if s.get("market_type") != "moneyline":
            continue
        p = s.get("implied_prob")
        if p is None:
            continue
        if s.get("side") == "home":
            home = float(p)
        elif s.get("side") == "away":
            away = float(p)
    if home is None or away is None or (home + away) <= 0:
        return None
    return devig_two_way(home, away)


def _minutes_to_event(event_start_iso: str) -> float:
    start = datetime.fromisoformat(event_start_iso.replace("Z", "+00:00"))
    delta = start - datetime.now(timezone.utc)
    return delta.total_seconds() / 60


def _fetch_book_probs(market_id: str, books: tuple[str, ...]) -> dict[str, float]:
    """For each requested book, fetch latest ML snapshot pair and devig.
    Returns {book: home_prob} for books where we have fresh data."""
    out: dict[str, float] = {}
    for b in books:
        snaps = db.latest_book_snapshots(market_id, b, within_minutes=FRESHNESS_MIN)
        p = _book_home_prob(snaps)
        if p is not None:
            out[b] = p
    return out


# ---------------------------------------------------------------------------
# Core detector
# ---------------------------------------------------------------------------

def compute_signal(market: dict[str, Any]) -> Signal | None:
    """Return a Signal if this market passes every filter, else None."""
    market_id = market["id"]

    # (1) Timing — only actionable games
    mins = _minutes_to_event(market["event_start"])
    if mins < 0:
        return None  # already started
    if mins > LOOKAHEAD_HOURS * 60:
        return None  # too early

    # (2, 3) Gather sharp + public probabilities
    sharp = _fetch_book_probs(market_id, SHARP_BOOKS)
    if len(sharp) < len(SHARP_BOOKS):
        return None  # need both POLY and PIN for consensus
    public = _fetch_book_probs(market_id, PUBLIC_BOOKS)
    if len(public) < MIN_PUBLIC_BOOKS:
        return None

    # Sharp consensus only valid if POLY and PIN agree
    poly_home = sharp["POLY"]
    pin_home = sharp["PIN"]
    if abs(poly_home - pin_home) * 100 > AGREE_MAX_PCT:
        return None
    sharp_home = (poly_home + pin_home) / 2

    # Public average across the books that responded
    public_home = sum(public.values()) / len(public)

    # (4) Edge check
    edge_home_pp = (sharp_home - public_home) * 100  # in percentage points
    if abs(edge_home_pp) < EDGE_MIN_PCT:
        return None

    # Fade side = whichever side the sharps think is more likely (we bet that side
    # at the PUBLIC book). "public underpriced this side."
    fade_side = "home" if edge_home_pp > 0 else "away"
    sharp_prob = sharp_home if fade_side == "home" else (1 - sharp_home)
    public_prob = public_home if fade_side == "home" else (1 - public_home)

    # (5) Dedup
    if db.recent_signal_exists(market_id, window_minutes=DEDUP_WINDOW_MIN):
        return None

    return Signal(
        market_id=market_id,
        signal_type="sharp_consensus",
        fade_side=fade_side,
        public_prob=round(public_prob, 4),
        sharp_prob=round(sharp_prob, 4),
        edge_pct=round(abs(edge_home_pp), 2),
        liquidity_usd=None,  # Owls doesn't give us book depth; skip for Phase 1
        notes={
            "minutes_to_event": round(mins, 1),
            "sharp_home": round(sharp_home, 4),
            "sharp_detail": {b: round(v, 4) for b, v in sharp.items()},
            "public_home": round(public_home, 4),
            "public_detail": {b: round(v, 4) for b, v in public.items()},
            "agree_pp": round(abs(poly_home - pin_home) * 100, 2),
        },
    )


# ---------------------------------------------------------------------------
# Scan + CLI
# ---------------------------------------------------------------------------

def scan_all(*, dry_run: bool = False) -> list[Signal]:
    """Run the detector over every active market. Returns emitted signals."""
    emitted: list[Signal] = []
    considered = 0
    for sport in config.sports_enabled:
        markets = db.list_active_markets(sport)
        for market in markets:
            considered += 1
            try:
                sig = compute_signal(market)
            except Exception as e:
                log.exception("compute_signal failed for %s: %s", market.get("id"), e)
                continue
            if not sig:
                continue
            if dry_run:
                log.info(
                    "[DRY-RUN] %s %s %s edge=%.2fpp fade=%s sharp=%.3f public=%.3f",
                    market.get("sport"), market.get("event_name"),
                    sig.signal_type, sig.edge_pct, sig.fade_side,
                    sig.sharp_prob, sig.public_prob,
                )
            else:
                row = db.insert_signal(sig)
                sid = row.get("id")
                log.info(
                    "signal %s %s %s edge=%.2fpp fade=%s sharp=%.3f public=%.3f",
                    sid, market.get("sport"), market.get("event_name"),
                    sig.edge_pct, sig.fade_side, sig.sharp_prob, sig.public_prob,
                )
            emitted.append(sig)
    log.info("scan complete: %d markets considered, %d signals %s",
             considered, len(emitted), "would-fire" if dry_run else "emitted")
    return emitted


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(prog="divergence")
    p.add_argument("--dry-run", action="store_true",
                   help="Print would-fire signals, don't insert into Supabase.")
    args = p.parse_args(argv)
    scan_all(dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
