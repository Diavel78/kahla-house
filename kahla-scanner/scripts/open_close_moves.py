"""OPEN → CLOSE: does the way the line moves predict the result? (Nov 1 2026)

Rob: "You track the odds, you track the results… any correlation to odds
movements in time frames to outcome". Our own line tape holds every minute
but only since Oct 29 2026; ESPN's core odds API holds DraftKings' OPEN and
CLOSE for every NBA / NHL game, seasons back. This grades the coarsest
window — open → close — on thousands of games:

  move   = cents the close sits from the open on the HOME / OVER side, at one
           line (a line move counts as the price it takes to hold the old
           number: `SLOPE` cents a point, the steam scan's scale).
  at the CLOSE: follow the move with the closing line + price — is the late
           chase worth anything once the market has finished moving?
  at the OPEN:  had you been on the side the market was about to move to —
           the ceiling an early read could reach.

Spread / total: won-lost (pushes out), against the line in question. ML: the
moved-to side's result minus its devigged price (cents a bet; 0 = fair).
Reads the nba_fetch / nhl_fetch caches for the game list + finals; the odds
are cached in <cache>/oc_<sport>.json (resumable).

  python -m scripts.open_close_moves --sport NBA --cache .nba_cache --seasons 2024,2025,2026
  python -m scripts.open_close_moves --sport NHL --cache .nhl_cache --probe 401559256
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor

log = logging.getLogger("open_close_moves")

SLOPE = {("NBA", "spread"): 3.0, ("NBA", "total"): 2.2, ("NBA", "ml"): 1.0,
         ("NHL", "puck"): 1.0, ("NHL", "total"): 17.0, ("NHL", "ml"): 1.0}
BUCKETS = ((0.0, 1.0), (1.0, 2.0), (2.0, 4.0), (4.0, 8.0), (8.0, 999.0))


def _mod(sport):
    if sport == "NBA":
        from scripts import nba_fetch as m
    else:
        from scripts import nhl_fetch as m
    return m


def _num(x):
    try:
        s = str(x).strip().lower().replace("+", "")
        if s in ("even", "ev", "pk", "pick"):
            return 100.0
        s = s.lstrip("ou")
        return float(s) if s not in ("", "none") else None
    except (TypeError, ValueError):
        return None


def _am(d):
    """An ESPN odds leaf → number: {"american": "-110"} / {"value": ..} / "-110"."""
    if isinstance(d, dict):
        for k in ("american", "alternateDisplayValue", "displayValue", "value", "odds"):
            v = _num(d.get(k))
            if v is not None:
                return v
        return None
    return _num(d)


def parse_item(it: dict) -> dict:
    """One core-odds item → {open: {...}, close: {...}}: ml_home, ml_away,
    sp_home (home line), sp_home_odds, sp_away_odds, total, over, under."""
    h, a = it.get("homeTeamOdds") or {}, it.get("awayTeamOdds") or {}
    out = {}
    for k in ("open", "close"):
        ho, ao, go = h.get(k) or {}, a.get(k) or {}, it.get(k) or {}
        tot = _am(go.get("total"))
        out[k] = {"ml_home": _am(ho.get("moneyLine")), "ml_away": _am(ao.get("moneyLine")),
                  "sp_home": _am(ho.get("pointSpread")),
                  "sp_home_odds": _am(ho.get("spread")), "sp_away_odds": _am(ao.get("spread")),
                  "total": abs(tot) if tot is not None else None,
                  "over": _am(go.get("over")), "under": _am(go.get("under"))}
    return out


def fetch_odds(mod, eid):
    b = mod._get(mod.CORE.format(e=eid))
    items = (b or {}).get("items") or []
    def has_open(i):
        return bool(((i.get("homeTeamOdds") or {}).get("open") or {}).get("moneyLine"))
    dk = [i for i in items if "draft" in str((i.get("provider") or {}).get("name") or "").lower()]
    pick = (next((i for i in dk if has_open(i)), None) or next((i for i in items if has_open(i)), None)
            or (dk[0] if dk else (items[0] if items else None)))
    if not pick:
        return None
    r = parse_item(pick)
    r["provider"] = (pick.get("provider") or {}).get("name")
    return r


def imp(a):
    if a is None or a == 0:
        return None
    return 100.0 / (a + 100.0) if a > 0 else -a / (-a + 100.0)


def devig(a, b):
    pa, pb = imp(a), imp(b)
    if not pa or not pb:
        return None
    return pa / (pa + pb) * 100.0


def market_reads(sport: str, o: dict):
    """{mt: (number, line, price_home_or_over_c)} for one side of the odds
    (open or close). number is on the steam scale (points, ML in cents)."""
    out = {}
    p = devig(o.get("ml_home"), o.get("ml_away"))
    if p is not None:
        out["ml"] = (p, 0.0, p)
    sk = "puck" if sport == "NHL" else "spread"
    if o.get("sp_home") is not None:
        p = devig(o.get("sp_home_odds"), o.get("sp_away_odds"))
        if p is None:
            p = 50.0
        out[sk] = (-o["sp_home"] + (p - 50.0) / SLOPE[(sport, sk)], o["sp_home"], p)
    if o.get("total") is not None:
        p = devig(o.get("over"), o.get("under"))
        if p is None:
            p = 50.0
        out["total"] = (o["total"] + (p - 50.0) / SLOPE[(sport, "total")], o["total"], p)
    return out


def grade(mt, sgn, line, price_c, hs, as_):
    """+1 / −1 / None(push) for spread/total; for ML the moved-to side's
    result minus its price, in cents (home price_c is the home's)."""
    if mt == "ml":
        win_home = 1.0 if hs > as_ else 0.0
        return ((win_home - price_c / 100.0) if sgn > 0 else ((1.0 - win_home) - (1.0 - price_c / 100.0))) * 100.0
    d = (hs + as_) - line if mt == "total" else (hs - as_) + line
    if abs(d) < 1e-9:
        return None
    return 1.0 if (d > 0) == (sgn > 0) else -1.0


def analyse(sport: str, games: list, odds: dict) -> dict:
    res = {}
    for mt in (("ml", "puck", "total") if sport == "NHL" else ("ml", "spread", "total")):
        sl = SLOPE[(sport, mt)]
        rows = []
        for g in games:
            od = odds.get(g["id"])
            if not od or g.get("hs") is None or g.get("as") is None:
                continue
            ro, rc = market_reads(sport, od.get("open") or {}), market_reads(sport, od.get("close") or {})
            if mt not in ro or mt not in rc:
                continue
            mv = (rc[mt][0] - ro[mt][0]) * sl
            if abs(mv) < 1e-9:
                rows.append((0.0, None, None, g.get("season")))
                continue
            sgn = 1 if mv > 0 else -1
            at_close = grade(mt, sgn, rc[mt][1], rc[mt][2], g["hs"], g["as"])
            at_open = grade(mt, sgn, ro[mt][1], ro[mt][2], g["hs"], g["as"])
            rows.append((mv, at_close, at_open, g.get("season")))
        moved = [r for r in rows if r[1] is not None or r[2] is not None]
        out = {"games": len(rows), "moved": len(moved),
               "no_move": sum(1 for r in rows if r[0] == 0.0)}

        def summ(sel, idx):
            v = [r[idx] for r in sel if r[idx] is not None]
            if not v:
                return None
            if mt == "ml":
                return f"n{len(v)} {sum(v) / len(v):+.1f}c/bet"
            w = sum(1 for x in v if x > 0)
            return f"{w}-{len(v) - w} ({w / len(v) * 100:.1f}%)"
        bks = []
        for lo, hi in BUCKETS:
            sel = [r for r in moved if lo <= abs(r[0]) < hi]
            bks.append({"move_c": f"{lo:g}-{hi:g}" if hi < 999 else f"{lo:g}+", "n": len(sel),
                        "follow_at_close": summ(sel, 1), "follow_at_open": summ(sel, 2)})
        out["buckets"] = bks
        out["all_moved"] = {"follow_at_close": summ(moved, 1), "follow_at_open": summ(moved, 2)}
        big = [r for r in moved if abs(r[0]) >= 2.0]
        out["by_season_2c_plus"] = {str(s): summ([r for r in big if r[3] == s], 1)
                                    for s in sorted({r[3] for r in big})}
        res[mt] = out
    return res


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--sport", default="NBA", choices=("NBA", "NHL"))
    ap.add_argument("--cache", default=".nba_cache")
    ap.add_argument("--seasons", default="2024,2025,2026")
    ap.add_argument("--probe", default="", help="an ESPN event id: print its raw odds item + parse")
    a = ap.parse_args()
    mod = _mod(a.sport)
    if a.probe:
        b = mod._get(mod.CORE.format(e=a.probe)) or {}
        for it in (b.get("items") or [])[:2]:
            print("RAW", json.dumps(it)[:2500])
            print("PARSED", json.dumps(parse_item(it)))
        # ESPN's per-provider line history, if it carries one (timestamps = intraday windows)
        base = mod.CORE.format(e=a.probe).split("?")[0].rstrip("/")
        for it in (b.get("items") or [])[:6]:
            pid = (it.get("provider") or {}).get("id")
            print("PROVIDER", pid, (it.get("provider") or {}).get("name"),
                  sorted(k for k in it.keys()))
            for path in (f"/{pid}/history/0/movement?limit=200", f"/{pid}/history/0?limit=200", f"/{pid}/history"):
                h = mod._get(base + path)
                print("HIST", pid, path, json.dumps(h)[:1500] if h else None)
        return 0
    seasons = [int(x) for x in a.seasons.split(",") if x]
    games = [g for g in mod.load(a.cache, seasons) if g.get("hs") is not None]
    log.info("%s: %d finished games in %s", a.sport, len(games), seasons)
    path = os.path.join(a.cache, f"oc_{a.sport}.json")
    have = json.load(open(path)) if os.path.exists(path) else {}
    todo = [g["id"] for g in games if g["id"] not in have]
    log.info("odds: %d cached, %d to fetch", len(have), len(todo))
    for i in range(0, len(todo), 400):
        chunk = todo[i:i + 400]
        with ThreadPoolExecutor(8) as ex:
            for eid, r in zip(chunk, ex.map(lambda e: fetch_odds(mod, e), chunk)):
                have[eid] = r
        json.dump(have, open(path, "w"))
        log.info("odds: %d / %d", min(i + 400, len(todo)), len(todo))
    filled = sum(1 for g in games if have.get(g["id"]) and (have[g["id"]].get("open") or {}).get("ml_home"))
    log.info("games with an OPEN moneyline: %d / %d", filled, len(games))
    rep = analyse(a.sport, games, have)
    print("\n=== %s OPEN → CLOSE (DraftKings via ESPN), seasons %s ===" % (a.sport, seasons))
    print(json.dumps(rep, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
