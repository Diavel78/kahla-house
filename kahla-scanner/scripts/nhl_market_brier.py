"""How good is VEGAS at hockey? The ceiling any NHL model is graded against.

Pulls every completed regular-season NHL game for the given ESPN seasons,
DraftKings' CLOSING moneyline from ESPN's core odds API, devigs it, and
scores it exactly like our models are scored (Brier on home win, OT/SO
included — a moneyline settles on the final). Read-only, no DB.

  python -m scripts.nhl_market_brier --seasons 2024,2025,2026
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from concurrent.futures import ThreadPoolExecutor

from scripts.nba_fetch import _amer, _get, _num

SB = "https://site.api.espn.com/apis/site/v2/sports/hockey/nhl/scoreboard"
CORE = ("https://sports.core.api.espn.com/v2/sports/hockey/leagues/nhl/"
        "events/{e}/competitions/{e}/odds")


def games(season):
    days = [dt.date(season - 1, 10, 1) + dt.timedelta(d) for d in range(215)]

    def one(day):
        b = _get(SB, {"dates": day.strftime("%Y%m%d"), "limit": 50}) or {}
        out = []
        for ev in b.get("events") or []:
            s = ev.get("season") or {}
            if s.get("type") != 2 or s.get("year") != season:
                continue
            if not ((ev.get("status") or {}).get("type") or {}).get("completed"):
                continue
            comp = (ev.get("competitions") or [{}])[0]
            sc = {c.get("homeAway"): _num(c.get("score")) for c in comp.get("competitors") or []}
            if sc.get("home") is None or sc.get("away") is None:
                continue
            out.append((ev["id"], sc["home"] > sc["away"]))
        return out

    with ThreadPoolExecutor(8) as ex:
        return {e: w for L in ex.map(one, days) for e, w in L}


def close_ml(eid):
    b = _get(CORE.format(e=eid)) or {}
    for it in b.get("items") or []:
        h = ((it.get("homeTeamOdds") or {}).get("close") or {}).get("moneyLine")
        a = ((it.get("awayTeamOdds") or {}).get("close") or {}).get("moneyLine")
        mh, ma = _amer(h), _amer(a)
        if mh and ma:
            return mh, ma, (it.get("provider") or {}).get("name")
    return None


def imp(ml):
    return 100 / (ml + 100) if ml > 0 else -ml / (-ml + 100)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seasons", default="2024,2025,2026")
    a = ap.parse_args()
    report = {}
    for s in [int(x) for x in a.seasons.split(",")]:
        g = games(s)
        with ThreadPoolExecutor(8) as ex:
            lines = dict(zip(g, ex.map(close_ml, list(g))))
        n = b = acc = hw = 0
        cal = [[0, 0.0, 0] for _ in range(10)]
        for e, home_won in g.items():
            ln = lines.get(e)
            if not ln:
                continue
            ph, pa = imp(ln[0]), imp(ln[1])
            p = ph / (ph + pa)
            y = 1.0 if home_won else 0.0
            n += 1; b += (p - y) ** 2; acc += (p > 0.5) == home_won; hw += y
            k = min(9, int(p * 10)); cal[k][0] += 1; cal[k][1] += p; cal[k][2] += y
        report[s] = {"games": len(g), "with_close": n,
                     "market_brier": round(b / n, 4) if n else None,
                     "market_acc": round(acc / n, 4) if n else None,
                     "home_win_rate": round(hw / n, 4) if n else None,
                     "base_rate_brier": round((hw / n) * (1 - hw / n), 4) if n else None,
                     "calib": [(round(c[1] / c[0], 3), round(c[2] / c[0], 3), c[0]) for c in cal if c[0]]}
        print(s, json.dumps(report[s]), flush=True)
    print("NHL_MARKET_REPORT " + json.dumps(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
