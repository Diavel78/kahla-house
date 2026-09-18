"""NFL pre-market inputs → cfbd_ratings (nfelo) + book_lines (nflverse Vegas lines).

Rob, Sep 17 2026 ("ESPN has an FPI endpoint for the NFL… check out NFELO… nflverse
also works"): the NFL results ratings have the same two-game September problem as
college. Two public, machine-readable sources fix it:
  • nfelo `output_data/elo_snapshot.csv` — per-team nfelo Elo WITH its QB adjustment
    and `pts_vs_avg` (points vs an average team) — the NFL prior app._cfbd_consensus
    blends against the results solve (source='nfelo').
  • nflverse `nfldata/data/games.csv` — the current week's Vegas `spread_line`
    (positive = home favored) and `total_line` — written to book_lines as book
    'nflverse' (line = HOME line, negative = home favored, the table's convention),
    behind Pinnacle/DraftKings/FanDuel in _BOOK_PRIORITY.
Daily on the batch lane. Public URLs, no key.

  python -m scripts.ingest_nfl_market --probe
  python -m scripts.ingest_nfl_market --commit
"""
from __future__ import annotations

import argparse
import csv
import io
import sys
from datetime import datetime, timedelta, timezone

import httpx

NFELO_URL = "https://raw.githubusercontent.com/greerreNFL/nfelo/main/output_data/elo_snapshot.csv"
NFLVERSE_URL = "https://github.com/nflverse/nfldata/raw/master/data/games.csv"
CODES = {"ARI": "Arizona Cardinals", "ATL": "Atlanta Falcons", "BAL": "Baltimore Ravens", "BUF": "Buffalo Bills",
         "CAR": "Carolina Panthers", "CHI": "Chicago Bears", "CIN": "Cincinnati Bengals", "CLE": "Cleveland Browns",
         "DAL": "Dallas Cowboys", "DEN": "Denver Broncos", "DET": "Detroit Lions", "GB": "Green Bay Packers",
         "HOU": "Houston Texans", "IND": "Indianapolis Colts", "JAX": "Jacksonville Jaguars", "KC": "Kansas City Chiefs",
         "LA": "Los Angeles Rams", "LAR": "Los Angeles Rams", "LAC": "Los Angeles Chargers", "LV": "Las Vegas Raiders", "OAK": "Las Vegas Raiders",
         "MIA": "Miami Dolphins", "MIN": "Minnesota Vikings", "NE": "New England Patriots", "NO": "New Orleans Saints",
         "NYG": "New York Giants", "NYJ": "New York Jets", "PHI": "Philadelphia Eagles", "PIT": "Pittsburgh Steelers",
         "SEA": "Seattle Seahawks", "SF": "San Francisco 49ers", "TB": "Tampa Bay Buccaneers", "TEN": "Tennessee Titans",
         "WAS": "Washington Commanders", "WSH": "Washington Commanders"}


def _get(url: str) -> str:
    r = httpx.get(url, follow_redirects=True, timeout=40.0, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    return r.text


def _f(x):
    try:
        return None if x in (None, "", "NA") else float(x)
    except (TypeError, ValueError):
        return None


def nfelo_rows(now):
    out = []
    for r in csv.DictReader(io.StringIO(_get(NFELO_URL))):
        team = CODES.get((r.get("team") or "").upper())
        pts = _f(r.get("pts_vs_avg"))
        if not team or pts is None:
            continue
        out.append({"source": "nfelo", "year": int(_f(r.get("season")) or now.year), "team": team,
                    "conference": None, "rating": pts,
                    "extra": {"nfelo": _f(r.get("nfelo")), "nfelo_base": _f(r.get("nfelo_base")),
                              "qb_adj": _f(r.get("qb_adj")), "week": _f(r.get("week"))},
                    "fetched_at": now.isoformat()})
    return out


def nflverse_games(now):
    out = []
    for r in csv.DictReader(io.StringIO(_get(NFLVERSE_URL))):
        if r.get("season") != str(now.year):
            continue
        try:
            gd = datetime.strptime(r["gameday"], "%Y-%m-%d").date()
        except Exception:
            continue
        if gd < (now - timedelta(days=1)).date() or gd > (now + timedelta(days=10)).date():
            continue
        away, home = CODES.get((r.get("away_team") or "").upper()), CODES.get((r.get("home_team") or "").upper())
        if not away or not home:
            continue
        out.append({"event_name": f"{away} @ {home}", "gameday": gd, "week": r.get("week"),
                    "spread_home": (-_f(r.get("spread_line"))) if _f(r.get("spread_line")) is not None else None,
                    "total": _f(r.get("total_line"))})
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--probe", action="store_true")
    a = ap.parse_args(argv)
    now = datetime.now(timezone.utc)
    ratings = nfelo_rows(now)
    games = nflverse_games(now)
    print(f"nfelo: {len(ratings)} teams (week {max((r['extra']['week'] or 0) for r in ratings) if ratings else '?'})")
    print(f"nflverse: {len(games)} games in the window, {sum(1 for g in games if g['spread_home'] is not None)} with a spread")
    if a.probe:
        for r in sorted(ratings, key=lambda r: -r["rating"])[:5]:
            print(f"   {r['team']:<24} pts_vs_avg {r['rating']:+6.2f}  qb_adj {r['extra']['qb_adj']:+6.1f}")
        for g in games[:6]:
            print(f"   {g['event_name']:<46} home line {g['spread_home']}  total {g['total']}")
    if not a.commit:
        return 0
    from storage import supabase_client as db
    sb = db.client()
    for i in range(0, len(ratings), 200):
        sb.table("cfbd_ratings").upsert(ratings[i:i + 200], on_conflict="source,year,team").execute()
    mk = (sb.table("markets").select("id,event_name,event_start").eq("sport", "NFL").eq("status", "active")
          .gte("event_start", (now - timedelta(days=1)).isoformat()).limit(400).execute().data) or []
    by_name = {}
    for m in mk:
        by_name.setdefault(m["event_name"], []).append(m)
    ups, miss = [], 0
    for g in games:
        cands = by_name.get(g["event_name"]) or []
        best = None
        for m in cands:
            try:
                es = datetime.fromisoformat(str(m["event_start"]).replace("Z", "+00:00")).date()
            except Exception:
                continue
            if abs((es - g["gameday"]).days) <= 1:
                best = m
        if not best:
            miss += 1
            continue
        if g["spread_home"] is not None:
            ups.append({"market_id": best["id"], "market_type": "spread", "book": "nflverse",
                        "line": g["spread_home"], "seen_at": now.isoformat()})
        if g["total"] is not None:
            ups.append({"market_id": best["id"], "market_type": "total", "book": "nflverse",
                        "line": g["total"], "seen_at": now.isoformat()})
    for i in range(0, len(ups), 200):
        sb.table("book_lines").upsert(ups[i:i + 200], on_conflict="market_id,market_type,book").execute()
    print(f"wrote {len(ratings)} nfelo rows, {len(ups)} nflverse book lines ({miss} games unmatched to markets)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
