"""CFBD ratings → cfbd_ratings (SP+, FPI, Elo, SRS) — the college PRE-MARKET line.

Rob, Sep 17 2026: the results-based ratings are an opponent-adjusted solve over
each team's 2-3 games in week 3 (40-day half-life ⇒ last season weighs ~0.5%);
Indiana read 64.8 off 52-16 and 55-0 over cupcakes, Notre Dame–Purdue priced
at 12.9. SP+/FPI carry a preseason prior with the roster in it. This mirrors
CollegeFootballData's documented, bearer-authenticated ratings endpoints so
app._cfbd_consensus can center a seat on them before a book line exists.

Env: CFBD_API_KEY (free key: collegefootballdata.com/key). Absent → exit 2,
nothing written (the pricer then falls back exactly as before).

  python -m scripts.ingest_cfbd_ratings --probe            # print 3 rows per source, write nothing
  python -m scripts.ingest_cfbd_ratings --commit [--year 2026]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

import httpx

BASE = "https://api.collegefootballdata.com"
SOURCES = {
    "sp":  ("/ratings/sp",  lambda r: r.get("rating")),
    "fpi": ("/ratings/fpi", lambda r: r.get("fpi")),
    "elo": ("/ratings/elo", lambda r: r.get("elo")),
    "srs": ("/ratings/srs", lambda r: r.get("rating")),
}


def _f(x):
    try:
        return None if x is None else float(x)
    except (TypeError, ValueError):
        return None


def fetch(source: str, year: int, key: str) -> list[dict]:
    path, getter = SOURCES[source]
    r = httpx.get(BASE + path, params={"year": year},
                  headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
                  timeout=30.0)
    r.raise_for_status()
    body = r.json()
    if not isinstance(body, list):
        raise RuntimeError(f"{source}: unexpected body {str(body)[:120]}")
    out = []
    for row in body:
        team = row.get("team")
        val = _f(getter(row))
        if not team or val is None:
            continue
        # SP+ rows repeat per week in some years; keep the newest when a `week` exists
        out.append({"source": source, "year": int(row.get("year") or year), "team": team,
                    "conference": row.get("conference"), "rating": val,
                    "extra": {k: v for k, v in row.items() if k not in ("team", "year", "conference")},
                    "fetched_at": datetime.now(timezone.utc).isoformat()})
    # de-dup per team: last row wins (CFBD returns season-final or latest-week first/last inconsistently;
    # SP+ carries no week field in season mode, elo carries `week` — take the max week)
    best: dict[str, dict] = {}
    for o in out:
        w = _f((o["extra"] or {}).get("week")) or 0.0
        prev = best.get(o["team"])
        if prev is None or w >= (_f((prev["extra"] or {}).get("week")) or 0.0):
            best[o["team"]] = o
    return list(best.values())


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--year", type=int, default=datetime.now().year)
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--probe", action="store_true")
    a = ap.parse_args(argv)
    key = (os.getenv("CFBD_API_KEY") or "").strip()
    if not key:
        print("CFBD_API_KEY not set — nothing fetched, nothing written (paste the free key into .env)")
        return 2
    total = 0
    sb = None
    if a.commit:
        from storage import supabase_client as db
        sb = db.client()
    for src in SOURCES:
        try:
            rows = fetch(src, a.year, key)
        except Exception as ex:
            print(f"{src}: FAILED {str(ex)[:200]}")
            continue
        print(f"{src}: {len(rows)} teams")
        if a.probe:
            for r in sorted(rows, key=lambda r: -(r["rating"] or 0))[:3]:
                print(f"   {r['team']:<24} {r['rating']:8.2f}  {json.dumps(r['extra'])[:80]}")
        if sb is not None and rows:
            for i in range(0, len(rows), 200):
                sb.table("cfbd_ratings").upsert(rows[i:i + 200], on_conflict="source,year,team").execute()
            total += len(rows)
    if sb is not None:
        print(f"wrote {total} rows")
    return 0 if total or a.probe else 1


if __name__ == "__main__":
    sys.exit(main())
