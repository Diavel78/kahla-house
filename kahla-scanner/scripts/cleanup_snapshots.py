"""Delete book_snapshots rows older than RETENTION_DAYS (default 15).

Run nightly via .github/workflows/snapshot-cleanup.yml. Trims Supabase
storage now that the divergence/Brier scanner is gone — chart history
beyond ~2 weeks isn't useful (games are over) and the table just bloats.

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
# Chunk size on the loop. PostgREST DELETE has no LIMIT clause, so we use a
# rolling captured_at upper bound to walk through old data without timing
# out a huge first-run delete.
CHUNK_HOURS = 6


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
        # Cheap count via head request would be ideal but supabase-py doesn't
        # expose it cleanly. Just sample the oldest few rows so we know there
        # is or isn't data older than the cutoff.
        sample = (
            db.client()
            .table("book_snapshots")
            .select("captured_at")
            .lt("captured_at", cutoff_iso)
            .order("captured_at", desc=False)
            .limit(5)
            .execute()
            .data
            or []
        )
        log.info("DRY RUN. Oldest rows older than cutoff (up to 5): %s",
                 [r["captured_at"] for r in sample])
        return 0

    # Walk backwards in CHUNK_HOURS slices so each delete is bounded. Stops
    # once a slice deletes 0 rows (no older data).
    total = 0
    upper = cutoff
    while True:
        lower = upper - timedelta(hours=CHUNK_HOURS)
        try:
            res = (
                db.client()
                .table("book_snapshots")
                .delete()
                .gte("captured_at", lower.isoformat())
                .lt("captured_at", upper.isoformat())
                .execute()
            )
        except Exception as e:
            log.exception("delete chunk failed (lower=%s upper=%s): %s",
                          lower.isoformat(), upper.isoformat(), e)
            break
        n = len(res.data) if res.data else 0
        log.info("Deleted %d rows in [%s, %s)", n, lower.isoformat(), upper.isoformat())
        total += n
        if n == 0:
            # No data in this slice. Two more empty slices in a row and we
            # stop entirely; otherwise keep walking back through quiet gaps.
            try:
                older_exists = (
                    db.client()
                    .table("book_snapshots")
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
            # Jump to the actual oldest row's slice instead of walking 6h at a time
            try:
                next_upper = datetime.fromisoformat(
                    older_exists[0]["captured_at"].replace("Z", "+00:00")
                ) + timedelta(seconds=1)
                upper = next_upper
                continue
            except Exception:
                break
        upper = lower

    log.info("Cleanup complete. Deleted %d rows total older than %d days.",
             total, args.days)
    return 0


if __name__ == "__main__":
    sys.exit(main())
