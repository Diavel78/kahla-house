"""Shared helpers for the Phase 4 paper-bet pickers.

Used by:
  - scripts/paper_bets_picker.py — early/late EV pickers
  - scripts/sharp_alerts.py      — steam logger (every Telegram steam
                                    alert also logs a paper bet)

Helpers:
  pin_devig_fair_prob — devig PIN's two-way market for our side
  find_best_entry     — best non-PIN price at a target line
  combined_score      — pick-ranking formula (sharp_score + edge_pp)
  insert_paper_bet    — write to paper_bets with dedup check
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from _lib.normalize import american_to_prob, devig_two_way

log = logging.getLogger(__name__)

# ─────────────────────── Pick filters ───────────────────────
# Per-market gates. Tightened from the early-days flat constants
# (SHARP_SCORE_MIN=4 / EDGE_PP_MIN=0.5) after ~250 graded picks
# showed:
#   • TOT picks losing at ~2σ below break-even (38% over 97 picks)
#   • The 0.5pp gate is below PIN's own bid/ask margin band, so a
#     0.5pp "edge" is statistically inside PIN's noise floor
#   • SPR/TOT carry steeper retail vig (-110/-110 = ~4.5% per side),
#     needing a wider edge to overcome
# Higher bars across the board, with TOT pushed hardest. Will produce
# fewer picks per cycle — that's the point. Sharp Bot needed to stop
# picking marginal noise.
SHARP_SCORE_MIN_BY_MARKET = {"moneyline": 4, "spread": 4, "total": 5}
EDGE_PP_MIN_BY_MARKET     = {"moneyline": 1.0, "spread": 1.5, "total": 2.0}

# Back-compat aliases for any reader still importing the flat names.
# Use the helpers below in new code.
SHARP_SCORE_MIN = 4
EDGE_PP_MIN     = 1.0


def sharp_min_for(market_type: str) -> int:
    return SHARP_SCORE_MIN_BY_MARKET.get(market_type, SHARP_SCORE_MIN)


def edge_min_for(market_type: str) -> float:
    return EDGE_PP_MIN_BY_MARKET.get(market_type, EDGE_PP_MIN)


# ─────────────────────── Opener freshness ───────────────────────
# Opener = earliest PIN snap WITHIN this age range. Was previously
# "earliest ever captured" which for a 36h-old market meant a snap
# from 2 days ago — making sharp_score include 2 days of news flow
# (weather, lineup, late scratches), not sharp opinion.
#   • MIN 1h: don't let an opener that's 5 minutes old produce a
#     meaningless "huge move" reading
#   • MAX 12h: stay inside the recent-action window where retail
#     hasn't fully absorbed PIN's pricing
# If no PIN snap exists in this range for a (market, market_type, side),
# the helper returns no opener → picker skips that market_type. Fine;
# very-new markets need data accumulation before they're tradeable.
OPENER_MIN_AGE_HOURS = 1
OPENER_MAX_AGE_HOURS = 12


# ─────────────────────── Score weighting ───────────────────────
# Old: 0.6 sharp / 0.4 edge with edge capped at 10pp. A 0.5pp edge
# contributed 0.02 to combined_score while sharp_score 5 contributed
# 0.30 — i.e. picks were ranked by PIN-movement magnitude, with edge
# as a tiebreaker. That's backwards: edge_pp IS the +EV signal,
# sharp_score is the noise filter.
# Flipped to 0.25 / 0.75 with edge cap dropped to 5pp so a 2pp edge
# contributes 0.30, comparable to sharp_score 6 (0.15). Edge dominates
# pick ranking; sharp_score still gates qualification.
SHARP_WEIGHT = 0.25
EDGE_WEIGHT  = 0.75
EDGE_CAP_PP  = 5.0

# Max picks per picker run (per bot). Steam isn't capped — fires when
# institutional flow synchronizes, which is rare enough to be self-
# limiting in practice (~0-3/day).
MAX_PICKS_PER_RUN = 5

# Sports the paper-bet system refuses to pick. UFC paper bets sit
# pending forever because paper_bets_resolver.py has no MMA scoreboard
# endpoint to grade against (ESPN's MMA API is per-event, not a
# consolidated scoreboard). Blocked at pick time so the pending queue
# doesn't fill with rows that will never settle. Also clipped by the
# steam logger so a UFC steam alert doesn't slip through.
BLOCKED_SPORTS = {"UFC"}

# Books we'll book a bet at. PIN excluded (it's the benchmark, not the
# entry — we're trying to beat the sharp line at retail). Others are the
# 14-book allowlist minus PIN.
ENTRY_BOOKS = {
    "DK", "FD", "MGM", "CAE", "HR", "BET365", "BR",
    "BOL", "LV", "BVD", "ESPN", "FAN", "MB",
}


# ──────────────────────────── Devig ────────────────────────────

def pin_devig_fair_prob(market_id: str, market_type: str, side: str,
                        pin_current: dict) -> float | None:
    """Devigged fair probability for `side` from PIN's latest two-way
    market. Returns None if either side missing, or for SPR/TOT if the
    home/away (or over/under) lines don't match.

    For SPR a matched pair has equal magnitude / opposite sign points
    (e.g. -1.5 / +1.5). For TOT both sides should have the same total
    (e.g. 8.5 / 8.5)."""
    if market_type == "moneyline":
        h = pin_current.get((market_id, "moneyline", "home"))
        a = pin_current.get((market_id, "moneyline", "away"))
        if not (h and a):
            return None
        try:
            ph = american_to_prob(int(h["price_american"]))
            pa = american_to_prob(int(a["price_american"]))
        except (ValueError, TypeError):
            return None
        fair_h = devig_two_way(ph, pa)
        return fair_h if side == "home" else 1.0 - fair_h

    if market_type == "spread":
        h = pin_current.get((market_id, "spread", "home"))
        a = pin_current.get((market_id, "spread", "away"))
        if not (h and a):
            return None
        h_line, a_line = h.get("line"), a.get("line")
        if h_line is None or a_line is None:
            return None
        # Matched pair = equal magnitude, opposite sign (within float tol).
        if abs(h_line + a_line) > 0.001:
            return None
        try:
            ph = american_to_prob(int(h["price_american"]))
            pa = american_to_prob(int(a["price_american"]))
        except (ValueError, TypeError):
            return None
        fair_h = devig_two_way(ph, pa)
        return fair_h if side == "home" else 1.0 - fair_h

    if market_type == "total":
        o = pin_current.get((market_id, "total", "over"))
        u = pin_current.get((market_id, "total", "under"))
        if not (o and u):
            return None
        if o.get("line") != u.get("line"):
            return None
        try:
            po = american_to_prob(int(o["price_american"]))
            pu = american_to_prob(int(u["price_american"]))
        except (ValueError, TypeError):
            return None
        fair_o = devig_two_way(po, pu)
        return fair_o if side == "over" else 1.0 - fair_o

    return None


# ──────────────────────────── Entry ────────────────────────────

def find_best_entry(market_id: str, market_type: str, side: str,
                    target_line: float | None,
                    latest_by_key: dict) -> dict | None:
    """Best non-PIN entry price for `side`. For SPR/TOT, only books
    quoting the same line as PIN qualify (so the devigged fair_prob
    actually applies to the entry).

    `latest_by_key`: {(market_id, book, market_type, side): snapshot}
    — built once per run by the caller from a single Supabase query.

    Returns the best snapshot dict (with `book`, `price_american`,
    `line`) or None.
    """
    best = None
    for book in ENTRY_BOOKS:
        snap = latest_by_key.get((market_id, book, market_type, side))
        if not snap:
            continue
        # Line gate for SPR/TOT.
        if market_type != "moneyline" and target_line is not None:
            if snap.get("line") != target_line:
                continue
        price = snap.get("price_american")
        if price is None:
            continue
        # "Best" American price = highest signed value (+150 beats -110
        # beats -130). American odds are monotonic in payout.
        if best is None or price > best["price_american"]:
            best = {
                "book": book,
                "price_american": int(price),
                "line": snap.get("line"),
            }
    return best


# ──────────────────────────── Score ────────────────────────────

def combined_score(sharp_score: int, edge_pp: float) -> float:
    """Pick-ranking formula. Edge is the primary signal (0.75 weight),
    sharp_score is the noise filter (0.25 weight). See SHARP_WEIGHT /
    EDGE_WEIGHT comment block for why this is flipped from the old
    sharp-primary formula."""
    return (
        SHARP_WEIGHT * (max(0, sharp_score) / 10.0)
        + EDGE_WEIGHT * min(max(0.0, edge_pp) / EDGE_CAP_PP, 1.0)
    )


# ──────────────────────────── Insert ────────────────────────────

def already_picked(sb, bot: str, market_id: str,
                   lookback_hours: int = 168) -> bool:
    """Has `bot` already logged a pick for this market in the lookback
    window? 7 days is longer than any game's pre-game lifecycle, so this
    safely catches 'late picker ran 30 min ago and already picked this'
    AND 'early picker ran this morning' alike."""
    cutoff = (datetime.now(timezone.utc)
              - timedelta(hours=lookback_hours)).isoformat()
    try:
        rows = (sb.table("paper_bets")
                .select("id")
                .eq("bot", bot)
                .eq("market_id", market_id)
                .gte("picked_at", cutoff)
                .limit(1)
                .execute().data) or []
    except Exception as e:
        log.warning("paper_bets dedup check failed (will skip pick): %s", e)
        return True  # fail closed — better to miss a pick than double-log
    return bool(rows)


def insert_paper_bet(sb, *, bot: str, market: dict, market_type: str,
                     side: str, entry_book: str, entry_price: int,
                     entry_line: float | None,
                     fair_prob: float | None, edge_pp: float | None,
                     sharp_score: int | None,
                     signal_blob: dict | None) -> bool:
    """Write a paper_bets row. Returns True on success."""
    row = {
        "bot":         bot,
        "market_id":   market["id"],
        "sport":       market.get("sport") or "",
        "event_name":  market.get("event_name") or "",
        "event_start": market.get("event_start"),
        "market_type": market_type,
        "side":        side,
        "entry_book":  entry_book,
        "entry_price": int(entry_price),
        "entry_line":  entry_line,
        "fair_prob":   fair_prob,
        "edge_pp":     edge_pp,
        "sharp_score": sharp_score,
        "signal_blob": signal_blob or {},
    }
    try:
        sb.table("paper_bets").insert(row).execute()
        return True
    except Exception as e:
        log.warning("paper_bets insert failed (%s %s/%s): %s",
                    bot, market.get("event_name"), market_type, e)
        return False


# ────────────────────────── Snapshot loaders ──────────────────────────

def fetch_latest_snapshots(sb, market_ids: Iterable[str],
                           lookback_hours: int = 24
                           ) -> dict[tuple[str, str, str, str], dict]:
    """Latest snapshot per (market_id, book, market_type, side) within
    the lookback. Sharp books (PIN) sit on a quote for hours, retail
    books re-price often — so 24h is a safe ceiling that still catches
    a stale-but-current PIN line."""
    out: dict[tuple[str, str, str, str], dict] = {}
    market_ids = list(market_ids)
    if not market_ids:
        return out
    cutoff = (datetime.now(timezone.utc)
              - timedelta(hours=lookback_hours)).isoformat()
    CHUNK = 100
    for i in range(0, len(market_ids), CHUNK):
        chunk = market_ids[i:i + CHUNK]
        try:
            rows = (sb.table("book_snapshots")
                    .select("market_id,book,market_type,side,"
                            "price_american,line,captured_at")
                    .in_("market_id", chunk)
                    .gte("captured_at", cutoff)
                    .order("captured_at", desc=True)
                    .limit(20000)
                    .execute().data) or []
        except Exception as e:
            log.warning("snapshot chunk fetch failed: %s", e)
            continue
        for r in rows:
            key = (r["market_id"], r["book"],
                   r["market_type"], r["side"])
            if key not in out:
                out[key] = r
    return out


def fetch_pin_openers(sb, market_ids: Iterable[str]) -> dict:
    """Earliest PIN snapshot per (market_id, market_type, side) WITHIN
    a recent age window — `OPENER_MIN_AGE_HOURS` to `OPENER_MAX_AGE_HOURS`
    old. Used to be all-time earliest; that meant a 36h-old market's
    "opener" was 2 days of news ago. Now the opener is a fresh-ish
    baseline so sharp_score reflects recent decision-making by PIN.

    Markets with no PIN snap in the window get no opener → picker
    skips that (market, market_type, side). Acceptable: new markets
    need an hour or two of data before they're tradeable, and very
    old markets without recent PIN activity probably aren't worth
    chasing anyway."""
    market_ids = list(market_ids)
    if not market_ids:
        return {}
    now = datetime.now(timezone.utc)
    lo_iso = (now - timedelta(hours=OPENER_MAX_AGE_HOURS)).isoformat()
    hi_iso = (now - timedelta(hours=OPENER_MIN_AGE_HOURS)).isoformat()
    out: dict = {}
    CHUNK = 100
    for i in range(0, len(market_ids), CHUNK):
        chunk = market_ids[i:i + CHUNK]
        try:
            rows = (sb.table("book_snapshots")
                    .select("market_id,market_type,side,"
                            "price_american,line,captured_at")
                    .in_("market_id", chunk)
                    .eq("book", "PIN")
                    .gte("captured_at", lo_iso)
                    .lte("captured_at", hi_iso)
                    .order("captured_at")
                    .limit(20000)
                    .execute().data) or []
        except Exception as e:
            log.warning("PIN openers chunk fetch failed: %s", e)
            continue
        for r in rows:
            key = (r["market_id"], r["market_type"], r["side"])
            if key not in out:
                out[key] = r
    return out
