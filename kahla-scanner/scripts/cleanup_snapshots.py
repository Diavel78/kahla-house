"""Delete old snapshot rows past RETENTION_DAYS (default 15).

Run nightly via .github/workflows/snapshot-cleanup.yml. Covers:
  - book_snapshots  (frozen PIN history — still drained per the cutover plan)
  - prop_snapshots  (props pipeline cent history — the movement engine only
                     reads the last 18-24h, so 15d is generous headroom)
  - poly_trades     (whale trade tape — the dossier card reads 24h; reviews
                     join by market_id+time within the same window class)
pm_snapshots is deliberately NOT here — it's the exchange history the CLV
close + sharp score read across a game's whole life; keep it.

CLI:
  python -m scripts.cleanup_snapshots               # default 15 days
  python -m scripts.cleanup_snapshots --days 30     # custom retention
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
# Chunk size on the loop. PostgREST DELETE has no LIMIT clause, so we use a
# rolling captured_at upper bound to walk through old data without timing
# out a huge first-run delete.
CHUNK_HOURS = 6


def _cleanup_table(table: str, cutoff: datetime) -> int:
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
                .gte("captured_at", lower.isoformat())
                .lt("captured_at", upper.isoformat())
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
                    .select("captured_at")
                    .lt("captured_at", lower.isoformat())
                    .order("captured_at", desc=True)
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
                    older_exists[0]["captured_at"].replace("Z", "+00:00")
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
    return 0


if __name__ == "__main__":
    sys.exit(main())
