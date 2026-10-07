"""Hoops IQ walk-forward backtest — ML / spread / total vs the DK close.

Predict-then-update on every regular-season game. Calibration coefficients
are refit each game date from PRIOR predictions only (≥ MIN_CAL pairs from
the last CAL_DAYS of season time). Season 2023 is warmup; eval = 2024+.

Reports per eval season + pooled:
  ML      — Brier / accuracy of P(home win) vs the DK closing ML (devigged)
  SPREAD  — Brier of P(home covers the DK close), pushes dropped
  TOTAL   — Brier of P(over the DK close), pushes dropped
  plus the same three Briers for the MARKET (devigged ML; 0.5 for the
  spread/total at their own number — the honest coin-flip floor).

Usage:
  python -m scripts.backtest_nba_iq --cache .nba_cache [--sweep] [--json]
"""
from __future__ import annotations

import argparse
import datetime as dt
import itertools
import json
import logging
import math
import sys

from _lib.nba_iq import HoopsIQ, calib_fit, calib_pred, ncdf, poss_of
from scripts.nba_fetch import load

log = logging.getLogger("bt_nba")
MIN_CAL = 400
CAL_DAYS = 2 * 175          # two seasons of season-time
WARMUP_SEASON = 2023
FEATS = {
    "base":  (["raw_m"], ["raw_t"]),
    "avail": (["raw_m", "d_val", "d_min"], ["raw_t", "s_pts", "s_min"]),
}


def _day(iso):
    d = dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
    # US game night: shift −10h so a 7:30pm ET tip (23:30Z/00:30Z) is one day
    return (d - dt.timedelta(hours=10)).toordinal()


def _imp(ml):
    if ml is None:
        return None
    return 100 / (ml + 100) if ml > 0 else -ml / (-ml + 100)


def run(rows, params, feats="avail", collect=False):
    m = HoopsIQ(**params)
    season_start = {}
    pairs = []        # calibration pairs + eval records
    preds = []
    i = 0
    by_day = []
    for r in rows:
        d = _day(r["date"])
        if not by_day or by_day[-1][0] != d:
            by_day.append((d, []))
        by_day[-1][1].append(r)
    fm, ft = FEATS[feats]
    for d, games in by_day:
        season = games[0]["season"]
        st = HoopsIQ.season_time(season, d, season_start)
        f = m.fit(st, season)
        cal = [p for p in pairs if st - p["st"] <= CAL_DAYS and p["st"] < st]
        cm = ct = None
        if f is not None and len(cal) >= MIN_CAL:
            cm = calib_fit(cal, fm, "act_m")
            ct = calib_fit(cal, ft, "act_t")
        staged = []
        for g in games:
            hb = 1.0 if d - m.last_date.get(g["home"], -99) == 1 else 0.0
            ab = 1.0 if d - m.last_date.get(g["away"], -99) == 1 else 0.0
            poss = None
            bx = g.get("box") or {}
            if bx.get("home") and bx.get("away"):
                p1, p2 = poss_of(bx["home"]), poss_of(bx["away"])
                if p1 and p2:
                    poss = (p1 + p2) / 2
            if f is not None:
                pr = m.project(f, g["home"], g["away"], hb, ab)
                pl = g.get("players") or {}
                played = {s: {x[0] for x in (pl.get(s) or []) if x[2] and x[2] > 0}
                          for s in ("home", "away")}
                av_h = m.availability(g["home"], d, played["home"])
                av_a = m.availability(g["away"], d, played["away"])
                rec = {"st": st, "season": season, "id": g["id"],
                       "raw_m": pr["margin"], "raw_t": pr["total"],
                       "d_val": av_a["miss_val"] - av_h["miss_val"],
                       "d_min": av_a["miss_min"] - av_h["miss_min"],
                       "s_pts": av_h["miss_pts"] + av_a["miss_pts"],
                       "s_min": av_h["miss_min"] + av_a["miss_min"],
                       "act_m": g["hs"] - g["as"], "act_t": g["hs"] + g["as"],
                       "line": g.get("line")}
                if cm is not None:
                    mu = calib_pred(cm[0], rec, fm); sd = cm[1]
                    tu = calib_pred(ct[0], rec, ft); tsd = ct[1]
                    rec.update(mu=mu, sd=sd, tu=tu, tsd=tsd)
                    preds.append(rec)
                if season >= WARMUP_SEASON and d - season_start[season] > 21:
                    staged.append(rec)
            if poss:
                staged_add = (st, season, d, g, poss, hb, ab)
                m.add(*staged_add)
        pairs.extend(staged)
        i += len(games)
    return preds


def score(preds, seasons):
    out = {}
    P = [p for p in preds if p["season"] in seasons]
    ml = {"n": 0, "b": 0.0, "acc": 0, "mb": 0.0, "mn": 0}
    sp = {"n": 0, "b": 0.0, "hit": 0}
    tt = {"n": 0, "b": 0.0, "hit": 0}
    lad = {"n": 0, "b": 0.0}
    cal = [[0, 0.0, 0] for _ in range(10)]
    for p in P:
        y = 1.0 if p["act_m"] > 0 else 0.0
        q = ncdf(p["mu"] / p["sd"])
        ml["n"] += 1; ml["b"] += (q - y) ** 2; ml["acc"] += (q > 0.5) == (y == 1)
        b = min(9, int(q * 10)); cal[b][0] += 1; cal[b][1] += q; cal[b][2] += y
        L = p.get("line") or {}
        ih, ia = _imp(L.get("ml_home")), _imp(L.get("ml_away"))
        if ih and ia:
            mq = ih / (ih + ia)
            ml["mn"] += 1; ml["mb"] += (mq - y) ** 2
        s = L.get("spread")
        if s is not None and p["act_m"] + s != 0:
            yc = 1.0 if p["act_m"] + s > 0 else 0.0
            qc = ncdf((p["mu"] + s) / p["sd"])
            sp["n"] += 1; sp["b"] += (qc - yc) ** 2; sp["hit"] += (qc > 0.5) == (yc == 1)
        t = L.get("total")
        if t is not None and p["act_t"] != t:
            yo = 1.0 if p["act_t"] > t else 0.0
            qo = ncdf((p["tu"] - t) / p["tsd"])
            tt["n"] += 1; tt["b"] += (qo - yo) ** 2; tt["hit"] += (qo > 0.5) == (yo == 1)
            # ladder: the venue lists rungs ±3..±15 around the number
            for off in (-15, -10, -6, -3, 3, 6, 10, 15):
                rung = t + off + 0.5 * (1 if (t + off) == int(t + off) else 0)
                yr = 1.0 if p["act_t"] > rung else 0.0
                qr = ncdf((p["tu"] - rung) / p["tsd"])
                lad["n"] += 1; lad["b"] += (qr - yr) ** 2
    f = lambda a, k="b", n="n": round(a[k] / a[n], 4) if a[n] else None
    out["ml"] = {"n": ml["n"], "brier": f(ml), "acc": round(ml["acc"] / ml["n"], 4) if ml["n"] else None,
                 "market_brier": f(ml, "mb", "mn"), "market_n": ml["mn"]}
    out["spread"] = {"n": sp["n"], "brier": f(sp), "hit": round(sp["hit"] / sp["n"], 4) if sp["n"] else None}
    out["total"] = {"n": tt["n"], "brier": f(tt), "hit": round(tt["hit"] / tt["n"], 4) if tt["n"] else None,
                    "ladder_brier": f(lad)}
    out["ml_calib"] = [(round(c[1] / c[0], 3), round(c[2] / c[0], 3), c[0]) for c in cal if c[0]]
    return out


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", default=".nba_cache")
    ap.add_argument("--seasons", default="2023,2024,2025,2026")
    ap.add_argument("--sweep", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    rows = load(a.cache, [int(x) for x in a.seasons.split(",")])
    n_line = sum(1 for r in rows if r.get("line"))
    n_box = sum(1 for r in rows if (r.get("box") or {}).get("home"))
    log.info("loaded %d games (%d with line, %d with box)", len(rows), n_line, n_box)
    base = dict(hl_days=40.0, carry=0.6, ridge_eff=60.0, ridge_pace=60.0)
    report = {"games": len(rows), "with_line": n_line, "with_box": n_box, "runs": []}
    configs = [("base", base, "base"), ("avail", base, "avail")]
    if a.sweep:
        for hl, cr, rg in itertools.product((70.0, 110.0, 160.0), (0.7, 0.85), (4.0, 10.0, 25.0)):
            configs.append((f"hl{hl:.0f}_c{cr}_r{rg:.0f}",
                            dict(hl_days=hl, carry=cr, ridge_eff=rg, ridge_pace=rg), "avail"))
    for name, params, feats in configs:
        preds = run(rows, params, feats)
        res = {"name": name, "params": params, "feats": feats,
               "select_2024": score(preds, {2024}),
               "eval_2025_26": score(preds, {2025, 2026}),
               "by_season": {s: score(preds, {s})["ml"] for s in (2024, 2025, 2026)}}
        report["runs"].append(res)
        e = res["eval_2025_26"]
        log.info("%-22s sel ML %.4f | eval ML %s (mkt %s) acc %s | SPR %s | TOT %s ladder %s",
                 name, res["select_2024"]["ml"]["brier"] or -1, e["ml"]["brier"],
                 e["ml"]["market_brier"], e["ml"]["acc"], e["spread"]["brier"],
                 e["total"]["brier"], e["total"]["ladder_brier"])
    if a.sweep:
        best = min(report["runs"], key=lambda r: r["select_2024"]["ml"]["brier"] or 9)
        report["best_by_2024"] = best["name"]
        log.info("BEST (selected on 2024 ML Brier): %s → eval %s", best["name"],
                 json.dumps(best["eval_2025_26"]))
    print("NBA_BT_REPORT " + json.dumps(report, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
