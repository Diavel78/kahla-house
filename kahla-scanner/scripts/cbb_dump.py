"""Dump every Division I men's college basketball game (final + the ESPN core-odds open and
close: ML, spread, total) as one gzip+base64 blob in the job log, for offline slicing in the
Claude sandbox (which can't reach ESPN or download artifacts). Nov 15 2026, Rob: "College
basketball time" — the fire zones are measured off this, the same way as NBA/NHL.

  python -m scripts.cbb_dump --seasons 2025,2026,2027 --cache .cbb_cache

Season Y = the Nov (Y-1) .. Apr Y season. The current season's days are re-read every run.
"""
from __future__ import annotations

import argparse
import base64
import gzip
import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

from scripts.cfb_spots import get
from scripts.open_close_moves import parse_item

log = logging.getLogger("cbb_dump")
SB = ("https://site.api.espn.com/apis/site/v2/sports/basketball/mens-college-basketball/scoreboard"
      "?groups=50&limit=500&dates={d}")
CORE = ("https://sports.core.api.espn.com/v2/sports/basketball/leagues/mens-college-basketball/"
        "events/{e}/competitions/{e}/odds")


def _day(d: date) -> list:
    b = get(SB.format(d=f"{d:%Y%m%d}")) or {}
    out = []
    for ev in b.get("events") or []:
        c = (ev.get("competitions") or [{}])[0]
        if not ((c.get("status") or {}).get("type") or {}).get("completed"):
            continue
        t = {x.get("homeAway"): x for x in c.get("competitors") or []}
        if "home" not in t or "away" not in t:
            continue
        try:
            hs, as_ = float(t["home"].get("score")), float(t["away"].get("score"))
        except (TypeError, ValueError):
            continue
        out.append({"id": str(ev["id"]), "date": ev.get("date"),
                    "neutral": bool(c.get("neutralSite")), "conf": bool(c.get("conferenceCompetition")),
                    "home": (t["home"].get("team") or {}).get("displayName"),
                    "away": (t["away"].get("team") or {}).get("displayName"),
                    "hs": hs, "as": as_,
                    "stype": (ev.get("season") or {}).get("type")})
    return out


def season_games(y: int, cache: str) -> list:
    p = os.path.join(cache, f"games_{y}.json")
    today = datetime.now(timezone.utc).date()
    closed = date(y, 4, 15) < today
    if closed and os.path.exists(p):
        return json.load(open(p))
    d0, d1 = date(y - 1, 11, 1), min(date(y, 4, 10), today)
    days = [d0 + timedelta(days=i) for i in range((d1 - d0).days + 1)]
    out, seen = [], set()
    with ThreadPoolExecutor(8) as ex:
        for lst in ex.map(_day, days):
            for g in lst:
                if g["id"] not in seen:
                    seen.add(g["id"])
                    g["season"] = y
                    out.append(g)
    log.info("season %s: %d finals", y, len(out))
    if closed:
        json.dump(out, open(p, "w"))
    return out


def fetch_odds(eid):
    b = get(CORE.format(e=eid)) or {}
    items = b.get("items") or []

    def has(i):
        return bool(((i.get("homeTeamOdds") or {}).get("open") or {}).get("pointSpread")
                    or ((i.get("homeTeamOdds") or {}).get("open") or {}).get("moneyLine"))
    dk = [i for i in items if "draft" in str((i.get("provider") or {}).get("name") or "").lower()]
    pick = (next((i for i in dk if has(i)), None) or next((i for i in items if has(i)), None)
            or (dk[0] if dk else (items[0] if items else None)))
    if not pick:
        return None
    r = parse_item(pick)
    r["provider"] = (pick.get("provider") or {}).get("name")
    return r


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--seasons", default="2025,2026,2027")
    ap.add_argument("--cache", default=".cbb_cache")
    a = ap.parse_args()
    os.makedirs(a.cache, exist_ok=True)
    games = []
    for y in [int(x) for x in a.seasons.split(",") if x]:
        games += season_games(y, a.cache)
    path = os.path.join(a.cache, "odds.json")
    have = json.load(open(path)) if os.path.exists(path) else {}
    todo = [g["id"] for g in games if g["id"] not in have or not have[g["id"]]]
    log.info("odds: %d cached, %d to fetch", len(have), len(todo))
    for i in range(0, len(todo), 400):
        chunk = todo[i:i + 400]
        with ThreadPoolExecutor(8) as ex:
            for eid, r in zip(chunk, ex.map(fetch_odds, chunk)):
                have[eid] = r
        json.dump(have, open(path, "w"))
        log.info("odds %d/%d", min(i + 400, len(todo)), len(todo))
    for g in games:
        g["odds"] = have.get(g["id"])
    print("rows", len(games), "with odds", sum(1 for g in games if g["odds"]))
    b = base64.b64encode(gzip.compress(json.dumps(games).encode())).decode()
    print("DUMP-BEGIN")
    for i in range(0, len(b), 2000):
        print(b[i:i + 2000])
    print("DUMP-END")


if __name__ == "__main__":
    main()
