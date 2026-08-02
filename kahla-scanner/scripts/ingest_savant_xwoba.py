"""Ingest per-batter per-DAY expected wOBA from Baseball Savant.

Diamond IQ ITER4 (the Statcast spine): xwOBA (exit velo + launch angle)
says what SHOULD have happened; blending batter quality toward it
regresses luck out faster than realized results. Daily grain keeps the
backtest walk-forward safe. Source: the public statcast_search CSV
(pitch-level; PA-ending rows carry `events`). Per standard practice a
PA's value = estimated_woba_using_speedangle when present (batted
balls), else woba_value (K/BB/HBP have no batted ball).

Probe-first (the UFCStats lesson): the CSV's column names AND whether
Savant tolerates Actions-runner IPs are verified from the probe log
before any backfill. Day-by-day fetch, ~1.5s courtesy sleep.

  python -m scripts.ingest_savant_xwoba --probe
  python -m scripts.ingest_savant_xwoba --backfill --start 2025-03-18 --commit
  python -m scripts.ingest_savant_xwoba --delta --commit
"""
from __future__ import annotations

import argparse
import csv
import io
import logging
import sys
import time
from datetime import date, datetime, timedelta, timezone

import httpx

from storage import supabase_client as db

log = logging.getLogger(__name__)

CSV_URL = ("https://baseballsavant.mlb.com/statcast_search/csv"
           "?all=true&type=details&player_type=batter"
           "&game_date_gt={d}&game_date_lt={d}")
_HDRS = {"User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/126.0 Safari/537.36"),
         "Accept": "text/csv,*/*"}
_SLEEP = 1.5
_TRIES = 3


def fetch_day(client: httpx.Client, d: str) -> list[dict] | None:
    """All pitch rows for one date; None = fetch failed (≠ empty day)."""
    url = CSV_URL.format(d=d)
    for attempt in range(_TRIES):
        try:
            r = client.get(url, timeout=60)
            if r.status_code == 200:
                time.sleep(_SLEEP)
                return list(csv.DictReader(io.StringIO(r.text)))
            log.warning("HTTP %s savant %s (try %d)", r.status_code, d,
                        attempt + 1)
        except Exception as e:
            log.warning("savant %s failed (try %d): %s", d, attempt + 1, e)
        time.sleep(3.0 * (attempt + 1))
    return None


def _f(v):
    try:
        return float(v) if v not in (None, "", "null") else None
    except (TypeError, ValueError):
        return None


def aggregate_day(rows: list[dict], d: str) -> list[dict]:
    """PA-ending rows → per-batter {pa, xwoba_sum}. estimated wOBA when
    present, realized woba_value otherwise (K/BB/HBP)."""
    agg: dict = {}
    for r in rows:
        if not (r.get("events") or "").strip():
            continue                       # not a PA-ending pitch
        try:
            bid = int(float(r.get("batter") or 0))
        except (TypeError, ValueError):
            continue
        if not bid:
            continue
        val = _f(r.get("estimated_woba_using_speedangle"))
        if val is None:
            val = _f(r.get("woba_value"))
        if val is None:
            continue
        a = agg.setdefault(bid, {"pa": 0, "x": 0.0})
        a["pa"] += 1
        a["x"] += val
    return [{"game_date": d, "batter_id": b, "pa": a["pa"],
             "xwoba_sum": round(a["x"], 4)} for b, a in agg.items()]


def probe(client) -> int:
    d = "2026-06-20"
    rows = fetch_day(client, d)
    if rows is None:
        print("PROBE FAIL: savant unreachable (403/timeout — check whether "
              "Actions IPs are blocked; a proxy or local run is the fallback)")
        return 2
    print(f"PROBE {d}: {len(rows)} pitch rows")
    if not rows:
        print("PROBE FAIL: empty CSV — check date/params")
        return 2
    cols = list(rows[0].keys())
    need = ["events", "batter", "estimated_woba_using_speedangle",
            "woba_value"]
    print(f"PROBE columns ({len(cols)}): has-needed="
          f"{[c for c in need if c in cols]}")
    missing = [c for c in need if c not in cols]
    if missing:
        print(f"PROBE FAIL: missing columns {missing}; sample cols: "
              f"{cols[:40]}")
        return 2
    day = aggregate_day(rows, d)
    day.sort(key=lambda a: -a["pa"])
    print(f"PROBE aggregated: {len(day)} batters, "
          f"{sum(a['pa'] for a in day)} PAs")
    for a in day[:5]:
        print(f"  batter {a['batter_id']}: pa={a['pa']} "
              f"xwoba={a['xwoba_sum'] / a['pa']:.3f}")
    if len(day) < 100:
        print("PROBE WARN: fewer batters than a full slate should have")
        return 1
    print("PROBE OK")
    return 0


def ingest(client, sb, start: str, end: str, commit: bool) -> int:
    have: set = set()
    try:
        page = 0
        while True:      # explicit paging — the 1,000-row lesson
            rows = (sb.table("mlb_xwoba_batter_days").select("game_date")
                    .range(page * 1000, page * 1000 + 999).execute().data) or []
            have.update(r["game_date"] for r in rows)
            if len(rows) < 1000:
                break
            page += 1
    except Exception as e:
        log.warning("preload failed (%s) — treating all days as new", e)
    d = date.fromisoformat(start)
    stop = date.fromisoformat(end)
    days_done = rows_written = 0
    while d <= stop:
        ds = d.isoformat()
        d += timedelta(days=1)
        if ds in have:
            continue
        raw = fetch_day(client, ds)
        if raw is None:
            log.warning("skip %s (fetch failed)", ds)
            continue
        day = aggregate_day(raw, ds)
        if day and commit:
            sb.table("mlb_xwoba_batter_days").upsert(
                day, on_conflict="game_date,batter_id").execute()
        days_done += 1
        rows_written += len(day)
        if days_done % 20 == 0:
            log.info("  ...%d days (%d batter-day rows)", days_done,
                     rows_written)
    log.info("done: %d days · %d batter-day rows%s", days_done,
             rows_written, "" if commit else " (dry-run)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--backfill", action="store_true")
    ap.add_argument("--delta", action="store_true")
    ap.add_argument("--start", default="2025-03-18")
    ap.add_argument("--end", default=None)
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    client = httpx.Client(headers=_HDRS, follow_redirects=True)
    if args.probe:
        return probe(client)
    end = args.end or datetime.now(timezone.utc).date().isoformat()
    sb = db.client()
    if args.backfill:
        return ingest(client, sb, args.start, end, args.commit)
    if args.delta:
        start = (datetime.now(timezone.utc).date()
                 - timedelta(days=4)).isoformat()
        return ingest(client, sb, start, end, args.commit)
    log.error("nothing to do — pass --probe, --backfill or --delta")
    return 2


if __name__ == "__main__":
    sys.exit(main())
