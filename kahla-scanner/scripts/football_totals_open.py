"""FOOTBALL TOTALS vs the OPEN (Nov 15 2026, Rob: "Get going, I want totals").

The hockey lesson (backtest_crease_totals): nobody beats a CLOSING total, but our model beat
the OPENING one. Football's total (Gridiron IQ, the results engine) passed its calibration
check vs finals in August and was never graded against a betting line. This grades it at the
open, walk-forward, NFL + college, seasons back — and the move rules on NFL totals beside it.

Each week's games are priced off ratings built only from games played before that week
(_lib/power_ratings.py, the production engine): proj = the two expected scores summed.
Two predictors, both fit on PRIOR weeks only:
  RAW    mu = a + b·proj                          (the model alone, location-fit)
  BLEND  mu = open + c + β·(a + b·proj − open)     (what the model adds to the opener)
P(over | no push) off a normal with the prior residual sd. A bet = the model's side at the
OPEN line and OPEN price when its probability clears the price's break-even by ≥ X pp;
graded at the open (what you get betting the open) and at the close line + close price.

Plus: the side the market moved (close − open) vs the model; and follow/fade-the-move
records for totals by Eastern start slot, seasons split.

  python -m scripts.football_totals_open --sport NFL --seasons 2022,2023,2024,2025,2026
  python -m scripts.football_totals_open --sport NCAAF --seasons 2023,2024,2025,2026
ESPN schedules + core odds only (no DB, no secrets).
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from _lib import power_ratings as pr
from scripts.cfb_spots import fetch_all_odds, fetch_games, pay

ET = ZoneInfo("America/New_York")
log = logging.getLogger("football_totals_open")


# ------------------------------------------------------------------ data
def nfl_games(y, cache):
    from scripts import nfl_dump
    p = os.path.join(cache, f"nfl_games_{y}.json")
    if os.path.exists(p):
        return json.load(open(p))
    out = nfl_dump.games(y)
    if y < datetime.now(timezone.utc).year:
        json.dump(out, open(p, "w"))
    return out


def nfl_odds(games, cache):
    from scripts import nfl_dump
    p = os.path.join(cache, "odds.json")
    have = json.load(open(p)) if os.path.exists(p) else {}
    todo = [g["id"] for g in games if not have.get(g["id"])]
    log.info("nfl odds: %d cached, %d to fetch", len(games) - len(todo), len(todo))
    with ThreadPoolExecutor(8) as ex:
        for e, r in zip(todo, ex.map(nfl_dump.odds, todo)):
            have[e] = r
    json.dump(have, open(p, "w"))
    return have


# ------------------------------------------------------------------ math
def _phi(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def p_over(mu, sd, line):
    """P(over | no push) for an integer-scored total at `line`."""
    if abs(line - round(line)) < 1e-9:
        po = 1 - _phi((line + 0.5 - mu) / sd)
        pu = _phi((line - 0.5 - mu) / sd)
    else:
        po = 1 - _phi((line - mu) / sd)
        pu = 1 - po
    s = po + pu
    return po / s if s > 0 else 0.5


def imp(px):
    px = px if px is not None else -110.0
    return 100.0 / (px + 100.0) if px > 0 else -px / (-px + 100.0)


def ols(xs, ys):
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    vx = sum((x - mx) ** 2 for x in xs)
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / vx if vx else 0.0
    return my - b * mx, b


def corr(xs, ys):
    n = len(xs)
    if n < 10:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    vx = sum((x - mx) ** 2 for x in xs)
    vy = sum((y - my) ** 2 for y in ys)
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / math.sqrt(vx * vy) if vx and vy else 0.0


def ou(total, line):
    x = total - line
    return 0 if abs(x) < 1e-9 else (1 if x > 0 else -1)


def _dt(s):
    return datetime.fromisoformat(str(s).replace("Z", "+00:00"))


def slot_of(sport, d):
    k = _dt(d).astimezone(ET)
    hr = k.hour + k.minute / 60
    if sport == "NFL":
        if k.strftime("%a") in ("Mon", "Thu") or hr >= 19:
            return "prime"
        return "1pm" if hr < 15 else "4pm"
    return "day" if hr < 18.5 else "night"


# ------------------------------------------------------------------ walk
def walk(sport, games, odds, first_season, half_life, min_prior):
    games = sorted(games, key=lambda g: g["date"])
    by_week = defaultdict(list)
    for g in games:
        by_week[(g["season"], g["week"])].append(g)
    rated = [{"date": g["date"], "home": str(g["home"]), "away": str(g["away"]),
              "home_score": g["hs"], "away_score": g["as"], "season": g["season"]} for g in games]
    hist = []        # every priced game (prior weeks feed the fits)
    out = []
    for (season, week), wk in sorted(by_week.items()):
        asof = min(_dt(g["date"]) for g in wk) - timedelta(hours=12)
        prior = [r for r in rated if _dt(r["date"]) < asof]
        if len(prior) < min_prior:
            continue
        R = pr.compute_ratings(prior, half_life_days=half_life, as_of=asof)
        if not R:
            continue
        T, lg = R["teams"], R["league_avg"]
        # fits off prior weeks only
        fit = [h for h in hist if h["asof"] < asof]
        cal = blend = None
        sd_raw = sd_bl = None
        if len(fit) >= 150:
            a, b = ols([h["proj"] for h in fit], [h["actual"] for h in fit])
            res = [h["actual"] - (a + b * h["proj"]) for h in fit]
            sd_raw = math.sqrt(sum(r * r for r in res) / len(res))
            cal = (a, b)
            fo = [h for h in fit if h.get("open") is not None]
            if len(fo) >= 150:
                xs = [(a + b * h["proj"]) - h["open"] for h in fo]
                ys = [h["actual"] - h["open"] for h in fo]
                c, beta = ols(xs, ys)
                rb = [y - (c + beta * x) for x, y in zip(xs, ys)]
                sd_bl = math.sqrt(sum(r * r for r in rb) / len(rb))
                blend = (c, beta)
        for g in wk:
            h, aw = T.get(str(g["home"])), T.get(str(g["away"]))
            if not (h and aw):
                continue
            proj = h["off"] + (aw["def"] - lg) + aw["off"] + (h["def"] - lg)
            od = odds.get(g["id"]) or {}
            o, c = od.get("open") or {}, od.get("close") or {}
            row = {"asof": asof, "season": season, "week": week, "proj": proj,
                   "actual": g["hs"] + g["as"], "open": o.get("total"), "close": c.get("total"),
                   "o_over": o.get("over"), "o_under": o.get("under"),
                   "c_over": c.get("over"), "c_under": c.get("under"),
                   "slot": slot_of(sport, g["date"])}
            hist.append(row)
            if season < first_season or not cal or row["open"] is None or row["close"] is None:
                continue
            a, b = cal
            row["mu_raw"], row["sd_raw"] = a + b * proj, sd_raw
            if blend:
                cc, beta = blend
                row["mu_bl"] = row["open"] + cc + beta * ((a + b * proj) - row["open"])
                row["sd_bl"], row["beta"] = sd_bl, beta
            out.append(row)
        log.info("%s week %s-%s: %d graded rows", sport, season, week, len(out))
    return out


# ------------------------------------------------------------------ report
def rec(rs):
    w = sum(1 for r in rs if r == 1)
    l = sum(1 for r in rs if r == -1)
    n = w + l
    return f"{w}-{l} {100 * w / n:.1f}% z{(w - l) / math.sqrt(n):+.1f}" if n else "—"


def roi(results):
    """results = [(+1/-1/0, american price)] → ROI per graded bet."""
    usd, n = 0.0, 0
    for x, px in results:
        if x == 0:
            continue
        n += 1
        usd += pay(px) if x == 1 else -1.0
    return f"{100 * usd / n:+.1f}%" if n else "—"


def devig(po, pu):
    a, b = imp(po), imp(pu)
    return a / (a + b) if a + b else 0.5


def brier(rows, key_mu, key_sd, line_key, ov, un):
    bm = bk = 0.0
    n = 0
    for r in rows:
        L = r[line_key]
        x = ou(r["actual"], L)
        if x == 0 or r.get(key_mu) is None:
            continue
        y = 1.0 if x == 1 else 0.0
        pm = p_over(r[key_mu], r[key_sd], L)
        pk = devig(r[ov], r[un])
        bm += (pm - y) ** 2
        bk += (pk - y) ** 2
        n += 1
    return (bm / n, bk / n, n) if n else (None, None, 0)


def bets(rows, key_mu, key_sd, th):
    """The model's side at the OPEN when it clears the open price by ≥ th pp."""
    o_res, c_res, picks = [], [], []
    for r in rows:
        if r.get(key_mu) is None:
            continue
        L = r["open"]
        po = p_over(r[key_mu], r[key_sd], L)
        eo, eu = po - imp(r["o_over"]), (1 - po) - imp(r["o_under"])
        if max(eo, eu) * 100 < th:
            continue
        side = 1 if eo >= eu else -1
        picks.append(side)
        o_res.append((ou(r["actual"], L) * side, r["o_over"] if side == 1 else r["o_under"]))
        c_res.append((ou(r["actual"], r["close"]) * side, r["c_over"] if side == 1 else r["c_under"]))
    return o_res, c_res, picks


def report(rows, title):
    print(f"\n==== {title} — {len(rows)} games ====")
    if not rows:
        return
    n = len(rows)
    print(f"level: model raw {sum(r['mu_raw'] for r in rows) / n:.1f}  open "
          f"{sum(r['open'] for r in rows) / n:.1f}  close {sum(r['close'] for r in rows) / n:.1f}  "
          f"actual {sum(r['actual'] for r in rows) / n:.1f}")
    o_all = [ou(r["actual"], r["open"]) for r in rows]
    print(f"every OVER at the open {rec(o_all)}  ·  every OVER at the close "
          f"{rec([ou(r['actual'], r['close']) for r in rows])}")
    gaps = [r["mu_raw"] - r["open"] for r in rows]
    mv = [r["close"] - r["open"] for r in rows]
    cc = corr(gaps, mv)
    print(f"corr(model − open, close − open) = {cc:+.3f}" if cc is not None else "corr: —")
    bl = [r for r in rows if r.get("mu_bl") is not None]
    if bl:
        print(f"blend β (what the model adds to the open, last fit) = {bl[-1]['beta']:+.3f}")
    for nm, km, ks, rs in (("RAW", "mu_raw", "sd_raw", rows), ("BLEND", "mu_bl", "sd_bl", bl)):
        bmo, bko, no = brier(rs, km, ks, "open", "o_over", "o_under")
        bmc, bkc, nc = brier(rs, km, ks, "close", "c_over", "c_under")
        if no:
            print(f"  {nm:5s} Brier at the OPEN  model {bmo:.4f} market {bko:.4f} (n {no})   "
                  f"at the CLOSE model {bmc:.4f} market {bkc:.4f}")
        print(f"  {nm:5s}  edge≥  bets  overs   at the OPEN (line+price)        at the CLOSE        "
              f"line moved model's way")
        for th in (0, 2, 3, 5, 8, 10):
            o_res, c_res, picks = bets(rs, km, ks, th)
            if not picks:
                continue
            # line moved the model's way, over the same bets
            sel = []
            for r in rs:
                if r.get(km) is None:
                    continue
                L = r["open"]
                po = p_over(r[km], r[ks], L)
                eo, eu = po - imp(r["o_over"]), (1 - po) - imp(r["o_under"])
                if max(eo, eu) * 100 >= th:
                    sel.append((1 if eo >= eu else -1, r["close"] - r["open"]))
            tw = sum(1 for s, m in sel if m * s > 0.01)
            k = sum(1 for s, m in sel if abs(m) > 0.01)
            ov = sum(1 for p in picks if p == 1)
            print(f"        {th:>4}  {len(picks):>5}  {100 * ov / len(picks):4.0f}%   "
                  f"{rec([x for x, _ in o_res]):20s} {roi(o_res):>7}   "
                  f"{rec([x for x, _ in c_res]):20s} {roi(c_res):>7}   "
                  f"{100 * tw / max(1, k):4.0f}% of {k} moves")


def move_rules(rows_all, sport, seasons):
    """Follow / fade the total's open → close move, graded at the close price."""
    print(f"\n==== {sport} TOTALS — follow the move (open → close), graded at the close ====")
    print("  (fade = the other side; ROI is the follower's at the close price)")
    for slot in (None,) + (("1pm", "4pm", "prime") if sport == "NFL" else ("day", "night")):
        for mn in (0.5, 1.0, 1.5, 2.0, 3.0):
            parts = []
            for y in seasons + [None]:
                rs = []
                for r in rows_all:
                    if r["open"] is None or r["close"] is None:
                        continue
                    if y is not None and r["season"] != y:
                        continue
                    if slot and r["slot"] != slot:
                        continue
                    m = r["close"] - r["open"]
                    if abs(m) < mn - 1e-9:
                        continue
                    side = 1 if m > 0 else -1
                    rs.append((ou(r["actual"], r["close"]) * side,
                               r["c_over"] if side == 1 else r["c_under"]))
                parts.append(f"{'all' if y is None else y}: {rec([x for x, _ in rs])} {roi(rs)}")
            print(f"  {slot or 'every game':10s} moved {mn:>3}+   " + "  |  ".join(parts))
    print(f"\n==== {sport} TOTALS — nobody moved it (|move| < 0.5): over / under at the close ====")
    for y in seasons + [None]:
        rs = [ou(r["actual"], r["close"]) for r in rows_all
              if r["open"] is not None and r["close"] is not None
              and abs(r["close"] - r["open"]) < 0.5 and (y is None or r["season"] == y)]
        print(f"  {'all' if y is None else y}: overs {rec(rs)}")


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--sport", default="NFL")
    ap.add_argument("--seasons", default=None)
    ap.add_argument("--cache", default=None)
    ap.add_argument("--half-life", type=float, default=None)
    a = ap.parse_args()
    sport = a.sport.upper()
    seasons = [int(s) for s in (a.seasons or ("2022,2023,2024,2025,2026" if sport == "NFL"
                                              else "2023,2024,2025,2026")).split(",")]
    cache = a.cache or (".nfl_cache" if sport == "NFL" else ".cfb_cache")
    os.makedirs(cache, exist_ok=True)
    games = []
    for y in seasons:
        games += nfl_games(y, cache) if sport == "NFL" else fetch_games(y, cache)
    odds = nfl_odds(games, cache) if sport == "NFL" else fetch_all_odds(games, cache)
    print(f"{sport}: {len(games)} games, {sum(1 for g in games if (odds.get(g['id']) or {}).get('open', {}).get('total') is not None)} with an opening total")
    hl = a.half_life or (60.0 if sport == "NFL" else 40.0)
    rows = walk(sport, games, odds, seasons[1], hl, 200 if sport == "NFL" else 600)
    report(rows, f"{sport} ALL")
    for y in sorted({r["season"] for r in rows}):
        report([r for r in rows if r["season"] == y], f"{sport} season {y}")
    if sport == "NFL":
        report([r for r in rows if r["week"] <= 6], "NFL weeks 1-6")
        report([r for r in rows if r["week"] >= 7], "NFL weeks 7+")
    else:
        report([r for r in rows if r["week"] <= 4], "NCAAF weeks 1-4")
        report([r for r in rows if r["week"] >= 5], "NCAAF weeks 5+")
    for sl in sorted({r["slot"] for r in rows}):
        report([r for r in rows if r["slot"] == sl], f"{sport} slot {sl}")
    # move rules use every game with both lines (no model needed)
    allrows = []
    for g in games:
        od = odds.get(g["id"]) or {}
        o, c = od.get("open") or {}, od.get("close") or {}
        allrows.append({"season": g["season"], "actual": g["hs"] + g["as"], "open": o.get("total"),
                        "close": c.get("total"), "c_over": c.get("over"), "c_under": c.get("under"),
                        "slot": slot_of(sport, g["date"])})
    move_rules(allrows, sport, seasons)


if __name__ == "__main__":
    main()
