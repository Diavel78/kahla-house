"""Dump every NBA / NHL game (finals + DraftKings open/close off ESPN core odds) as one
gzip+base64 blob in the job log, for offline slicing in the Claude sandbox (which can't
reach ESPN or download artifacts). Nov 15 2026, Rob: "Do the same for NBA and NHL".

  python -m scripts.pro_dump --sport NBA --cache .nba_cache --seasons 2025,2026,2027
"""
from __future__ import annotations

import argparse
import base64
import gzip
import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor

from scripts.open_close_moves import _mod, fetch_odds


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--sport", default="NBA")
    ap.add_argument("--cache", default=".nba_cache")
    ap.add_argument("--seasons", default="2025,2026,2027")
    a = ap.parse_args()
    mod = _mod(a.sport)
    seasons = [int(x) for x in a.seasons.split(",") if x]
    games = [g for g in mod.load(a.cache, seasons) if g.get("hs") is not None]
    path = os.path.join(a.cache, f"oc_{a.sport}.json")
    have = json.load(open(path)) if os.path.exists(path) else {}
    todo = [g["id"] for g in games if g["id"] not in have or not have[g["id"]]]
    logging.info("odds: %d cached, %d to fetch", len(have), len(todo))
    with ThreadPoolExecutor(8) as ex:
        for eid, r in zip(todo, ex.map(lambda e: fetch_odds(mod, e), todo)):
            have[eid] = r
    json.dump(have, open(path, "w"))
    out = [{"id": g["id"], "date": g["date"], "season": g["season"], "home": g["home"], "away": g["away"],
            "hs": g["hs"], "as": g["as"], "odds": have.get(g["id"])} for g in games]
    print("rows", len(out), "with odds", sum(1 for r in out if r["odds"]))
    b = base64.b64encode(gzip.compress(json.dumps(out).encode())).decode()
    print("DUMP-BEGIN")
    for i in range(0, len(b), 2000):
        print(b[i:i + 2000])
    print("DUMP-END")


if __name__ == "__main__":
    main()
