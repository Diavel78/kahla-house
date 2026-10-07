"""NBA data-source probe — Phase 0 of the NBA side + totals model.

Read-only. Hits every candidate free source from the Actions runner (the
Claude sandbox's egress blocks ESPN/NBA hosts) and prints, per source:
reachable?, row counts, and the FIELD SHAPE we would build on. No writes.

Questions it answers:
  1. Scores/schedule spine     — ESPN scoreboard (we already use it).
  2. Team box (pace inputs)    — ESPN summary boxscore stat labels
                                 (FGA/FTA/OREB/TOV -> possessions).
  3. Player minutes/injuries   — ESPN summary players + injuries.
  4. CLOSING LINES (gate 2)    — ESPN summary `pickcenter` and the core
                                 odds API (open/close?) for a PAST game.
  5. NBA's own feeds           — cdn.nba.com schedule + boxscore,
                                 stats.nba.com team game log.
  6. Bulk history              — shufinskiy/nba_data + hoopR-nba-data
                                 GitHub releases.
  7. Exchange coverage         — Kalshi NBA series (game / total / spread).

Usage:  python -m scripts.probe_nba_sources [--date YYYYMMDD]
"""
from __future__ import annotations

import argparse
import json
import sys

import requests

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"}
NBA_HDRS = dict(UA, **{"Referer": "https://www.nba.com/", "Origin": "https://www.nba.com",
                       "Accept": "application/json, text/plain, */*",
                       "x-nba-stats-origin": "stats", "x-nba-stats-token": "true"})

REPORT: dict = {}


def get(url, headers=None, timeout=20):
    try:
        r = requests.get(url, headers=headers or UA, timeout=timeout)
        body = None
        try:
            body = r.json()
        except Exception:
            pass
        return r.status_code, body, len(r.content)
    except Exception as e:
        return f"ERR {type(e).__name__}: {e}"[:160], None, 0


def shape(obj, depth=2):
    """Key skeleton (no values) so the log shows structure, not data."""
    if depth < 0:
        return type(obj).__name__
    if isinstance(obj, dict):
        return {k: shape(v, depth - 1) for k, v in list(obj.items())[:25]}
    if isinstance(obj, list):
        return [shape(obj[0], depth - 1), f"len={len(obj)}"] if obj else []
    return type(obj).__name__


def probe_espn(date):
    sb_url = f"https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard?dates={date}"
    st, body, n = get(sb_url)
    ev = (body or {}).get("events") or []
    out = {"scoreboard": {"status": st, "events": len(ev)}}
    if not ev:
        REPORT["espn"] = out
        return
    eid = ev[0]["id"]
    comp = ev[0]["competitions"][0]
    out["scoreboard"]["sample"] = ev[0].get("name")
    out["scoreboard"]["comp_odds"] = shape(comp.get("odds"), 2)

    st, s, n = get(f"https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary?event={eid}")
    out["summary"] = {"status": st, "bytes": n, "top_keys": list((s or {}).keys())}
    if s:
        teams = (s.get("boxscore") or {}).get("teams") or []
        if teams:
            out["summary"]["team_stat_labels"] = [
                (x.get("name") or x.get("label"), x.get("displayValue")) for x in teams[0].get("statistics", [])]
        players = (s.get("boxscore") or {}).get("players") or []
        if players:
            st0 = (players[0].get("statistics") or [{}])[0]
            out["summary"]["player_labels"] = st0.get("labels") or st0.get("names")
            out["summary"]["player_rows"] = len(st0.get("athletes") or [])
        out["summary"]["pickcenter"] = [
            {k: pc.get(k) for k in ("provider", "details", "spread", "overUnder",
                                     "homeTeamOdds", "awayTeamOdds")}
            for pc in (s.get("pickcenter") or [])][:3]
        out["summary"]["odds_shape"] = shape(s.get("odds"), 2)
        out["summary"]["injuries_teams"] = len(s.get("injuries") or [])

    core = (f"https://sports.core.api.espn.com/v2/sports/basketball/leagues/nba/"
            f"events/{eid}/competitions/{eid}/odds")
    st, c, n = get(core)
    out["core_odds"] = {"status": st, "count": (c or {}).get("count")}
    items = (c or {}).get("items") or []
    if items:
        first = items[0]
        out["core_odds"]["provider"] = (first.get("provider") or {}).get("name")
        out["core_odds"]["keys"] = list(first.keys())
        for k in ("open", "close", "current"):
            if k in (first.get("homeTeamOdds") or {}):
                out["core_odds"][f"home_{k}"] = first["homeTeamOdds"][k]
        out["core_odds"]["providers"] = [(i.get("provider") or {}).get("name") for i in items]
    REPORT["espn"] = out


def probe_nba_cdn():
    out = {}
    st, b, n = get("https://cdn.nba.com/static/json/staticData/scheduleLeagueV2.json", NBA_HDRS)
    dates = ((b or {}).get("leagueSchedule") or {}).get("gameDates") or []
    games = [g for d in dates for g in d.get("games", [])]
    out["schedule"] = {"status": st, "season": ((b or {}).get("leagueSchedule") or {}).get("seasonYear"),
                       "game_dates": len(dates), "games": len(games),
                       "game_shape": shape(games[0], 1) if games else None}
    gid = "0022500001"  # first 2025-26 regular-season game id
    st, b, n = get(f"https://cdn.nba.com/static/json/liveData/boxscore/boxscore_{gid}.json", NBA_HDRS)
    g = (b or {}).get("game") or {}
    out["boxscore"] = {"status": st, "game_keys": list(g.keys())[:30],
                       "team_stat_keys": list(((g.get("homeTeam") or {}).get("statistics") or {}).keys())[:60]}
    st, b, n = get("https://stats.nba.com/stats/leaguegamelog?Counter=10&Direction=DESC&LeagueID=00"
                   "&PlayerOrTeam=T&Season=2025-26&SeasonType=Regular%20Season&Sorter=DATE",
                   NBA_HDRS, timeout=25)
    rs = ((b or {}).get("resultSets") or [{}])[0]
    out["stats_api"] = {"status": st, "headers": rs.get("headers"), "rows": len(rs.get("rowSet") or [])}
    REPORT["nba_cdn"] = out


def probe_github():
    out = {}
    for repo in ("shufinskiy/nba_data", "sportsdataverse/hoopR-nba-data"):
        st, b, n = get(f"https://api.github.com/repos/{repo}/releases?per_page=10")
        rel = b if isinstance(b, list) else []
        out[repo] = {"status": st, "releases": [
            {"tag": r.get("tag_name"), "published": r.get("published_at"),
             "assets": len(r.get("assets") or []),
             "sample": [a["name"] for a in (r.get("assets") or [])][-6:]} for r in rel[:6]]}
    REPORT["github"] = out


def probe_kalshi():
    base = "https://api.elections.kalshi.com/trade-api/v2"
    out = {}
    st, b, n = get(f"{base}/series?category=Sports", timeout=30)
    ser = (b or {}).get("series") or []
    out["nba_series"] = sorted({s.get("ticker") for s in ser if "NBA" in (s.get("ticker") or "")})[:60]
    for t in ("KXNBAGAME", "KXNBATOTAL", "KXNBASPREAD"):
        st, b, n = get(f"{base}/markets?series_ticker={t}&status=open&limit=5")
        m = (b or {}).get("markets") or []
        out[t] = {"status": st, "open": len(m), "sample": [x.get("ticker") for x in m[:3]]}
    REPORT["kalshi"] = out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default="20260315", help="a PAST regular-season date")
    a = ap.parse_args()
    for fn, args in ((probe_espn, (a.date,)), (probe_nba_cdn, ()),
                     (probe_github, ()), (probe_kalshi, ())):
        try:
            fn(*args)
        except Exception as e:
            REPORT[fn.__name__] = f"CRASH {type(e).__name__}: {e}"
    print("NBA_PROBE_REPORT " + json.dumps(REPORT, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
