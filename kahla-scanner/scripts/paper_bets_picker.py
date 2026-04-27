"""Phase 4 Sharp Bot — Early + Late EV pickers.

Two bots, one script:
  --bot early   Runs 1×/day (8am ET via cron-job.org → paper-bets-early.yml).
                Candidates: games where event_start is 10–36h from now.
                Thesis: soft opener, sharps positioned, retail will push
                line further by close.
  --bot late    Runs every 30 min (appended step in scanner-poll.yml).
                Candidates: games where event_start is 15min–2h from now.
                Thesis: closing-line sharp money, near-CLV proxy.

Per market we:
  1. Determine the sharp side (PIN movement opener → current) using the
     same logic as the on-card chip + Telegram alerts (_lib/sharp).
  2. Devig PIN's two-way market for that side → fair_prob.
  3. Find the best non-PIN entry price at PIN's current line (line gate
     for SPR/TOT — devigged fair only applies if entry book is at the
     same point).
  4. Compute edge_pp = (fair_prob − implied_at_entry) × 100.
  5. Filter: sharp_score ≥ 4 AND edge_pp ≥ 1.
  6. Score = 0.6 × sharp/10 + 0.4 × min(edge_pp/10, 1).
  7. Sort, dedup-by-market, take top 5, insert into paper_bets.

Steam bot is logged from scripts/sharp_alerts.py — it's a trigger event,
not a snapshot evaluation, so it doesn't fit this picker.
"""
from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from _lib import paper_bets as pb
from _lib import sharp
from _lib.normalize import american_to_prob
from storage import supabase_client as db

log = logging.getLogger(__name__)


# ─────────────────────────── Bot windows ──────────────────────────
# Hours-from-now bounds. Markets whose event_start falls inside the
# bot's window become candidates; everything else is filtered out.
WINDOWS = {
    # 10h floor: sharp money has had the morning to react to opener
    # but retail hasn't pushed the line yet. 36h ceiling: tomorrow's
    # late-night games are still in scope when picker runs at 8am ET.
    "early": (timedelta(hours=10),    timedelta(hours=36)),
    # 15min floor: ignore games already at/inside live-buffer window.
    # 2h ceiling: "late steam" is the last 1-2 hours pre-game where
    # syndicate flow + lineup news drive the closing line.
    "late":  (timedelta(minutes=15),  timedelta(hours=2)),
}


@dataclass
class Candidate:
    market:        dict
    market_type:   str
    side:          str
    sharp_score:   int
    fair_prob:     float
    edge_pp:       float
    entry_book:    str
    entry_price:   int
    entry_line:    float | None
    combined:      float
    opener_snap:   dict
    current_snap:  dict


# ─────────────────────────── Markets in window ──────────────────────────

def _fetch_markets_in_window(sb, low: timedelta, high: timedelta) -> list[dict]:
    now = datetime.now(timezone.utc)
    lo_iso = (now + low).isoformat()
    hi_iso = (now + high).isoformat()
    try:
        return (sb.table("markets")
                .select("id,sport,event_name,event_start")
                .eq("status", "active")
                .gte("event_start", lo_iso)
                .lte("event_start", hi_iso)
                .order("event_start")
                .limit(500)
                .execute().data) or []
    except Exception as e:
        log.error("markets fetch failed: %s", e)
        return []


# ─────────────────────────── Build candidates ──────────────────────────

_SHARP_HELPERS = {
    "moneyline": sharp.sharp_for_ml,
    "spread":    sharp.sharp_for_spread,
    "total":     sharp.sharp_for_total,
}


def _build_candidates(sb, markets: list[dict]) -> list[Candidate]:
    if not markets:
        return []
    market_by_id = {m["id"]: m for m in markets}
    market_ids = list(market_by_id.keys())

    # Latest snapshot per (market, book, market_type, side) — one query
    # for ALL books, since we need both PIN (for sharp side / devig) and
    # all non-PIN books (for entry pricing).
    latest = pb.fetch_latest_snapshots(sb, market_ids)
    pin_current = {(mid, mt, sd): snap
                   for (mid, bk, mt, sd), snap in latest.items()
                   if bk == "PIN"}
    openers = pb.fetch_pin_openers(sb, market_ids)

    candidates: list[Candidate] = []
    for mid, market in market_by_id.items():
        for market_type, helper in _SHARP_HELPERS.items():
            r = helper(mid, openers, pin_current)
            if r is None:
                continue
            side, sharp_score, opener_snap, current_snap = r
            if sharp_score < pb.SHARP_SCORE_MIN:
                continue

            fair_prob = pb.pin_devig_fair_prob(
                mid, market_type, side, pin_current)
            if fair_prob is None:
                continue

            target_line = (current_snap.get("line")
                           if market_type != "moneyline" else None)
            entry = pb.find_best_entry(
                mid, market_type, side, target_line, latest)
            if entry is None:
                continue

            try:
                implied = american_to_prob(int(entry["price_american"]))
            except (ValueError, TypeError):
                continue
            edge_pp = (fair_prob - implied) * 100.0
            if edge_pp < pb.EDGE_PP_MIN:
                continue

            candidates.append(Candidate(
                market       = market,
                market_type  = market_type,
                side         = side,
                sharp_score  = sharp_score,
                fair_prob    = fair_prob,
                edge_pp      = edge_pp,
                entry_book   = entry["book"],
                entry_price  = entry["price_american"],
                entry_line   = entry.get("line"),
                combined     = pb.combined_score(sharp_score, edge_pp),
                opener_snap  = opener_snap,
                current_snap = current_snap,
            ))
    return candidates


# ─────────────────────────── Pick + insert ──────────────────────────

def _pick_and_insert(sb, bot: str, candidates: list[Candidate]) -> int:
    """Sort by combined score, dedup-by-market (one bet per game per
    bot), skip already-picked, insert up to MAX_PICKS_PER_RUN rows."""
    candidates.sort(key=lambda c: c.combined, reverse=True)
    inserted = 0
    seen_markets: set[str] = set()
    for c in candidates:
        if inserted >= pb.MAX_PICKS_PER_RUN:
            break
        mid = c.market["id"]
        if mid in seen_markets:
            continue
        if pb.already_picked(sb, bot, mid):
            seen_markets.add(mid)  # don't reconsider this market
            continue
        ok = pb.insert_paper_bet(
            sb,
            bot         = bot,
            market      = c.market,
            market_type = c.market_type,
            side        = c.side,
            entry_book  = c.entry_book,
            entry_price = c.entry_price,
            entry_line  = c.entry_line,
            fair_prob   = c.fair_prob,
            edge_pp     = c.edge_pp,
            sharp_score = c.sharp_score,
            signal_blob = {
                "combined":      round(c.combined, 4),
                "opener_price":  c.opener_snap.get("price_american"),
                "current_price": c.current_snap.get("price_american"),
                "opener_line":   c.opener_snap.get("line"),
                "current_line":  c.current_snap.get("line"),
            },
        )
        if ok:
            inserted += 1
            seen_markets.add(mid)
            log.info(
                "PICK %s %s: %s %s/%s @ %s %s%s edge=%.2fpp sharp=%d combined=%.3f",
                bot, c.market.get("sport"),
                c.market.get("event_name"), c.market_type, c.side,
                c.entry_book, c.entry_price,
                f" {c.entry_line:+g}" if c.entry_line is not None else "",
                c.edge_pp, c.sharp_score, c.combined,
            )
    return inserted


# ─────────────────────────── CLI ──────────────────────────

def run(bot: str) -> int:
    if bot not in WINDOWS:
        raise SystemExit(f"unknown bot '{bot}' — must be early|late")
    low, high = WINDOWS[bot]
    sb = db.client()
    markets = _fetch_markets_in_window(sb, low, high)
    log.info("bot=%s window=%s..%s candidates_pool=%d",
             bot, low, high, len(markets))
    if not markets:
        return 0
    candidates = _build_candidates(sb, markets)
    log.info("bot=%s candidates_qualified=%d", bot, len(candidates))
    inserted = _pick_and_insert(sb, bot, candidates)
    log.info("bot=%s inserted=%d", bot, inserted)
    return inserted


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(prog="paper_bets_picker")
    p.add_argument("--bot", required=True, choices=["early", "late"])
    args = p.parse_args(argv)
    run(args.bot)
    return 0


if __name__ == "__main__":
    sys.exit(main())
