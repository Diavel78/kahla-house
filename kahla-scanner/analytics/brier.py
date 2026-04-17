"""Brier-score calibration (M0).

For every settled market, pulls each source's HOME-side implied probability at
checkpoint offsets (T-24h, T-6h, T-1h, T-0) relative to event_start, then
computes Brier score against the actual outcome (home win = 1.0, else 0.0).

Lowest Brier score per checkpoint = sharpest source at that horizon.

Sources scored:
    poly  — latest Polymarket tick at/before checkpoint, outcome='HOME'
    dk    — latest DK moneyline snapshot, devig'd home prob
    fd    — latest FD moneyline snapshot, devig'd home prob

Run:
    python -m analytics.brier --sport NFL --days 30
    python -m analytics.brier --days 30          # all sports

Output: one table per source summarising n_games and mean Brier per checkpoint.
Plus a CSV dump per market if --csv out.csv given.
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


@dataclass
class RowResult:
    market_id: str
    sport: str
    event_name: str
    event_start: datetime
    home_won: int             # 1 if home won, 0 if away, -1 for void
    # probs_by_source[source][hours_before] = prob | None
    probs: dict[str, dict[float, float | None]]


def _home_prob_from_book(snaps: list[dict[str, Any]]) -> float | None:
    home = away = None
    for s in snaps:
        if s["market_type"] != "moneyline":
            continue
        if s["side"] == "home":
            home = float(s["implied_prob"]) if s.get("implied_prob") is not None else None
        elif s["side"] == "away":
            away = float(s["implied_prob"]) if s.get("implied_prob") is not None else None
    if home is None or away is None or (home + away) <= 0:
        return None
    return devig_two_way(home, away)


def _checkpoint_ts(event_start: datetime, hours_before: float) -> datetime:
    return event_start - timedelta(hours=hours_before)


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
            "poly": {}, "dk": {}, "fd": {}
        }
        for h in CHECKPOINTS_HOURS:
            ts = _checkpoint_ts(start, h)
            # Poly
            tick = db.poly_tick_nearest(m["id"], "HOME", ts)
            probs["poly"][h] = float(tick["price"]) if tick else None
            # DK / FD
            probs["dk"][h] = _home_prob_from_book(
                db.book_snapshot_nearest(m["id"], "DK", ts)
            )
            probs["fd"][h] = _home_prob_from_book(
                db.book_snapshot_nearest(m["id"], "FD", ts)
            )
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
    """Returns: summary[source][hours_before] = {'n': int, 'brier': float}"""
    out: dict[str, dict[float, dict[str, float]]] = {
        s: {h: {"n": 0, "sse": 0.0} for h in CHECKPOINTS_HOURS}
        for s in ("poly", "dk", "fd")
    }
    for r in rows:
        actual = float(r.home_won)
        for source in ("poly", "dk", "fd"):
            for h in CHECKPOINTS_HOURS:
                p = r.probs[source][h]
                if p is None:
                    continue
                out[source][h]["n"] += 1
                out[source][h]["sse"] += (p - actual) ** 2
    summary: dict[str, dict[float, dict[str, float]]] = {}
    for source, by_h in out.items():
        summary[source] = {}
        for h, agg in by_h.items():
            n = int(agg["n"])
            brier = (agg["sse"] / n) if n else float("nan")
            summary[source][h] = {"n": float(n), "brier": brier}
    return summary


def _fmt_row(source: str, summary: dict[float, dict[str, float]]) -> str:
    parts = [f"{source.upper():>4}"]
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
    for source in ("poly", "dk", "fd"):
        print(_fmt_row(source, s[source]))
    print()
    # Head-to-head: best source per checkpoint
    print("Best source per checkpoint:")
    for h in CHECKPOINTS_HOURS:
        candidates = [
            (src, s[src][h]["brier"], int(s[src][h]["n"]))
            for src in ("poly", "dk", "fd")
            if s[src][h]["n"] > 0
        ]
        if not candidates:
            print(f"  T-{int(h)}h: no data")
            continue
        best = min(candidates, key=lambda x: x[1])
        print(
            f"  T-{int(h)}h: {best[0].upper()} (Brier {best[1]:.4f}, "
            f"n={best[2]})"
        )
    print()


def dump_csv(path: str, rows: list[RowResult]) -> None:
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        header = ["market_id", "sport", "event_name", "event_start", "home_won"]
        for src in ("poly", "dk", "fd"):
            for h in CHECKPOINTS_HOURS:
                header.append(f"{src}_t_minus_{int(h)}h")
        w.writerow(header)
        for r in rows:
            row = [
                r.market_id, r.sport, r.event_name,
                r.event_start.isoformat(), r.home_won,
            ]
            for src in ("poly", "dk", "fd"):
                for h in CHECKPOINTS_HOURS:
                    p = r.probs[src][h]
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
