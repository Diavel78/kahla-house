"""FOOTBALL SHEETS — cloud-to-production DB sync.

Built Sep 19 2026 after /football-picks on the live site sat showing
games from three weeks earlier with no error anywhere. Root cause: the
Cellar cutover (Sep 4-5 2026) repointed Vercel's SUPABASE_URL at the
box's local Postgres (db.thekahlahouse.com), but this pipeline
(football-sheets-data.yml + football_sheet_render.py) still writes to
the ORIGINAL cloud Supabase project — two databases silently diverged,
and nothing that checked "did the build succeed" ever looked at whether
the SITE could see the result.

This script reads football_sheets + football_sheet_weeks for one
(week_key, sport) from the CLOUD project (SUPABASE_URL/SUPABASE_SERVICE_KEY
— the same GitHub Actions repo secrets the assembly step already uses)
and POSTs them to the live site's /api/football-sheets-mirror, which
upserts into whatever DB get_supabase() resolves to in production. Runs
on GitHub Actions (network reaches both the cloud DB and the site) —
NOT from a CCR sandbox, which is blocked from reaching thekahlahouse.com
entirely (see docs/football-sheet-runbook.md's site-curl bridge note).

Usage:
  python -m scripts.football_sheet_sync --week-key 2026-09-14 --sport NCAAF
  python -m scripts.football_sheet_sync --week-key 2026-09-14 --sport NCAAF --sport NFL
  python -m scripts.football_sheet_sync --latest --sport NCAAF   # whatever
      week_key football_sheet_weeks currently has for that sport
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts.football_sheet_data import sb_select  # noqa: E402

log = logging.getLogger("football_sheet_sync")

SITE = "https://www.thekahlahouse.com"


def sync_one(sport: str, week_key: str | None) -> dict:
    if not week_key:
        wk_rows = sb_select("football_sheet_weeks", {
            "select": "*", "sport": f"eq.{sport}",
            "order": "week_key.desc", "limit": "1"})
        if not wk_rows:
            return {"sport": sport, "error": "no football_sheet_weeks row"}
        week = wk_rows[0]
        week_key = week["week_key"]
    else:
        wk_rows = sb_select("football_sheet_weeks", {
            "select": "*", "sport": f"eq.{sport}", "week_key": f"eq.{week_key}"})
        if not wk_rows:
            return {"sport": sport, "week_key": week_key,
                    "error": "no football_sheet_weeks row for that week"}
        week = wk_rows[0]

    sheets = sb_select("football_sheets", {
        "select": ("id,week_key,sport,market_id,espn_id,event_name,"
                   "event_start,tier,data_blob,sheet_md,friday_md,"
                   "data_built_at,published_at,friday_published_at,created_at"),
        "week_key": f"eq.{week_key}", "sport": f"eq.{sport}"})

    key = (os.environ.get("FILLS_CRON_SECRET") or "").strip()
    if not key:
        raise SystemExit("FILLS_CRON_SECRET not set")
    r = httpx.post(f"{SITE}/api/football-sheets-mirror",
                   params={"key": key},
                   json={"week": week, "sheets": sheets},
                   timeout=120)
    r.raise_for_status()
    resp = r.json()
    return {"sport": sport, "week_key": week_key, "games": len(sheets),
            "mirror_response": resp}


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--sport", action="append", choices=["NFL", "NCAAF"],
                    required=True)
    ap.add_argument("--week-key", default=None,
                    help="omit (or pass --latest) to sync whatever "
                         "football_sheet_weeks currently has for the sport")
    ap.add_argument("--latest", action="store_true")
    args = ap.parse_args()

    week_key = None if args.latest else args.week_key
    results = [sync_one(sport, week_key) for sport in args.sport]
    for r in results:
        log.info("%s", r)
    print(json.dumps(results, indent=2, default=str))
    return 0 if all("error" not in r for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
