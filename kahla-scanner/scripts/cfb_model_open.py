"""Does our model's number against the OPENER predict where the college line goes?

Rob, Nov 15 2026: "Go… as it stands, we aren't betting". The money is right against the open
(cfb_spots.py: the team the line moved toward covers the OPEN line ~59%) and efficient by the
close — so the only way to get ahead of it is to know, at the open, which way it will move.

Walk-forward: every week's games are priced off ratings built only from games played before
that week (the results engine, _lib/power_ratings.py, 40-day half-life, the fixed home edge),
then compared to ESPN's opener (ESPN BET / DK, the one cfb_spots reads) and the DK close.

  gap  = model's home line − the opener's home line   (negative = the model likes home more)
  move = close − open                                 (negative = the market moved to home)

Reports: corr(gap, move); the model's side at |gap| ≥ X — % the line moved its way, its record
against the OPEN line and against the CLOSE line; split by week (early ratings are thin).
Read-only, no DB: ESPN schedules + odds through cfb_spots' cache.
"""
from __future__ import annotations

import argparse
import logging
import math
import os
from collections import defaultdict
from datetime import datetime, timedelta

from _lib import power_ratings as pr
from scripts.cfb_spots import fetch_all_odds, fetch_games, pay

HFA = 2.43          # the NCAAF home edge after the opponent adjustment (Aug 20 2026 fix)


def _dt(s):
    return datetime.fromisoformat(str(s).replace("Z", "+00:00"))


def walk(games, odds, first_season, half_life):
    games = sorted(games, key=lambda g: g["date"])
    by_week = defaultdict(list)
    for g in games:
        by_week[(g["season"], g["week"])].append(g)
    rated = [{"date": g["date"], "home": str(g["home"]), "away": str(g["away"]),
              "home_score": g["hs"], "away_score": g["as"], "season": g["season"]} for g in games]
    out = []
    for (season, week), wk in sorted(by_week.items()):
        if season < first_season:
            continue
        asof = min(_dt(g["date"]) for g in wk) - timedelta(hours=12)
        prior = [r for r in rated if _dt(r["date"]) < asof]
        if len(prior) < 300:
            continue
        R = pr.compute_ratings(prior, half_life_days=half_life, as_of=asof)
        if not R:
            continue
        T, lg = R["teams"], R["league_avg"]
        cnt = defaultdict(int)
        for r in prior:
            if r["season"] == season:
                cnt[r["home"]] += 1
                cnt[r["away"]] += 1
        for g in wk:
            h, a = T.get(str(g["home"])), T.get(str(g["away"]))
            od = odds.get(g["id"]) or {}
            o, c = od.get("open") or {}, od.get("close") or {}
            if not (h and a) or o.get("sp_home") is None or c.get("sp_home") is None:
                continue
            hfa = 0.0 if g["neutral"] else HFA
            m = (h["off"] + (a["def"] - lg)) - (a["off"] + (h["def"] - lg)) + hfa
            out.append({"id": g["id"], "season": season, "week": week, "proj": m,
                        "gp": min(cnt[str(g["home"])], cnt[str(g["away"])]),
                        "open": float(o["sp_home"]), "close": float(c["sp_home"]),
                        "px_h": c.get("sp_home_odds"), "px_a": c.get("sp_away_odds"),
                        "margin": g["hs"] - g["as"]})
        print(f"  week {season}-{week}: {len(out)} priced so far", flush=True)
    return out


def ats(margin, home_line, side):
    x = (margin + home_line) * side          # side +1 home, −1 away
    return 0 if abs(x) < 1e-9 else (1 if x > 0 else -1)


def corr(xs, ys):
    n = len(xs)
    if n < 10:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    vx = sum((x - mx) ** 2 for x in xs)
    vy = sum((y - my) ** 2 for y in ys)
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / math.sqrt(vx * vy) if vx and vy else 0.0


def rec(rs):
    w = sum(1 for r in rs if r == 1)
    l = sum(1 for r in rs if r == -1)
    n = w + l
    return f"{w}-{l} {100 * w / n:.1f}% z{(w - l) / math.sqrt(n):+.1f}" if n else "—"


def report(rows, beta, title):
    print(f"\n==== {title} — {len(rows)} games (model shrunk ×{beta:g}) ====")
    gaps = [(-beta * r["proj"]) - r["open"] for r in rows]
    moves = [r["close"] - r["open"] for r in rows]
    c = corr(gaps, moves)
    print(f"corr(model − open, close − open) = {c:+.3f}" if c is not None else "corr: too few")
    print("  |gap|≥   games   line moved model's way  (moved away)   vs OPEN line          vs CLOSE line          ROI@close")
    for th in (1, 2, 3, 4, 5, 7, 10):
        sel = [(g, r) for g, r in zip(gaps, rows) if abs(g) >= th]
        if not sel:
            continue
        tw = aw = 0
        ro, rc, usd = [], [], 0.0
        for g, r in sel:
            side = 1 if g < 0 else -1                       # model likes home → bet home
            mv = (r["close"] - r["open"]) * -side            # + = moved to the model's side
            tw += mv > 0.01
            aw += mv < -0.01
            ro.append(ats(r["margin"], r["open"], side))
            x = ats(r["margin"], r["close"], side)
            rc.append(x)
            px = r["px_h"] if side == 1 else r["px_a"]
            usd += pay(px) if x == 1 else (-1 if x == -1 else 0)
        n = sum(1 for x in rc if x)
        print(f"  {th:>4}   {len(sel):>5}   {100 * tw / len(sel):5.1f}%                ({100 * aw / len(sel):4.1f}%)"
              f"      {rec(ro):22s} {rec(rc):22s} {100 * usd / max(1, n):+.1f}%")


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--seasons", default="2023,2024,2025,2026")
    ap.add_argument("--cache", default=".cfb_cache")
    ap.add_argument("--half-life", type=float, default=40.0)
    a = ap.parse_args()
    os.makedirs(a.cache, exist_ok=True)
    seasons = [int(s) for s in a.seasons.split(",")]
    games = []
    for y in seasons:
        games += fetch_games(y, a.cache)
    odds = fetch_all_odds(games, a.cache)
    first = max(2024, seasons[0] + 1)            # 2023 lines are broken + it's the warm-up season
    rows = walk(games, odds, first, a.half_life)
    for beta in (1.0, 0.7):
        report(rows, beta, "ALL")
    report([r for r in rows if r["week"] <= 4], 0.7, "weeks 1-4")
    report([r for r in rows if 5 <= r["week"] <= 9], 0.7, "weeks 5-9")
    report([r for r in rows if r["week"] >= 10], 0.7, "weeks 10+")
    for y in sorted({r["season"] for r in rows}):
        report([r for r in rows if r["season"] == y and r["week"] >= 5], 0.7, f"season {y}, weeks 5+")


if __name__ == "__main__":
    main()
