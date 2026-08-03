"""Grade Whiff IQ K-prop paperlog shadows from mlb_pitcher_games.

Pending pickbot_paperlog rows with signal_blob.whiff_shadow=true are
matched by (pitcher name, ET game date) to the spine's actual starter
line; side 'yes' wins iff K >= line, 'no' wins iff K < line (the PMM
ladder has no push — "at least L" is binary). PnL = to-WIN at
entry_price, the house convention. Runs daily in whiff-iq-compute.yml
after the snapshot build; unmatched rows stay pending and retry.

  python -m scripts.grade_whiff_paperlog
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone, date
from zoneinfo import ZoneInfo

from storage import supabase_client as db

ET = ZoneInfo("America/New_York")


def _to_win_pnl(price, won: bool, units: float) -> float | None:
    try:
        p = float(price)
    except (TypeError, ValueError):
        return None
    if won:
        return units
    return -units * (100.0 / p) if p > 0 else -units * (abs(p) / 100.0)


def main() -> int:
    sb = db.client()
    rows = (sb.table("pickbot_paperlog")
            .select("id,side,line,entry_price,units,event_start,signal_blob")
            .eq("status", "pending").eq("market_type", "prop")
            .filter("signal_blob->>whiff_shadow", "eq", "true")
            .lt("event_start",
                (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat())
            .limit(2000).execute().data) or []
    if not rows:
        print("no pending whiff shadows past start")
        return 0
    # spine lookup window bounded by the oldest pending row
    dates = set()
    for r in rows:
        try:
            dt = datetime.fromisoformat(str(r["event_start"]).replace("Z", "+00:00"))
            dates.add(dt.astimezone(ET).date().isoformat())
        except Exception:
            continue
    lo = min(dates)
    starts = (sb.table("mlb_pitcher_games")
              .select("pitcher_name,game_date,strikeouts,batters_faced")
              .eq("started", True).gte("game_date", lo)
              .limit(5000).execute().data) or []
    actual: dict[tuple, list[int]] = {}
    for s in starts:
        actual.setdefault((s["pitcher_name"], s["game_date"]), []).append(
            s.get("strikeouts") or 0)

    graded = skipped = 0
    now = datetime.now(timezone.utc).isoformat()
    for r in rows:
        blob = r.get("signal_blob") or {}
        if isinstance(blob, str):
            try:
                blob = json.loads(blob)
            except Exception:
                blob = {}
        name = ((blob.get("whiff") or {}).get("pitcher")) or ""
        try:
            gdate = (datetime.fromisoformat(str(r["event_start"])
                     .replace("Z", "+00:00")).astimezone(ET).date().isoformat())
            line = float(r["line"])
        except Exception:
            skipped += 1
            continue
        ks = actual.get((name, gdate))
        if not ks:
            skipped += 1              # not final / name miss — retry tomorrow
            continue
        if len(ks) > 1:               # doubleheader double-start ambiguity
            skipped += 1
            continue
        won = (ks[0] >= line) if r["side"] == "yes" else (ks[0] < line)
        pnl = _to_win_pnl(r.get("entry_price"), won, float(r.get("units") or 1))
        sb.table("pickbot_paperlog").update(
            {"status": "won" if won else "lost", "pnl_units": pnl,
             "result_score": json.dumps({"k": ks[0]}), "settled_at": now}
        ).eq("id", r["id"]).execute()
        graded += 1
    print(f"whiff shadows: graded {graded}, still pending {skipped}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
