"""Grade Fight IQ duration-family paperlog shadows from ufc_fights.

The per-minute resolver grades UFC MONEYLINE only (ESPN winner boolean).
The duration shadows (signal_blob.ufc_duration: the distance prop +
rounds lines) need the fight's round + time — which the weekly UFCStats
ingest delivers to `ufc_fights`. This runs right after it in the
ufc-model workflow: match each pending shadow to its bout by fighter
last-name tokens (either orientation, ±2 days), compute the duration,
grade, and stamp to-WIN pnl. Weekly latency is fine for a shadow
dataset that only feeds the promotion review.

  python -m scripts.grade_ufc_paperlog            # dry-run
  python -m scripts.grade_ufc_paperlog --commit
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from datetime import datetime, timedelta, timezone

from storage import supabase_client as db

log = logging.getLogger(__name__)

DISTANCE_3R = 14.99
DISTANCE_5R = 24.99


def _toks(s: str) -> list[str]:
    return [t for t in re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).split()
            if len(t) >= 3]


def _name_match(a: str, b: str) -> bool:
    ta, tb = _toks(a), _toks(b)
    if not ta or not tb:
        return False
    return ta[-1] in tb or tb[-1] in ta


def _duration_min(rnd, fight_time) -> float | None:
    if not rnd:
        return None
    try:
        mm, ss = str(fight_time or "5:00").split(":")
        return (int(rnd) - 1) * 5.0 + int(mm) + int(ss) / 60.0
    except (ValueError, TypeError):
        return None


def _pnl_units(status: str, price, units) -> float:
    """to-WIN sizing — mirrors bot_picks_resolver."""
    units = float(units or 1)
    if status == "won":
        return units
    if status != "lost":
        return 0.0
    try:
        p = int(price)
    except (TypeError, ValueError):
        return -units
    return -(units * 100.0 / p) if p > 0 else -(units * abs(p) / 100.0)


def _find_bout(sb, away: str, home: str, event_start: str) -> dict | None:
    try:
        d0 = datetime.fromisoformat(str(event_start).replace("Z", "+00:00"))
    except Exception:
        return None
    lo = (d0 - timedelta(days=2)).date().isoformat()
    hi = (d0 + timedelta(days=2)).date().isoformat()
    try:
        rows = (sb.table("ufc_fights")
                .select("fighter1_name,fighter2_name,method,round,fight_time,result")
                .gte("event_date", lo).lte("event_date", hi)
                .limit(200).execute().data) or []
    except Exception:
        return []
    for f in rows:
        n1, n2 = f.get("fighter1_name") or "", f.get("fighter2_name") or ""
        if ((_name_match(n1, home) and _name_match(n2, away))
                or (_name_match(n1, away) and _name_match(n2, home))):
            return f
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    sb = db.client()

    try:
        rows = (sb.table("pickbot_paperlog")
                .select("id,event_name,event_start,market_type,side,line,"
                        "entry_price,units,signal_blob")
                .eq("sport", "UFC").eq("status", "pending")
                .eq("signal_blob->>ufc_duration", "true")
                .limit(2000).execute().data) or []
    except Exception as e:
        log.error("paperlog query failed: %s", e)
        return 2
    log.info("%d pending UFC duration shadows", len(rows))
    graded = skipped = 0
    for r in rows:
        name = r.get("event_name") or ""
        if " @ " not in name:
            continue
        away, home = [x.strip() for x in name.split(" @ ", 1)]
        bout = _find_bout(sb, away, home, r.get("event_start"))
        if not bout or bout.get("result") == "nc":
            skipped += 1
            continue
        dur = _duration_min(bout.get("round"), bout.get("fight_time"))
        if dur is None:
            skipped += 1
            continue
        five_r = (bout.get("round") or 0) > 3
        mt, side = r.get("market_type"), r.get("side")
        if mt == "distance":
            went = dur >= (DISTANCE_5R if five_r else DISTANCE_3R)
            won = (side == "yes") == went
        elif mt == "total" and r.get("line") is not None:
            cut = int(float(r["line"])) * 5 + 2.5
            over = dur > cut
            won = (side == "over") == over
        else:
            skipped += 1
            continue
        status = "won" if won else "lost"
        pnl = _pnl_units(status, r.get("entry_price"), r.get("units"))
        result = json.dumps({"duration_min": round(dur, 2),
                             "round": bout.get("round"),
                             "method": bout.get("method")})
        graded += 1
        log.info("  %s %s/%s %s -> %s (%.2fmin) pnl %+0.2f",
                 name, mt, side, r.get("line"), status.upper(), dur, pnl)
        if args.commit:
            sb.table("pickbot_paperlog").update({
                "status": status, "pnl_units": pnl,
                "settled_at": datetime.now(timezone.utc).isoformat(),
                "result_score": result,
            }).eq("id", r["id"]).execute()
    log.info("graded %d · unmatched/skipped %d%s", graded, skipped,
             "" if args.commit else " (dry-run)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
