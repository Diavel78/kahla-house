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


def _fmt_amer(p) -> str:
    if p is None:
        return "?"
    try:
        v = int(p)
    except (TypeError, ValueError):
        return str(p)
    return f"+{v}" if v > 0 else str(v)


def _fmt_pt(v) -> str:
    if v is None:
        return ""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    if f > 0 and abs(f) < 1000:
        return f"+{f:g}"
    return f"{f:g}"


def auto_reasons(blob: dict, market_type: str, side: str,
                 away: str, home: str) -> list[str]:
    """Plain-English bullets grounded in this market's signals. Mirrors
    `_buildReasons()` in templates/handicapper.html — keep them in sync.
    Returns 1-3 bullets. Empty list if blob lacks usable data."""
    if not blob:
        return []
    odds = (blob.get("odds") or {})
    blk = (odds.get(market_type) or {})
    mv = blk.get("movement") or {}
    splits = blob.get("splits") or {}

    side_name = (side or "").upper() if market_type == "total" else (
        home if side == "home" else away
    )
    sharp_side = mv.get("sharp_side")
    sharp_name = None
    if sharp_side:
        sharp_name = (sharp_side or "").upper() if market_type == "total" else (
            home if sharp_side == "home" else away
        )
    score = mv.get("sharp_score") or 0

    lines: list[str] = []

    # Line movement
    if mv:
        oP, cP = mv.get("opener_price"), mv.get("current_price")
        oL, cL = mv.get("opener_line"),  mv.get("current_line")
        sharp_frag = (
            f" — sharp side {sharp_name} ({score}/10)" if sharp_name
            else f" — sharp {score}/10"
        )
        if market_type == "moneyline":
            if oP is not None and cP is not None and oP != cP:
                lines.append(f"Line moved {_fmt_amer(oP)} → {_fmt_amer(cP)}{sharp_frag}")
            elif score == 0:
                lines.append("PIN hasn't budged — flat market, no line signal")
        else:
            line_changed = (oL is not None and cL is not None and abs(cL - oL) >= 0.01)
            # Tag displayed prices with ref_side on TOT — ref_side may
            # be the OPPOSITE of sharp_side when the vig moved on the
            # side that got easier (= other side sharp by elimination).
            ref_side = (mv.get("ref_side") or "") if isinstance(mv, dict) else ""
            ref_tag = f" [{ref_side}]" if (ref_side and market_type == "total") else ""
            if line_changed:
                label = "Spread" if market_type == "spread" else "Total"
                lines.append(f"{label} moved {_fmt_pt(oL)} → {_fmt_pt(cL)}{sharp_frag}")
            elif oP is not None and cP is not None and oP != cP:
                line_at = f" at {_fmt_pt(cL)}" if cL is not None else ""
                lines.append(f"PIN{ref_tag}: {_fmt_amer(oP)} → {_fmt_amer(cP)}{line_at}{sharp_frag}")
            elif score == 0:
                lines.append("PIN line and vig flat — no movement signal")

    # Public splits — ML only. Money% (Action) is the sharp fingerprint;
    # bets% (Covers / VegasInsider) is the square fingerprint. When only
    # bets% is available, fall back to heavy/light public-side wording.
    if splits and market_type == "moneyline":
        m_key = "home_money" if side == "home" else "away_money"
        b_key = "home_bets"  if side == "home" else "away_bets"
        m = splits.get(m_key)
        b = splits.get(b_key)
        sources = splits.get("sources") or []
        src_lbl = f" [{len(sources)} sources]" if len(sources) > 1 else ""
        if m is not None and b is not None:
            try:
                diff = round(float(m) - float(b))
            except (TypeError, ValueError):
                diff = 0
            if diff >= 10:
                lines.append(f"Public {m}% money / {b}% bets on {side_name} — sharp money divergence (+{diff}pp){src_lbl}")
            elif diff <= -10:
                lines.append(f"Public {m}% money / {b}% bets on {side_name} — square money, fade signal ({abs(diff)}pp){src_lbl}")
            elif (m or 0) >= 65:
                lines.append(f"Public heavy on {side_name} ({m}% money / {b}% bets){src_lbl}")
            elif (m or 0) <= 35:
                lines.append(f"Public light on {side_name} ({m}% money / {b}% bets) — contrarian side{src_lbl}")
            else:
                lines.append(f"Public split flat ({b}% bets / {m}% money on {side_name}){src_lbl}")
        elif b is not None:
            if b >= 65:
                lines.append(f"Public heavy on {side_name} ({b}% bets){src_lbl} — money% unavailable")
            elif b <= 35:
                lines.append(f"Public light on {side_name} ({b}% bets){src_lbl} — contrarian side")
            else:
                lines.append(f"Public split roughly even ({b}% bets on {side_name}){src_lbl}")

    # Polymarket fair line (only when it differs from PIN's current price)
    pin_side = (blk.get("pin_current") or {}).get(side) or {}
    fair = pin_side.get("fair_american")
    pin_now = pin_side.get("price")
    if fair is not None and pin_now != fair:
        pin_frag = f", PIN now {_fmt_amer(pin_now)}" if pin_now is not None else ""
        lines.append(f"Polymarket target {_fmt_amer(fair)} — devigged fair{pin_frag}")

    return lines[:3]


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

    # If caller didn't pass --reason and we have a signal blob, fall back
    # to auto-generated bullets. Same wording as the on-page suggestion
    # card so chat-logged picks aren't blank on /handicapper.
    reasons = list(args.reason) if args.reason else []
    if not reasons and signal_blob:
        away, _, home = (m["event_name"] or "").partition(" @ ")
        reasons = auto_reasons(signal_blob, args.market_type, args.side,
                               away.strip(), home.strip())

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
        "reasons":     reasons or None,
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
