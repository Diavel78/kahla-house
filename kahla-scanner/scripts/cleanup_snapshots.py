"""Delete old snapshot rows past each table's retention window.

Run nightly via .github/workflows/snapshot-cleanup.yml. Covers (Aug 5
2026 rev — the Supabase Disk-IO email: the DB hit ~1GB, 668MB of it
10-day-old prop_snapshots, plus 82MB of heartbeat rows nothing read):
  - book_snapshots   15d (frozen PIN history — still drained)
  - prop_snapshots   15d (props cent tape; the movement engine reads 24h)
  - poly_trades      15d (whale tape; dossier card reads 24h)
  - vsin_snapshots   60d (Circa movement curve — reviews read weeks)
  - pm_snapshots     60d (exchange history: CLV close + sharp score read
                     18h windows; backtests read weeks. 60d bounds it
                     without starving any current consumer)
  - resolver_runs     7d (per-minute heartbeat — header shows the latest)
  - odds_ingest_runs  7d (DEAD since the June 19 cutover — keeps it empty)

CLI:
  python -m scripts.cleanup_snapshots               # per-table defaults
  python -m scripts.cleanup_snapshots --days 30     # override table set 1
  python -m scripts.cleanup_snapshots --dry-run     # log only, no delete
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timedelta, timezone

from storage import supabase_client as db

log = logging.getLogger(__name__)

DEFAULT_DAYS = 15
TABLES = ("book_snapshots", "prop_snapshots", "poly_trades")
# (table, timestamp_column, retention_days) — tables with their OWN windows,
# cleaned after the --days set above. Timestamp columns differ per table.
EXTRA_TABLES = (
    ("vsin_snapshots", "captured_at", 60),
    ("pm_snapshots", "captured_at", 60),
    ("resolver_runs", "run_at", 7),
    ("odds_ingest_runs", "fetched_at", 7),
)
# Chunk size on the loop. PostgREST DELETE has no LIMIT clause, so we use a
# rolling captured_at upper bound to walk through old data without timing
# out a huge first-run delete.
CHUNK_HOURS = 6


def _cleanup_table(table: str, cutoff: datetime, col: str = "captured_at") -> int:
    """Walk backwards in CHUNK_HOURS slices so each delete is bounded.
    Stops once no rows older than the current slice exist."""
    total = 0
    upper = cutoff
    while True:
        lower = upper - timedelta(hours=CHUNK_HOURS)
        try:
            res = (
                db.client()
                .table(table)
                .delete()
                .gte(col, lower.isoformat())
                .lt(col, upper.isoformat())
                .execute()
            )
        except Exception as e:
            log.exception("%s: delete chunk failed (lower=%s upper=%s): %s",
                          table, lower.isoformat(), upper.isoformat(), e)
            break
        n = len(res.data) if res.data else 0
        log.info("%s: deleted %d rows in [%s, %s)",
                 table, n, lower.isoformat(), upper.isoformat())
        total += n
        if n == 0:
            # No data in this slice — jump straight to the actual oldest
            # remaining row instead of walking 6h at a time through gaps.
            try:
                older_exists = (
                    db.client()
                    .table(table)
                    .select(col)
                    .lt(col, lower.isoformat())
                    .order(col, desc=True)
                    .limit(1)
                    .execute()
                    .data
                    or []
                )
            except Exception:
                older_exists = []
            if not older_exists:
                break
            try:
                upper = datetime.fromisoformat(
                    older_exists[0][col].replace("Z", "+00:00")
                ) + timedelta(seconds=1)
                continue
            except Exception:
                break
        upper = lower
    return total


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(prog="cleanup_snapshots")
    p.add_argument("--days", type=int, default=DEFAULT_DAYS,
                   help=f"Retention window in days (default {DEFAULT_DAYS})")
    p.add_argument("--dry-run", action="store_true",
                   help="Count only — don't delete anything.")
    args = p.parse_args(argv)

    cutoff = datetime.now(timezone.utc) - timedelta(days=args.days)
    cutoff_iso = cutoff.isoformat()
    log.info("Retention window: %d days (cutoff %s)", args.days, cutoff_iso)

    if args.dry_run:
        for table in TABLES:
            try:
                sample = (
                    db.client()
                    .table(table)
                    .select("captured_at")
                    .lt("captured_at", cutoff_iso)
                    .order("captured_at", desc=False)
                    .limit(5)
                    .execute()
                    .data
                    or []
                )
            except Exception as e:
                log.warning("%s: dry-run sample failed (%s)", table, e)
                continue
            log.info("DRY RUN %s. Oldest rows older than cutoff (up to 5): %s",
                     table, [r["captured_at"] for r in sample])
        return 0

    for table in TABLES:
        total = _cleanup_table(table, cutoff)
        log.info("%s: cleanup complete, deleted %d rows older than %d days.",
                 table, total, args.days)
    now = datetime.now(timezone.utc)
    for table, col, days in EXTRA_TABLES:
        total = _cleanup_table(table, now - timedelta(days=days), col)
        log.info("%s: cleanup complete, deleted %d rows older than %d days.",
                 table, total, days)
    return 0


if __name__ == "__main__":
    sys.exit(main())
