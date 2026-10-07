"""NHL history fetcher — ESPN scoreboard + summary → compact JSONL cache.

The skater-availability layer's spine (Oct 7 2026, Rob: "I want bets on
hockey"). One row per REGULAR-SEASON game (ESPN season.type 2):
  {id, date, season, home, away, hs, as,
   players:{home:[[aid, name, toi_min, pos, pm, pts, goalie]...], away:[...]},
   line:{ml_home, ml_away, total, provider}}

Team codes are ESPN's; `nhl_sheet_data._ESPN_TO_NHL` maps them to the
goalie/shot spine's tricodes. Players NOT dressed are simply absent from
ESPN's box — that absence is the historical "inactive list".

  python -m scripts.nhl_fetch --seasons 2023,2024,2025,2026 --out .nhl_cache
Resumable like nba_fetch: a cached game id is never refetched.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor

from scripts.nba_fetch import _amer, _get, _num

log = logging.getLogger("nhl_fetch")
SB = "https://site.api.espn.com/apis/site/v2/sports/hockey/nhl/scoreboard"
SUM = "https://site.api.espn.com/apis/site/v2/sports/hockey/nhl/summary"
CORE = ("https://sports.core.api.espn.com/v2/sports/hockey/leagues/nhl/"
        "events/{e}/competitions/{e}/odds")


def _toi(v):
    """'18:32' → 18.53 minutes."""
    if v is None:
        return None
    s = str(v)
    if ":" in s:
        try:
            m, sec = s.split(":", 1)
            return int(m) + int(sec) / 60.0
        except ValueError:
            return None
    return _num(s)


def scoreboard_ids(day: dt.date, season: int):
    b = _get(SB, {"dates": day.strftime("%Y%m%d"), "limit": 50})
    out = []
    for ev in (b or {}).get("events") or []:
        s = ev.get("season") or {}
        if s.get("type") != 2 or s.get("year") != season:
            continue
        if not (((ev.get("status") or {}).get("type") or {}).get("completed")):
            continue
        out.append(ev["id"])
    return out


def parse_players(group: dict):
    """One ESPN boxscore stat group → rows. Labels are located by NAME so a
    reordered box can only null a stat, never swap two."""
    labels = group.get("labels") or group.get("names") or []
    idx = {str(l).upper(): i for i, l in enumerate(labels)}
    gname = (group.get("name") or group.get("text") or "").lower()
    is_g = "goalie" in gname or "SV" in idx or "SA" in idx
    rows = []
    for a in group.get("athletes") or []:
        ath = a.get("athlete") or {}
        st = a.get("stats") or []
        pos = ((ath.get("position") or {}).get("abbreviation")) or ("G" if is_g else None)

        def f(k, conv=_num):
            i = idx.get(k)
            return conv(st[i]) if i is not None and i < len(st) else None
        toi = f("TOI", _toi) if st else 0.0
        g, ast = f("G"), f("A")
        pts = (g or 0.0) + (ast or 0.0) if (g is not None or ast is not None) else None
        rows.append([str(ath.get("id")), ath.get("displayName"), toi or 0.0, pos,
                     f("+/-"), pts, is_g])
    return rows


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
    players = {}
    for t in (s.get("boxscore") or {}).get("players") or []:
        side = side_of.get(str((t.get("team") or {}).get("id")))
        if not side:
            continue
        rows = []
        for grp in t.get("statistics") or []:
            rows.extend(parse_players(grp))
        players[side] = rows
    return {"id": eid, "date": comp.get("date"), "season": season,
            "home": teams["home"]["abbr"], "away": teams["away"]["abbr"],
            "home_id": teams["home"]["id"], "away_id": teams["away"]["id"],
            "hs": teams["home"]["score"], "as": teams["away"]["score"],
            "players": players, "line": None}


def core_line(eid):
    """DK close off ESPN's core odds API (moneyline + total)."""
    b = _get(CORE.format(e=eid))
    for it in (b or {}).get("items") or []:
        h, a = it.get("homeTeamOdds") or {}, it.get("awayTeamOdds") or {}
        mh = _amer((h.get("close") or {}).get("moneyLine")) or _num(h.get("moneyLine"))
        ma = _amer((a.get("close") or {}).get("moneyLine")) or _num(a.get("moneyLine"))
        total = _num(it.get("overUnder"))
        tc = _amer(((it.get("close") or {}).get("total") or {}))
        if tc is not None:
            total = abs(tc)
        if not (mh and ma) and total is None:
            continue
        return {"ml_home": mh, "ml_away": ma, "total": total,
                "provider": (it.get("provider") or {}).get("name")}
    return None


def fetch_season(season: int, out_dir: str, workers: int = 8):
    path = os.path.join(out_dir, f"nhl_{season}.jsonl")
    have = set()
    if os.path.exists(path):
        for ln in open(path):
            try:
                have.add(json.loads(ln)["id"])
            except Exception:
                pass
    days = [dt.date(season - 1, 10, 1) + dt.timedelta(d) for d in range(215)]
    with ThreadPoolExecutor(workers) as ex:
        ids = sorted({e for L in ex.map(lambda d: scoreboard_ids(d, season), days)
                      for e in L} - have)
    log.info("season %s: %d cached, %d to fetch", season, len(have), len(ids))

    def one(eid):
        s = _get(SUM, {"event": eid})
        row = parse_summary(eid, s, season) if s else None
        if row:
            row["line"] = core_line(eid)
        return row

    n = nl = 0
    with open(path, "a") as f, ThreadPoolExecutor(workers) as ex:
        for row in ex.map(one, ids):
            if not row or row["hs"] is None:
                continue
            f.write(json.dumps(row, separators=(",", ":")) + "\n")
            n += 1
            nl += bool(row["line"])
    log.info("season %s: wrote %d (with line %d)", season, n, nl)


def load(out_dir: str, seasons):
    rows, seen = [], set()
    for s in seasons:
        p = os.path.join(out_dir, f"nhl_{s}.jsonl")
        if not os.path.exists(p):
            continue
        for ln in open(p):
            r = json.loads(ln)
            if r["id"] not in seen:
                seen.add(r["id"])
                rows.append(r)
    rows.sort(key=lambda r: r["date"])
    return rows


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--seasons", default="2023,2024,2025,2026")
    ap.add_argument("--out", default=".nhl_cache")
    ap.add_argument("--probe", default="", help="an ESPN event id: print the box shape")
    a = ap.parse_args()
    if a.probe:
        s = _get(SUM, {"event": a.probe}) or {}
        for t in (s.get("boxscore") or {}).get("players") or []:
            for g in t.get("statistics") or []:
                print("GROUP", g.get("name"), g.get("labels"), len(g.get("athletes") or []))
        print("ROW", json.dumps(parse_summary(a.probe, s, 0))[:1500])
        print("LINE", core_line(a.probe))
        return 0
    os.makedirs(a.out, exist_ok=True)
    for s in [int(x) for x in a.seasons.split(",") if x]:
        fetch_season(s, a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
