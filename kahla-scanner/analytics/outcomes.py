"""Populate market_outcomes from various sources.

For M0 this is deliberately minimal — manual CSV entry is fine. Later this
gets wired to a scores API or Polymarket's resolution feed.

Usage (CLI):
    python -m analytics.outcomes from-csv path/to/outcomes.csv

CSV format: market_id,winning_side
  (winning_side in {'home','away','void'})
"""
from __future__ import annotations

import argparse
import csv
import logging
import sys

from storage import supabase_client as db

log = logging.getLogger(__name__)


def ingest_csv(path: str) -> int:
    n = 0
    with open(path) as f:
        for row in csv.DictReader(f):
            mid = row.get("market_id")
            side = (row.get("winning_side") or "").strip().lower()
            if not mid or side not in {"home", "away", "void"}:
                log.warning("skipping row: %s", row)
                continue
            db.upsert_outcome(mid, side, source="manual")
            n += 1
    return n


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(prog="outcomes")
    sub = p.add_subparsers(dest="cmd", required=True)

    from_csv = sub.add_parser("from-csv", help="Ingest outcomes from a CSV file")
    from_csv.add_argument("path")

    args = p.parse_args(argv)
    if args.cmd == "from-csv":
        n = ingest_csv(args.path)
        log.info("upserted %d outcomes", n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
