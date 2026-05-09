"""Handicapper Bot — pick logger.

After running scripts/handicapper.py and writing the analysis, this CLI
inserts the recommendation into bot_picks. Resolver later grades it.

CLI:
  python -m scripts.handicapper_log_pick \
      --market-id <uuid> \
      --market-type moneyline \
      --side home \
      --book DK \
      --price -125 \
      --line "" \
      --units 3 \
      --confidence high \
      --fair-prob 0.62 \
      --edge-pp 2.4 \
      --sharp-score 6 \
      --analysis-file /tmp/analysis.md \
      --reason "PIN moved Tor -120 → -135 (sharp 5)" \
      --reason "Splits: 38% bets / 62% money on home — sharp money on TOR" \
      --query "Toronto vs Angels today, thoughts?"

Sport / event_name / event_start are pulled from the markets row.
asked_by defaults to "admin" (CLI invocation = me running it manually).

Idempotent on (market_id, market_type, side) within 7 days — second
invocation on the same pick is silently skipped (returns exit 0). Pass
--allow-duplicate to override.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from storage import supabase_client as db

log = logging.getLogger(__name__)


def _already_logged(sb, market_id: str, market_type: str, side: str,
                    lookback_hours: int = 168) -> bool:
    cutoff = (datetime.now(timezone.utc)
              - timedelta(hours=lookback_hours)).isoformat()
    try:
        rows = (sb.table("bot_picks")
                .select("id")
                .eq("market_id", market_id)
                .eq("market_type", market_type)
                .eq("side", side)
                .gte("picked_at", cutoff)
                .limit(1)
                .execute().data) or []
    except Exception as e:
        log.warning("dedup check failed (will skip pick): %s", e)
        return True
    return bool(rows)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="handicapper_log_pick")
    p.add_argument("--market-id", required=True)
    p.add_argument("--market-type", required=True,
                   choices=["moneyline", "spread", "total"])
    p.add_argument("--side", required=True,
                   choices=["home", "away", "over", "under"])
    p.add_argument("--book", required=True,
                   help="Entry book code (DK, FD, MGM, ...)")
    p.add_argument("--price", type=int, required=True,
                   help="American odds at entry (e.g. -125 or 145)")
    p.add_argument("--line", default="",
                   help="Spread/total point. Empty for moneyline.")
    p.add_argument("--units", type=int, required=True,
                   choices=[1, 3, 5, 10])
    p.add_argument("--confidence", required=True,
                   choices=["low", "medium", "high", "whale"])
    p.add_argument("--fair-prob", type=float, default=None)
    p.add_argument("--edge-pp", type=float, default=None)
    p.add_argument("--sharp-score", type=int, default=None)
    p.add_argument("--analysis-file", type=Path, default=None,
                   help="Path to a markdown file with the full write-up")
    p.add_argument("--analysis", default=None,
                   help="Inline analysis text (alternative to --analysis-file)")
    p.add_argument("--reason", action="append", default=[],
                   help="Bullet reason (repeat). E.g. --reason \"...\"")
    p.add_argument("--query", default="",
                   help="Original user question")
    p.add_argument("--asked-by", default="admin",
                   help="Firebase UID of the asker (default: 'admin')")
    p.add_argument("--signal-blob", default=None,
                   help="JSON-serialized snapshot of the dossier")
    p.add_argument("--allow-duplicate", action="store_true",
                   help="Skip the dedup check")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(message)s")

    sb = db.client()

    # Lookup the market for sport / event_name / event_start.
    try:
        m = (sb.table("markets")
             .select("id,sport,event_name,event_start,status")
             .eq("id", args.market_id)
             .single()
             .execute().data)
    except Exception as e:
        log.error("market %s not found: %s", args.market_id, e)
        return 2
    if not m:
        log.error("market %s not found", args.market_id)
        return 2

    if not args.allow_duplicate and _already_logged(
            sb, args.market_id, args.market_type, args.side):
        log.info("pick already logged for this market/type/side in the last 7d — skipping")
        return 0

    line_val: float | None = None
    if args.line.strip():
        try:
            line_val = float(args.line)
        except ValueError:
            log.error("--line must be a number or empty")
            return 2

    if args.market_type in ("spread", "total") and line_val is None:
        log.error("--line required for spread/total picks")
        return 2

    analysis_md = ""
    if args.analysis_file:
        try:
            analysis_md = args.analysis_file.read_text()
        except Exception as e:
            log.error("could not read analysis file: %s", e)
            return 2
    elif args.analysis:
        analysis_md = args.analysis

    signal_blob = None
    if args.signal_blob:
        try:
            signal_blob = json.loads(args.signal_blob)
        except json.JSONDecodeError as e:
            log.error("--signal-blob is not valid JSON: %s", e)
            return 2

    row = {
        "asked_by":    args.asked_by,
        "query_text":  args.query,
        "market_id":   args.market_id,
        "sport":       m["sport"],
        "event_name":  m["event_name"],
        "event_start": m["event_start"],
        "market_type": args.market_type,
        "side":        args.side,
        "entry_book":  args.book,
        "entry_price": args.price,
        "entry_line":  line_val,
        "units":       args.units,
        "confidence":  args.confidence,
        "fair_prob":   args.fair_prob,
        "edge_pp":     args.edge_pp,
        "sharp_score": args.sharp_score,
        "analysis_md": analysis_md,
        "reasons":     args.reason or None,
        "signal_blob": signal_blob,
    }

    try:
        res = sb.table("bot_picks").insert(row).execute()
    except Exception as e:
        log.error("insert failed: %s", e)
        return 3

    pick_id = (res.data or [{}])[0].get("id")
    print(json.dumps({
        "ok": True,
        "id": pick_id,
        "event": m["event_name"],
        "side":  args.side,
        "book":  args.book,
        "price": args.price,
        "line":  line_val,
        "units": args.units,
        "confidence": args.confidence,
    }, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
