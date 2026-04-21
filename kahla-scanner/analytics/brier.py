"""Brier-score calibration (M0).

For every settled market, pulls each book's HOME-side implied probability at
checkpoint offsets (T-24h, T-6h, T-1h, T-0) relative to event_start, then
computes Brier score against the actual outcome (home win = 1.0, else 0.0).

Lowest Brier score per checkpoint = sharpest book at that horizon.

All book data (including Polymarket) comes from the book_snapshots table,
populated by scrapers/owls.py. Books scored:
    POLY, PIN, CIR, DK, FD, MGM, CAE, HR, NVG

Run:
    python -m analytics.brier --sport NFL --days 30
    python -m analytics.brier --days 30          # all sports

Output: one table row per book summarising n_games and mean Brier per
checkpoint. Plus a CSV dump per market if --csv out.csv given.
"""
from __future__ import annotations

import argparse
import csv
import logging
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from signals.normalize import devig_two_way
from storage import supabase_client as db

log = logging.getLogger(__name__)

# Checkpoint offsets BEFORE event_start. Order matters for output.
CHECKPOINTS_HOURS: list[float] = [24, 6, 1, 0]

# Books scored, in display order. Must match codes written by scrapers/owls.py.
BOOKS: list[str] = ["POLY", "PIN", "CIR", "DK", "FD", "MGM", "CAE", "HR", "NVG"]

# Minimum sample size for a book to qualify as "best at this checkpoint".
MIN_N_WINNER = 5


@dataclass
class RowResult:
    market_id: str
    sport: str
    event_name: str
    event_start: datetime
    home_won: int                 # 1 if home won, 0 if away, -1 for void
    # probs[book][hours_before] = prob | None
    probs: dict[str, dict[float, float | None]]


def _checkpoint_ts(event_start: datetime, hours_before: float) -> datetime:
    return event_start - timedelta(hours=hours_before)


def _book_home_probs_nearest(
    market_id: str, target: datetime, lookback_days: int = 14
) -> dict[str, float]:
    """Return {book_code: devig'd home prob} using the NEWEST moneyline
    snapshot at or before `target` per book. A book's posted line is live
    until they change it, so an unchanged sharp-book line from hours ago
    IS their line at the checkpoint. A narrow ±30-min window biased the
    comparison toward retail books (MGM/CAE) that re-price constantly
    vs sharp books (PIN/CIR) that rarely do."""
    lower = (target - timedelta(days=lookback_days)).isoformat()
    upper = target.isoformat()
    res = (
        db.client()
        .table("book_snapshots")
        .select("book,market_type,side,implied_prob,captured_at")
        .eq("market_id", market_id)
        .eq("market_type", "moneyline")
        .gte("captured_at", lower)
        .lte("captured_at", upper)
        .order("captured_at", desc=True)
        .limit(2000)
        .execute()
    )
    rows = res.data or []

    # Keep newest snapshot per (book, side).
    newest: dict[tuple[str, str], dict[str, Any]] = {}
    for r in rows:
        side = r.get("side")
        if side not in ("home", "away"):
            continue
        newest.setdefault((r["book"], side), r)

    book_sides: dict[str, dict[str, float]] = {}
    for (book, side), r in newest.items():
        p = r.get("implied_prob")
        if p is None:
            continue
        book_sides.setdefault(book, {})[side] = float(p)

    out: dict[str, float] = {}
    for book, sides in book_sides.items():
        h = sides.get("home")
        a = sides.get("away")
        if h is None or a is None or (h + a) <= 0:
            continue
        out[book] = devig_two_way(h, a)
    return out


def collect(sport: str | None, since: datetime) -> list[RowResult]:
    markets = db.list_settled_markets(sport=sport, since=since)
    out: list[RowResult] = []
    for m in markets:
        side = m["winning_side"]
        if side == "void":
            continue
        home_won = 1 if side == "home" else 0
        start = datetime.fromisoformat(m["event_start"].replace("Z", "+00:00"))
        probs: dict[str, dict[float, float | None]] = {
            book: {h: None for h in CHECKPOINTS_HOURS} for book in BOOKS
        }
        for h in CHECKPOINTS_HOURS:
            ts = _checkpoint_ts(start, h)
            by_book = _book_home_probs_nearest(m["id"], ts)
            for book, p in by_book.items():
                if book in probs:
                    probs[book][h] = p
        out.append(
            RowResult(
                market_id=m["id"],
                sport=m.get("sport", ""),
                event_name=m.get("event_name", ""),
                event_start=start,
                home_won=home_won,
                probs=probs,
            )
        )
    return out


def score(rows: list[RowResult]) -> dict[str, dict[float, dict[str, float]]]:
    """Returns: summary[book][hours_before] = {'n': int, 'brier': float}"""
    agg: dict[str, dict[float, dict[str, float]]] = {
        book: {h: {"n": 0, "sse": 0.0} for h in CHECKPOINTS_HOURS}
        for book in BOOKS
    }
    for r in rows:
        actual = float(r.home_won)
        for book in BOOKS:
            for h in CHECKPOINTS_HOURS:
                p = r.probs[book][h]
                if p is None:
                    continue
                agg[book][h]["n"] += 1
                agg[book][h]["sse"] += (p - actual) ** 2
    summary: dict[str, dict[float, dict[str, float]]] = {}
    for book, by_h in agg.items():
        summary[book] = {}
        for h, v in by_h.items():
            n = int(v["n"])
            brier = (v["sse"] / n) if n else float("nan")
            summary[book][h] = {"n": float(n), "brier": brier}
    return summary


def _fmt_row(book: str, summary: dict[float, dict[str, float]]) -> str:
    parts = [f"{book:>4}"]
    for h in CHECKPOINTS_HOURS:
        s = summary[h]
        n = int(s["n"])
        b = s["brier"]
        if n == 0:
            parts.append(f"T-{int(h)}h: —")
        else:
            parts.append(f"T-{int(h)}h: {b:.4f} (n={n})")
    return "  ".join(parts)


def print_summary(rows: list[RowResult]) -> None:
    s = score(rows)
    total = len(rows)
    print(f"\nBrier scores — {total} settled markets")
    print("(lower is better; shows n games scored per checkpoint)\n")
    for book in BOOKS:
        # Skip books with no data across any checkpoint
        has_any = any(s[book][h]["n"] > 0 for h in CHECKPOINTS_HOURS)
        if not has_any:
            continue
        print(_fmt_row(book, s[book]))
    print()
    # Winner must be scored on >= 50% of the max-n book's slate AND n >= 5
    # absolute. Prevents a small-sample book from beating a full-slate book.
    print("Best book per checkpoint (n >= max(5, 50% of max book's n)):")
    for h in CHECKPOINTS_HOURS:
        ns = [int(s[book][h]["n"]) for book in BOOKS]
        max_n = max(ns) if ns else 0
        min_required = max(MIN_N_WINNER, int(max_n * 0.5))
        candidates = [
            (book, s[book][h]["brier"], int(s[book][h]["n"]))
            for book in BOOKS
            if s[book][h]["n"] >= min_required
        ]
        if not candidates:
            print(f"  T-{int(h)}h: (insufficient data; need n >= {min_required})")
            continue
        best = min(candidates, key=lambda x: x[1])
        print(
            f"  T-{int(h)}h: {best[0]} (Brier {best[1]:.4f}, n={best[2]}; "
            f"min_required={min_required}, max_n={max_n})"
        )
    print()


def dump_csv(path: str, rows: list[RowResult]) -> None:
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        header = ["market_id", "sport", "event_name", "event_start", "home_won"]
        for book in BOOKS:
            for h in CHECKPOINTS_HOURS:
                header.append(f"{book.lower()}_t_minus_{int(h)}h")
        w.writerow(header)
        for r in rows:
            row = [
                r.market_id, r.sport, r.event_name,
                r.event_start.isoformat(), r.home_won,
            ]
            for book in BOOKS:
                for h in CHECKPOINTS_HOURS:
                    p = r.probs[book][h]
                    row.append("" if p is None else f"{p:.4f}")
            w.writerow(row)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    p = argparse.ArgumentParser(prog="brier")
    p.add_argument("--sport", help="Filter to one sport (e.g. NFL)")
    p.add_argument("--days", type=int, default=30,
                   help="Look back N days of settled markets (default 30)")
    p.add_argument("--csv", help="Write per-market CSV to this path")
    args = p.parse_args(argv)

    since = datetime.now(timezone.utc) - timedelta(days=args.days)
    rows = collect(args.sport, since)
    if not rows:
        log.warning("no settled markets found — populate market_outcomes first")
        return 0

    print_summary(rows)
    if args.csv:
        dump_csv(args.csv, rows)
        log.info("wrote %s (%d rows)", args.csv, len(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
