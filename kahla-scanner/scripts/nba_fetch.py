"""NBA history fetcher — ESPN scoreboard + summary → compact JSONL cache.

One row per REGULAR-SEASON game (ESPN season.type 2):
  {id, date, season, home, away, hs, as, box:{home:{...}, away:{...}},
   players:{home:[[aid, name, min, starter, pm, pts]...], away:[...]},
   line:{spread, total, ml_home, ml_away, provider}}

`spread` is the HOME line (negative = home favored), as ESPN's pickcenter
serves it. For a finished game pickcenter carries the CLOSE (verified on the
Mar 15 2026 probe: core odds `close` == pickcenter).

Usage:
  python -m scripts.nba_fetch --seasons 2023,2024,2025,2026 --out .nba_cache
Resumable: a season file that already holds a game id is never refetched.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import requests

log = logging.getLogger("nba_fetch")
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"}
SB = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"
SUM = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary"

_S = requests.Session()
_S.headers.update(UA)


def _get(url, params=None, tries=4):
    for i in range(tries):
        try:
            r = _S.get(url, params=params, timeout=20)
            if r.status_code == 200:
                return r.json()
            if r.status_code in (404, 400):
                return None
        except Exception:
            pass
        time.sleep(1.5 * (i + 1))
    return None


def season_dates(season: int):
    """ESPN season Y = the (Y-1)-Y season. Scan Oct 1 → Apr 25."""
    d = dt.date(season - 1, 10, 1)
    end = dt.date(season, 4, 25)
    while d <= end:
        yield d
        d += dt.timedelta(days=1)


def scoreboard_ids(day: dt.date, season: int):
    b = _get(SB, {"dates": day.strftime("%Y%m%d"), "limit": 50})
    out = []
    for ev in (b or {}).get("events") or []:
        s = ev.get("season") or {}
        if s.get("type") != 2 or s.get("year") != season:
            continue
        st = ((ev.get("status") or {}).get("type") or {})
        if not st.get("completed"):
            continue
        out.append((ev["id"], ev.get("date")))
    return out


def _num(v):
    try:
        return float(str(v).replace("+", ""))
    except Exception:
        return None


def _box(team_stats):
    m = {}
    for x in team_stats or []:
        name, val = x.get("name") or "", x.get("displayValue")
        if "-" in name and isinstance(val, str) and "-" in val:
            a, b = name.split("-", 1)
            va, vb = val.split("-", 1)
            m[a], m[b] = _num(va), _num(vb)
        else:
            m[name] = _num(val)
    keep = ("fieldGoalsMade", "fieldGoalsAttempted", "threePointFieldGoalsMade",
            "threePointFieldGoalsAttempted", "freeThrowsMade", "freeThrowsAttempted",
            "offensiveRebounds", "defensiveRebounds", "totalRebounds", "assists",
            "steals", "blocks", "turnovers", "totalTurnovers", "fouls")
    return {k: m.get(k) for k in keep}


def parse_summary(eid, s, season):
    comp = ((s.get("header") or {}).get("competitions") or [{}])[0]
    teams = {}
    for c in comp.get("competitors") or []:
        teams[c.get("homeAway")] = {"id": str((c.get("team") or {}).get("id")),
                                     "abbr": (c.get("team") or {}).get("abbreviation"),
                                     "score": _num(c.get("score"))}
    if "home" not in teams or "away" not in teams:
        return None
    side_of = {teams["home"]["id"]: "home", teams["away"]["id"]: "away"}
    box, players = {}, {}
    bx = s.get("boxscore") or {}
    for t in bx.get("teams") or []:
        side = side_of.get(str((t.get("team") or {}).get("id")))
        if side:
            box[side] = _box(t.get("statistics"))
    for t in bx.get("players") or []:
        side = side_of.get(str((t.get("team") or {}).get("id")))
        if not side:
            continue
        st0 = (t.get("statistics") or [{}])[0]
        labels = st0.get("labels") or st0.get("names") or []
        idx = {l: i for i, l in enumerate(labels)}
        rows = []
        for a in st0.get("athletes") or []:
            ath = a.get("athlete") or {}
            stats = a.get("stats") or []
            if not stats or a.get("didNotPlay"):
                mn = 0.0
                pm = pts = None
            else:
                mn = _num(stats[idx["MIN"]]) if "MIN" in idx else None
                pm = _num(stats[idx["+/-"]]) if "+/-" in idx else None
                pts = _num(stats[idx["PTS"]]) if "PTS" in idx else None
            rows.append([str(ath.get("id")), ath.get("displayName"), mn or 0.0,
                         bool(a.get("starter")), pm, pts])
        players[side] = rows
    line = None
    for pc in s.get("pickcenter") or []:
        if pc.get("overUnder") is None and pc.get("spread") is None:
            continue
        line = {"spread": _num(pc.get("spread")), "total": _num(pc.get("overUnder")),
                "ml_home": _num((pc.get("homeTeamOdds") or {}).get("moneyLine")),
                "ml_away": _num((pc.get("awayTeamOdds") or {}).get("moneyLine")),
                "provider": (pc.get("provider") or {}).get("name")}
        break
    return {"id": eid, "date": comp.get("date"), "season": season,
            "home": teams["home"]["abbr"], "away": teams["away"]["abbr"],
            "hs": teams["home"]["score"], "as": teams["away"]["score"],
            "box": box, "players": players, "line": line}


def fetch_season(season: int, out_dir: str, workers: int = 8):
    path = os.path.join(out_dir, f"nba_{season}.jsonl")
    have = set()
    if os.path.exists(path):
        with open(path) as f:
            for ln in f:
                try:
                    have.add(json.loads(ln)["id"])
                except Exception:
                    pass
    days = list(season_dates(season))
    with ThreadPoolExecutor(workers) as ex:
        lists = list(ex.map(lambda d: scoreboard_ids(d, season), days))
    ids = sorted({eid for L in lists for eid, _ in L} - have)
    log.info("season %s: %d cached, %d to fetch", season, len(have), len(ids))

    def one(eid):
        s = _get(SUM, {"event": eid})
        return parse_summary(eid, s, season) if s else None

    n_ok = n_line = 0
    with open(path, "a") as f, ThreadPoolExecutor(workers) as ex:
        for row in ex.map(one, ids):
            if not row or row["hs"] is None:
                continue
            f.write(json.dumps(row, separators=(",", ":")) + "\n")
            n_ok += 1
            n_line += bool(row["line"])
    log.info("season %s: wrote %d (with line %d)", season, n_ok, n_line)
    return path


def load(out_dir: str, seasons):
    rows = []
    for s in seasons:
        p = os.path.join(out_dir, f"nba_{s}.jsonl")
        if not os.path.exists(p):
            continue
        seen = set()
        with open(p) as f:
            for ln in f:
                r = json.loads(ln)
                if r["id"] in seen:
                    continue
                seen.add(r["id"])
                rows.append(r)
    rows.sort(key=lambda r: r["date"])
    return rows


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--seasons", default="2023,2024,2025,2026")
    ap.add_argument("--out", default=".nba_cache")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    for s in [int(x) for x in a.seasons.split(",") if x]:
        fetch_season(s, a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
