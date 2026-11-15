"""Dump every NFL regular-season game (finals + DK open/close off ESPN core odds) into the job
log as gzip+base64, for offline slicing (the Claude sandbox can't reach ESPN). Nov 15 2026.

  python -m scripts.nfl_dump --seasons 2024,2025,2026 --cache .nfl_cache
"""
from __future__ import annotations

import argparse
import base64
import gzip
import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor

from scripts.cfb_spots import get, parse_item

SB = ("https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
      "?limit=100&dates={y}&seasontype=2&week={w}")
CORE = "https://sports.core.api.espn.com/v2/sports/football/leagues/nfl/events/{e}/competitions/{e}/odds"


def games(y):
    out, seen = [], set()
    for w in range(1, 19):
        d = get(SB.format(y=y, w=w)) or {}
        for ev in d.get("events") or []:
            if ev.get("id") in seen:
                continue
            seen.add(ev.get("id"))
            c = (ev.get("competitions") or [{}])[0]
            if not ((c.get("status") or {}).get("type") or {}).get("completed"):
                continue
            t = {x.get("homeAway"): x for x in c.get("competitors") or []}
            try:
                hs, as_ = float(t["home"]["score"]), float(t["away"]["score"])
            except (KeyError, TypeError, ValueError):
                continue
            out.append({"id": ev["id"], "season": y, "week": w, "date": ev.get("date"),
                        "neutral": bool(c.get("neutralSite")),
                        "home": t["home"]["team"].get("abbreviation"), "away": t["away"]["team"].get("abbreviation"),
                        "hs": hs, "as": as_})
    return out


def odds(eid):
    b = get(CORE.format(e=eid)) or {}
    items = b.get("items") or []
    dk = [i for i in items if "draft" in str((i.get("provider") or {}).get("name") or "").lower()]
    pick = (dk or items or [None])[0]
    if not pick:
        return None
    r = parse_item(pick)
    h, a = pick.get("homeTeamOdds") or {}, pick.get("awayTeamOdds") or {}
    for k in ("open", "close"):
        mh = ((h.get(k) or {}).get("moneyLine") or {})
        ma = ((a.get(k) or {}).get("moneyLine") or {})
        from scripts.cfb_spots import _am
        r[k]["ml_home"], r[k]["ml_away"] = _am(mh), _am(ma)
    return r


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--seasons", default="2024,2025,2026")
    ap.add_argument("--cache", default=".nfl_cache")
    a = ap.parse_args()
    os.makedirs(a.cache, exist_ok=True)
    G = []
    for y in [int(s) for s in a.seasons.split(",")]:
        G += games(y)
    p = os.path.join(a.cache, "odds.json")
    have = json.load(open(p)) if os.path.exists(p) else {}
    todo = [g["id"] for g in G if not have.get(g["id"])]
    with ThreadPoolExecutor(8) as ex:
        for e, r in zip(todo, ex.map(odds, todo)):
            have[e] = r
    json.dump(have, open(p, "w"))
    for g in G:
        g["odds"] = have.get(g["id"])
    print("rows", len(G), "with odds", sum(1 for g in G if g["odds"]))
    b = base64.b64encode(gzip.compress(json.dumps(G).encode())).decode()
    print("DUMP-BEGIN")
    for i in range(0, len(b), 2000):
        print(b[i:i + 2000])
    print("DUMP-END")


if __name__ == "__main__":
    main()
