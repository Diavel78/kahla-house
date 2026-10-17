"""NHL TOTALS vs the market — walk-forward (Oct 17 2026, Rob: "if Vegas can
model the over-under… we have access to the same data").

Crease IQ v2's total was printed with "no backtested edge" and never graded
against the number it bets into. This scores it against DraftKings' CLOSING
total (ESPN core odds, over/under prices devigged) the way a bet settles:
at the market's own line, pushes refunded.

Per held-out season it reports
  * LEVEL: mean model total vs the DK line vs what happened, by month —
    a model whose level trails the league's scoring can only pick unders;
  * Brier at the DK line (P(over | no push)) for the model, the level-fixed
    variants and the no-vig market;
  * MARKET + MODEL: a logistic on logit(p_market) + logit(p_model), fit on
    prior seasons only — does the model add anything the close lacks? —
    and what betting its edge at the closing price would have returned.

  python -m scripts.backtest_crease_totals --cache .nhl_cache
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _lib import crease_iq2 as ci2  # noqa: E402
from scripts.backtest_crease_avail import espn_day, fit_logit, logit, nhl  # noqa: E402
from scripts.nba_fetch import _get, _num  # noqa: E402
from scripts.nhl_fetch import CORE  # noqa: E402
from scripts.nhl_fetch import load as load_espn  # noqa: E402

log = logging.getLogger("backtest_crease_totals")
SEASONS = ("2023-09-01", "2024-09-01", "2025-09-01")
# league-level trackers: (half-life days, prior weight in team-games)
LEVELS = {"lvl14": (14.0, 60.0), "lvl30": (30.0, 120.0), "lvl60": (60.0, 240.0)}


# ------------------------------------------------------------- market
def _am(v):
    """An American price from '+105' / '-120' / 'EVEN' / a number; None otherwise."""
    if v is None:
        return None
    sv = str(v).strip().upper()
    if sv in ("EVEN", "EV"):
        return 100.0
    try:
        x = float(sv.replace("+", ""))
    except ValueError:
        return None
    return x if abs(x) >= 100 else None


def _odds(side):
    if not isinstance(side, dict):
        return _am(side)
    for k in ("american", "alternateDisplayValue", "odds", "displayValue"):
        v = _am(side.get(k))
        if v:
            return v
    return None


def _side_line(blk):
    tl = (blk or {}).get("total") or {}
    for k in ("american", "alternateDisplayValue", "displayValue", "value"):
        v = _num(str(tl.get(k) or "").lstrip("ouOU"))
        if v and 3.0 <= abs(v) <= 10.0:
            return abs(v)
    return None


def core_total(eid):
    """DK's closing total + over/under prices for one event, and the OPEN."""
    b = _get(CORE.format(e=eid))
    for it in (b or {}).get("items") or []:
        cl, op = it.get("close") or {}, it.get("open") or {}
        line = _side_line(cl) or _num(it.get("overUnder"))
        ov = _odds(cl.get("over")) or _am(it.get("overOdds"))
        un = _odds(cl.get("under")) or _am(it.get("underOdds"))
        if line and ov and un:
            return {"line": line, "over": ov, "under": un,
                    "o_line": _side_line(op), "o_over": _odds(op.get("over")),
                    "o_under": _odds(op.get("under")),
                    "provider": (it.get("provider") or {}).get("name")}
    return None


def market_totals(cache, games):
    path = os.path.join(cache, "nhl_totals_v2.json")
    have = json.load(open(path)) if os.path.exists(path) else {}
    todo = [g["id"] for g in games if g["id"] not in have]
    log.info("market totals: %d cached, %d to fetch", len(have), len(todo))
    if todo:
        with ThreadPoolExecutor(8) as ex:
            for eid, r in zip(todo, ex.map(core_total, todo)):
                have[eid] = r
        json.dump(have, open(path, "w"))
    return have


def imp(ml):
    return 100 / (ml + 100) if ml > 0 else -ml / (-ml + 100)


def payout(ml):
    return ml / 100 if ml > 0 else 100 / -ml


# -------------------------------------------------------------- walk
def walk(cache):
    gp, sp = os.path.join(cache, "goalies.json"), os.path.join(cache, "shots.json")
    if os.path.exists(gp) and os.path.exists(sp):
        goalies, shots = json.load(open(gp)), json.load(open(sp))
    else:
        from scripts.nhl_sheet_data import load_goalie_rows, load_shot_rows
        goalies, shots = load_goalie_rows(), load_shot_rows()
        json.dump(goalies, open(gp, "w"))
        json.dump(shots, open(sp, "w"))
    games = ci2.build_games(goalies, shots)
    log.info("crease: %d finished games", len(games))
    out = {}
    lines = (4.5, 5.0, 5.5, 6.0, 6.5, 7.0, 7.5)
    for i, start in enumerate(SEASONS):
        end = SEASONS[i + 1] if i + 1 < len(SEASONS) else "2099-01-01"
        train = [g for g in games if g["date"] < start]
        st, params = ci2.fit(train)
        # the level the intercept learned: mean non-EN regulation goals a team-game
        base = (sum((g["reg_h"] - g["en_h"]) + (g["reg_a"] - g["en_a"]) for g in train)
                / (2 * len(train)))
        lv = {k: [0.0, 0.0, None] for k in LEVELS}     # decayed sum, weight, last day
        for g in train:                                 # warm the trackers on history
            _lv_add(lv, g)
        log.info("season %s: base %.3f goals/team-game, n_fit %d", start[:4], base, params["n_fit"])
        for g in games:
            if not (start <= g["date"] < end):
                continue
            d = date.fromisoformat(g["date"])
            pr = ci2.price(st, params, g["home"], g["away"], d, g["starter_h"], g["starter_a"],
                           totals=lines)
            if pr:
                rec = {"season": start[:4], "date": g["date"], "pr": pr,
                       "final_t": g["final_h"] + g["final_a"], "lv": {}}
                for k, (hl, pw) in LEVELS.items():
                    s, w, last = lv[k]
                    f = 1.0 if last is None else 0.5 ** ((d - last).days / hl)
                    rate = (s * f + base * pw) / (w * f + pw)
                    p2 = dict(params, coef=[params["coef"][0] + math.log(rate / base)]
                              + list(params["coef"][1:]))
                    rec["lv"][k] = ci2.price(st, p2, g["home"], g["away"], d,
                                             g["starter_h"], g["starter_a"], totals=lines)
                    if k == "lvl30":
                        # NO GOALIE NEWS: the open is often posted before the
                        # starters are known — price with league-average goalies
                        rec["lv"]["nogk30"] = ci2.price(st, p2, g["home"], g["away"], d,
                                                        None, None, totals=lines)
                out[(g["date"], g["home"], g["away"])] = rec
            st.update(g)
            _lv_add(lv, g)
    return out


def _lv_add(lv, g):
    d = date.fromisoformat(g["date"])
    v = (g["reg_h"] - g["en_h"]) + (g["reg_a"] - g["en_a"])
    for k, (hl, _pw) in LEVELS.items():
        s, w, last = lv[k]
        f = 1.0 if last is None else 0.5 ** ((d - last).days / hl)
        lv[k] = [s * f + v, w * f + 2.0, d]


def p_over(pr, line):
    """P(over | no push) at any line the price covered."""
    return ci2.over_prob(pr, line)


# -------------------------------------------------------------- main
def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", default=".nhl_cache")
    ap.add_argument("--seasons", default="2023,2024,2025,2026")
    ap.add_argument("--probe", default="", help="an ESPN event id: print its raw odds item")
    a = ap.parse_args()
    if a.probe:
        b = _get(CORE.format(e=a.probe)) or {}
        for it in (b.get("items") or [])[:2]:
            print("ITEM", json.dumps({k: it.get(k) for k in it if k not in ("links",)})[:3000])
        print("PARSED", core_total(a.probe))
        return 0
    cp = walk(a.cache)
    espn = load_espn(a.cache, [int(x) for x in a.seasons.split(",")])
    mk = market_totals(a.cache, espn)

    rows, opens = [], []
    for g in espn:
        d = espn_day(g["date"])
        h, w = nhl(g["home"]), nhl(g["away"])
        key = next((k for k in ((d.isoformat(), h, w), ((d + timedelta(1)).isoformat(), h, w),
                                ((d - timedelta(1)).isoformat(), h, w)) if k in cp), None)
        m = mk.get(g["id"])
        if not key or not m:
            continue
        L = float(m["line"])
        if L not in (4.5, 5.0, 5.5, 6.0, 6.5, 7.0, 7.5):
            continue
        rec = cp[key]
        t = rec["final_t"]
        OL = m.get("o_line")
        if OL in (4.5, 5.0, 5.5, 6.0, 6.5, 7.0, 7.5) and m.get("o_over") and m.get("o_under") and t != OL:
            oo, ou = imp(m["o_over"]), imp(m["o_under"])
            opens.append({"season": rec["season"], "line": OL, "close": L, "y": 1.0 if t > OL else 0.0,
                          "pm": oo / (oo + ou), "over": m["o_over"], "under": m["o_under"],
                          "c_over": m["over"], "c_under": m["under"],
                          "p": p_over(rec["pr"], OL),
                          **{k: p_over(v, OL) for k, v in rec["lv"].items() if v}})
        if t == L:
            continue                              # a push settles nothing
        po_m, pu_m = imp(m["over"]), imp(m["under"])
        rows.append({"season": rec["season"], "month": rec["date"][:7], "line": L,
                     "y": 1.0 if t > L else 0.0, "t": t,
                     "pm": po_m / (po_m + pu_m), "over": m["over"], "under": m["under"],
                     "exp": rec["pr"]["exp_total"], "p": p_over(rec["pr"], L),
                     **{k: p_over(v, L) for k, v in rec["lv"].items() if v},
                     **{"exp_" + k: v["exp_total"] for k, v in rec["lv"].items() if v}})
    log.info("joined rows (no push, priced, DK close): %d", len(rows))
    report = {"n": len(rows)}

    def br(ps, ys):
        return round(float(np.mean((np.asarray(ps) - np.asarray(ys)) ** 2)), 4)

    print("\n=== LEVEL (mean total: model / variants / DK line / actual) ===")
    by = defaultdict(list)
    for r in rows:
        by[(r["season"], r["month"])].append(r)
    for k in sorted(by):
        rs = by[k]
        line = "  %s %s n=%3d model %.2f %s line %.2f actual %.2f over%% %.0f mkt %.0f" % (
            k[0], k[1], len(rs), np.mean([r["exp"] for r in rs]),
            " ".join("%s %.2f" % (v, np.mean([r["exp_" + v] for r in rs])) for v in LEVELS),
            np.mean([r["line"] for r in rs]), np.mean([r["t"] for r in rs]),
            100 * np.mean([r["y"] for r in rs]), 100 * np.mean([r["pm"] for r in rs]))
        print(line)

    print("\n=== BRIER at the DK line, no pushes (lower is better) ===")
    seasons = sorted({r["season"] for r in rows})
    for s in seasons + ["ALL"]:
        rs = [r for r in rows if s in ("ALL", r["season"])]
        ys = [r["y"] for r in rs]
        out = {"n": len(rs), "coin": br([0.5] * len(rs), ys), "market": br([r["pm"] for r in rs], ys),
               "model": br([r["p"] for r in rs], ys)}
        for v in LEVELS:
            out[v] = br([r[v] for r in rs], ys)
        report[s] = out
        print("  %s %s" % (s, json.dumps(out)))

    print("\n=== MARKET + MODEL (fit on prior seasons, scored on the next) ===")
    combo = {}
    for i, s in enumerate(seasons):
        tr = [r for r in rows if r["season"] in seasons[:i]]
        te = [r for r in rows if r["season"] == s]
        if len(tr) < 400 or not te:
            continue
        for v in ["p"] + list(LEVELS):
            X = [[1.0, logit(r["pm"]), logit(r[v])] for r in tr]
            b = fit_logit(X, [r["y"] for r in tr], ridge=1.0)
            q = [1 / (1 + math.exp(-(b[0] + b[1] * logit(r["pm"]) + b[2] * logit(r[v])))) for r in te]
            res = {"coef": [round(x, 3) for x in b], "brier": br(q, [r["y"] for r in te]),
                   "market": br([r["pm"] for r in te], [r["y"] for r in te])}
            for thr in (2.0, 4.0, 6.0):
                n = u = 0.0
                for r, qq in zip(te, q):
                    eo = (qq - imp(r["over"])) * 100
                    eu = ((1 - qq) - imp(r["under"])) * 100
                    if max(eo, eu) < thr:
                        continue
                    side_over = eo >= eu
                    ml = r["over"] if side_over else r["under"]
                    win = (r["y"] == 1.0) == side_over
                    n += 1
                    u += payout(ml) if win else -1.0
                res[f"bet>={thr:g}pp"] = {"n": int(n), "units": round(u, 2),
                                          "roi": round(u / n, 3) if n else None}
            combo[f"{s}:{v}"] = res
            print("  %s %s %s" % (s, v, json.dumps(res)))
    report["combo"] = combo

    # BETTING THE OPEN: the model's side at the opening number and price,
    # graded at the result. CLV = did the close move toward the bet (line,
    # else price)? An edge only exists where the market has not caught up.
    print("\n=== BETTING THE OPEN (n=%d games with an open total) ===" % len(opens))
    op_rep = {}
    for s in seasons + ["ALL"]:
        rs = [r for r in opens if s in ("ALL", r["season"])]
        if not rs:
            continue
        ys = [r["y"] for r in rs]
        out = {"n": len(rs), "market_open": br([r["pm"] for r in rs], ys),
               "model": br([r["p"] for r in rs], ys), "lvl30": br([r["lvl30"] for r in rs], ys),
               "nogk30": br([r["nogk30"] for r in rs], ys)}
        for v in ("p", "lvl30", "nogk30"):
            for thr in (3.0, 5.0, 8.0):
                n = u = clv = 0.0
                for r in rs:
                    eo = (r[v] - imp(r["over"])) * 100
                    eu = ((1 - r[v]) - imp(r["under"])) * 100
                    if max(eo, eu) < thr:
                        continue
                    over = eo >= eu
                    ml = r["over"] if over else r["under"]
                    n += 1
                    u += payout(ml) if (r["y"] == 1.0) == over else -1.0
                    if r["close"] != r["line"]:
                        clv += 1.0 if (r["close"] > r["line"]) == over else -1.0
                    else:
                        c = imp(r["c_over"] if over else r["c_under"]) - imp(ml)
                        clv += 1.0 if c > 0.005 else (-1.0 if c < -0.005 else 0.0)
                out[f"{v}>={thr:g}pp"] = {"n": int(n), "units": round(u, 2),
                                          "roi": round(u / n, 3) if n else None,
                                          "clv_net": int(clv)}
        op_rep[s] = out
        print("  %s %s" % (s, json.dumps(out)))
    report["open"] = op_rep
    print("CREASE_TOTALS_REPORT " + json.dumps(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
