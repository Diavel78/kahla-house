"""Grade Whiff IQ K-props — paperlog shadows AND real bot_picks bets —
from mlb_pitcher_games.

Rows are matched by (pitcher name, ET game date) to the spine's actual
starter line; side 'yes' wins iff K >= line, 'no' wins iff K < line
(the PMM ladder has no push — "at least L" is binary). PnL = to-WIN at
entry_price, the house convention. bot_picks prop rows carry the line
in signal_blob.whiff.line (entry_line is NULL by prop convention); the
per-minute resolver skips market_type='prop', so this daily pass is
their ONLY grader. Runs in whiff-iq-compute.yml after the snapshot
build; unmatched rows stay pending and retry.

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
    picks = (sb.table("bot_picks")
             .select("id,side,entry_price,units,event_start,signal_blob")
             .eq("status", "pending").eq("market_type", "prop")
             .filter("signal_blob->whiff->>pitcher", "neq", "")
             .lt("event_start",
                 (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat())
             .limit(500).execute().data) or []
    if not rows and not picks:
        print("no pending whiff rows past start")
        return 0
    # spine lookup window bounded by the oldest pending row (either table)
    dates = set()
    for r in rows + picks:
        try:
            dt = datetime.fromisoformat(str(r["event_start"]).replace("Z", "+00:00"))
            dates.add(dt.astimezone(ET).date().isoformat())
        except Exception:
            continue
    if not dates:
        print("no parseable event dates")
        return 0
    lo = min(dates)
    starts = (sb.table("mlb_pitcher_games")
              .select("pitcher_name,game_date,strikeouts,batters_faced,"
                      "outs,hits,walks")
              .eq("started", True).gte("game_date", lo)
              .limit(5000).execute().data) or []
    # value tuple per start: (K, outs, hits, walks) — every pitcher-stat
    # family rides this grader; whiff.ptype picks the metric index.
    actual: dict[tuple, list[tuple]] = {}
    for s in starts:
        actual.setdefault((s["pitcher_name"], s["game_date"]), []).append(
            (s.get("strikeouts") or 0, s.get("outs"),
             s.get("hits"), s.get("walks")))

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
        pty = ((blob.get("whiff") or {}).get("ptype")) or "k"
        idx = {"k": 0, "outs": 1, "ha": 2, "bb": 3}.get(pty, 0)
        val = ks[0][idx]
        if val is None:
            skipped += 1
            continue
        won = (val >= line) if r["side"] == "yes" else (val < line)
        pnl = _to_win_pnl(r.get("entry_price"), won, float(r.get("units") or 1))
        sb.table("pickbot_paperlog").update(
            {"status": "won" if won else "lost", "pnl_units": pnl,
             "result_score": json.dumps({pty: val}),
             "settled_at": now}
        ).eq("id", r["id"]).execute()
        graded += 1
    print(f"whiff shadows: graded {graded}, still pending {skipped}")

    # ---- real bot_picks K-prop bets (the whiff_autobet lane) ----------
    bg = bs = 0
    for r in picks:
        blob = r.get("signal_blob") or {}
        w = blob.get("whiff") or {}
        name = w.get("pitcher") or ""
        try:
            gdate = (datetime.fromisoformat(str(r["event_start"])
                     .replace("Z", "+00:00")).astimezone(ET).date().isoformat())
            line = float(w.get("line"))
        except Exception:
            bs += 1
            continue
        ks = actual.get((name, gdate))
        if not ks or len(ks) > 1:
            bs += 1
            continue
        _tmap = {"baseball_player_outs": "outs",
                 "baseball_player_hits_allowed": "ha",
                 "baseball_player_walks_allowed": "bb"}
        pty = (w.get("ptype")
               or _tmap.get((blob.get("prop") or {}).get("type") or "")
               or "k")
        idx = {"k": 0, "outs": 1, "ha": 2, "bb": 3}.get(pty, 0)
        val = ks[0][idx]
        if val is None:
            bs += 1
            continue
        won = (val >= line) if r["side"] == "yes" else (val < line)
        pnl = _to_win_pnl(r.get("entry_price"), won, float(r.get("units") or 1))
        sb.table("bot_picks").update(
            {"status": "won" if won else "lost", "pnl_units": pnl,
             "result_score": {pty: val},
             "settled_at": now}
        ).eq("id", r["id"]).execute()
        bg += 1
    print(f"whiff bot_picks: graded {bg}, still pending {bs}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
