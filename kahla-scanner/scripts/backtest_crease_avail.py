"""Crease IQ v2 + SKATER AVAILABILITY — walk-forward vs the DK close.

Rob, Oct 7 2026: "I want bets on hockey." Crease IQ v2 sits level with
Vegas (2025-26 0.2459 vs 0.2444) but cannot see a skater scratch — a
McDavid night prices like any other. This stacks a missing-regulars layer
on top of the production engine's own predictions and asks whether it
closes the gap to, or passes, the market.

  1. Crease IQ v2 walk (identical to backtest_crease_iq2): fit on every
     game before the season, price each game before it updates.
  2. ESPN box history (scripts/nhl_fetch): per team, each skater's decayed
     TOI / points / +/-; a REGULAR (≥12 min a night, seen in the last 30
     days, still on the team) absent from tonight's box = missing.
  3. Walk-forward logistic recalibration, refit each date from PRIOR
     games only:  logit P(home) ~ logit(crease) + Δmissing features.
  4. Graded: Brier, and flat-stake ROI of the sheet's PLAY rule (edge
     4-12pp vs the no-vig DK close) at the DK closing price.

  python -m scripts.backtest_crease_avail --cache .nhl_cache
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _lib import crease_iq2 as ci2  # noqa: E402
from scripts.nhl_fetch import load as load_espn  # noqa: E402

log = logging.getLogger("bt_crease_avail")
SEASONS = ("2023-09-01", "2024-09-01", "2025-09-01")
_ESPN_TO_NHL = {"LA": "LAK", "NJ": "NJD", "SJ": "SJS", "TB": "TBL",
                "UTAH": "UTA", "UTA": "UTA", "WAS": "WSH", "MON": "MTL",
                "NAS": "NSH", "CLB": "CBJ", "VEG": "VGK", "LV": "VGK"}
MIN_CAL = 400


def nhl(code):
    return _ESPN_TO_NHL.get(code, code)


def espn_day(iso):
    d = datetime.fromisoformat(iso.replace("Z", "+00:00")) - timedelta(hours=10)
    return d.date()


# ------------------------------------------------------- Crease IQ v2 walk
def crease_preds(cache):
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
    for i, start in enumerate(SEASONS):
        end = SEASONS[i + 1] if i + 1 < len(SEASONS) else "2099-01-01"
        st, params = ci2.fit([g for g in games if g["date"] < start])
        for g in games:
            if not (start <= g["date"] < end):
                continue
            pr = ci2.price(st, params, g["home"], g["away"], date.fromisoformat(g["date"]),
                           g["starter_h"], g["starter_a"])
            if pr:
                out[(g["date"], g["home"], g["away"])] = {
                    "season": start[:4], "p": pr["win_home"], "tot": pr["exp_total"],
                    "y": g["home_won"], "final_t": g["final_h"] + g["final_a"]}
            st.update(g)
    return out


# --------------------------------------------------- skater availability
class Avail:
    def __init__(self, hl_games=15.0, reg_toi=12.0, recent_days=30, k=4.0, top_n=99, min_gp=2.0):
        self.dec = 0.5 ** (1.0 / hl_games)
        self.reg_toi, self.recent, self.k = reg_toi, recent_days, k
        self.top_n, self.min_gp = top_n, min_gp
        self.pl = defaultdict(lambda: [0.0, 0.0, 0.0, 0.0, None])  # toi, gp, pts, pm, last
        self.team_of = {}
        self.roster = defaultdict(set)
        self.lg = [0.0, 0.0]    # league pts, games among regular rows → prior ppg

    def regulars(self, team, day):
        prior = self.lg[0] / self.lg[1] if self.lg[1] else 0.5
        out = {}
        for aid in self.roster[team]:
            if self.team_of.get(aid) != team:
                continue
            toi, gp, pts, pm, last = self.pl[aid]
            if gp < self.min_gp or last is None or (day - last).days > self.recent:
                continue
            e_toi = toi / gp
            if e_toi < self.reg_toi:
                continue
            ppg = (pts + prior * self.k) / (gp + self.k)
            pm60 = pm / (toi + 300.0) * 60.0
            out[aid] = (e_toi, ppg, pm60)
        if len(out) > self.top_n:
            keep = sorted(out, key=lambda a: -out[a][0])[:self.top_n]
            out = {a: out[a] for a in keep}
        return out

    def missing(self, team, day, played):
        regs = self.regulars(team, day)
        miss = [v for a, v in regs.items() if a not in played]
        return {"pts": sum(m[1] for m in miss), "toi": sum(m[0] for m in miss) / 60.0,
                "pm": sum(m[0] * m[2] / 60.0 for m in miss), "n": len(miss),
                "regs": len(regs)}

    def update(self, team, day, rows):
        for aid, _n, toi, _pos, pm, pts, is_g in rows:
            if is_g:
                continue
            s = self.pl[aid]
            s[0] = s[0] * self.dec + (toi or 0.0)
            s[1] = s[1] * self.dec + 1.0
            s[2] = s[2] * self.dec + (pts or 0.0)
            s[3] = s[3] * self.dec + (pm or 0.0)
            s[4] = day
            self.team_of[aid] = team
            self.roster[team].add(aid)
            if (toi or 0) >= self.reg_toi:
                self.lg[0] = self.lg[0] * 0.999 + (pts or 0.0)
                self.lg[1] = self.lg[1] * 0.999 + 1.0


# ---------------------------------------------------------------- fitting
def logit(p):
    p = min(max(p, 1e-6), 1 - 1e-6)
    return math.log(p / (1 - p))


def fit_logit(X, y, ridge=1.0):
    X, y = np.asarray(X), np.asarray(y)
    b = np.zeros(X.shape[1]); b[1] = 1.0 if X.shape[1] > 1 else 0.0
    R = ridge * np.eye(X.shape[1]); R[0, 0] = 0.0
    for _ in range(25):
        z = X @ b
        p = 1 / (1 + np.exp(-z))
        W = p * (1 - p)
        g = X.T @ (y - p) - R @ b
        H = X.T @ (X * W[:, None]) + R
        step = np.linalg.solve(H, g)
        b += step
        if np.max(np.abs(step)) < 1e-7:
            break
    return b


def imp(ml):
    return 100 / (ml + 100) if ml > 0 else -ml / (-ml + 100)


def payout(ml):
    return ml / 100 if ml > 0 else 100 / -ml


FEATS = {
    "crease":   None,
    "cal":      ["lp"],
    "avail":    ["lp", "d_pts", "d_pm"],
    "avail+":   ["lp", "d_pts", "d_pm", "d_toi"],
}


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", default=".nhl_cache")
    ap.add_argument("--seasons", default="2023,2024,2025,2026")
    ap.add_argument("--avail", default="", help="json Avail kwargs")
    a = ap.parse_args()
    cp = crease_preds(a.cache)
    espn = load_espn(a.cache, [int(x) for x in a.seasons.split(",")])
    log.info("espn: %d games", len(espn))

    av = Avail(**(json.loads(a.avail) if a.avail else {}))
    rows, unmatched = [], []
    for g in espn:
        d = espn_day(g["date"])
        h, w = nhl(g["home"]), nhl(g["away"])
        pl = g.get("players") or {}
        key = next((k for k in ((d.isoformat(), h, w),
                                ((d + timedelta(1)).isoformat(), h, w),
                                ((d - timedelta(1)).isoformat(), h, w)) if k in cp), None)
        if key and pl.get("home") and pl.get("away"):
            mh = av.missing(g["home"], d, {r[0] for r in pl["home"]})
            ma = av.missing(g["away"], d, {r[0] for r in pl["away"]})
            c = cp[key]
            L = g.get("line") or {}
            rows.append({"date": d, "season": c["season"], "p": c["p"], "lp": logit(c["p"]),
                         "y": c["y"], "d_pts": mh["pts"] - ma["pts"], "d_pm": mh["pm"] - ma["pm"],
                         "d_toi": mh["toi"] - ma["toi"], "n_h": mh["n"], "n_a": ma["n"],
                         "regs": (mh["regs"], ma["regs"]),
                         "mlh": L.get("ml_home"), "mla": L.get("ml_away")})
        elif not key and len(unmatched) < 12:
            unmatched.append((d.isoformat(), g["home"], g["away"]))
        for side in ("home", "away"):
            av.update(g[side], d, pl.get(side) or [])
    log.info("joined %d games; unmatched sample %s", len(rows), unmatched)
    nm = [r["n_h"] + r["n_a"] for r in rows]
    log.info("missing regulars per game: mean %.2f, games with ≥1: %.1f%%, mean regs/team %.1f",
             float(np.mean(nm)), 100 * float(np.mean([x > 0 for x in nm])),
             float(np.mean([sum(r["regs"]) / 2 for r in rows])))

    # walk-forward recalibration, refit per date
    dates = sorted({r["date"] for r in rows})
    by_date = defaultdict(list)
    for r in rows:
        by_date[r["date"]].append(r)
    hist = []
    preds = {k: [] for k in FEATS}
    coefs = {}
    for d in dates:
        todays = by_date[d]
        for name, cols in FEATS.items():
            if cols is None:
                for r in todays:
                    preds[name].append((r, r["p"]))
                continue
            if len(hist) < MIN_CAL:
                continue
            b = fit_logit([[1.0] + [h[c] for c in cols] for h in hist], [h["y"] for h in hist])
            coefs[name] = b
            for r in todays:
                z = b[0] + sum(bi * r[c] for bi, c in zip(b[1:], cols))
                preds[name].append((r, 1 / (1 + math.exp(-z))))
        hist.extend(todays)

    report = {"joined": len(rows), "coefs": {k: [round(x, 4) for x in v] for k, v in coefs.items()}}
    # grade every model on the SAME games: the ones every model priced
    graded_ids = None
    for name in FEATS:
        ids = {id(r) for r, _ in preds[name]}
        graded_ids = ids if graded_ids is None else graded_ids & ids
    for season in ("2023", "2024", "2025", "ALL"):
        res = {}
        mk = [0, 0.0]
        for name in FEATS:
            n = b = 0.0
            bets = won = 0
            profit = 0.0
            for r, p in preds[name]:
                if id(r) not in graded_ids or (season != "ALL" and r["season"] != season):
                    continue
                n += 1; b += (p - r["y"]) ** 2
                if r["mlh"] and r["mla"]:
                    ih, ia = imp(r["mlh"]), imp(r["mla"])
                    q = ih / (ih + ia)
                    if name == "crease":
                        mk[0] += 1; mk[1] += (q - r["y"]) ** 2
                    for pe, qe, ml, win in ((p, q, r["mlh"], r["y"] == 1),
                                            (1 - p, 1 - q, r["mla"], r["y"] == 0)):
                        e = (pe - qe) * 100
                        if 4.0 <= e <= 12.0:
                            bets += 1
                            won += win
                            profit += payout(ml) if win else -1.0
            res[name] = {"n": int(n), "brier": round(b / n, 4) if n else None,
                         "plays": bets, "won": won,
                         "roi": round(profit / bets, 4) if bets else None}
        res["dk_close"] = {"n": mk[0], "brier": round(mk[1] / mk[0], 4) if mk[0] else None}
        report[season] = res
        log.info("%s: %s", season, json.dumps(res))
    print("CREASE_AVAIL_REPORT " + json.dumps(report, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
