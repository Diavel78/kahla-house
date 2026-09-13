"""Fit k — points per unit of QB ANY/A — for the football QB adjustment.

Walk-forward, the same walk as backtest_gridiron_spread: for each game
date, ratings + fitted HFA from prior games only, the shrinkage state from
prior (proj, actual) pairs only, and — new — each team's QB delta from the
spine's games before that date only. Then:

    residual   = actual margin − (alpha + beta·proj + mean)
    qb_delta   = (q(home starter) − baseline(home)) − (q(away starter) − baseline(away))
    residual ~ k · qb_delta                      (OLS, reported with SE / t)

and the Brier at the market-offset ladder WITH the term (k from prior
dates only — walk-forward too) vs WITHOUT. The starter used here is the
passer who ACTUALLY threw the game — the ceiling a perfect roster feed
reaches; the live compute reads ESPN's depth chart as its proxy.

  python -m scripts.backtest_football_qb --sport NFL
  python -m scripts.backtest_football_qb --sport NCAAF --min-gp 4
"""
from __future__ import annotations

import argparse
import logging
import math
import sys
from collections import defaultdict
from datetime import datetime, timezone

from _lib import football_qb as fq
from _lib import gridiron_spread as gsp
from _lib import power_ratings as pr
from storage import supabase_client as db

log = logging.getLogger(__name__)

_WINDOW_DAYS = {"NFL": 250, "NCAAF": 250}
_OFFSETS = (-14, -10, -7, -3, 0, 3, 7, 10, 14)


def _paged(sb, table, cols, filt):
    rows = []
    for page in range(60):
        q = sb.table(table).select(cols)
        for k, v in filt.items():
            q = q.eq(k, v)
        got = (q.order("id").range(page * 1000, page * 1000 + 999)
               .execute().data) or []
        rows.extend(got)
        if len(got) < 1000:
            break
    return rows


def load(sb, sport):
    games = []
    for r in _paged(sb, "game_results",
                    "id,espn_id,home,away,home_score,away_score,event_start",
                    {"sport": sport}):
        dt = pr._parse_dt(r.get("event_start"))
        if not dt or r.get("home_score") is None:
            continue
        games.append({"espn_id": str(r["espn_id"]), "home": r["home"],
                      "away": r["away"], "home_score": float(r["home_score"]),
                      "away_score": float(r["away_score"]), "date": dt})
    games.sort(key=lambda g: g["date"])
    by_eid = {g["espn_id"]: g for g in games}
    passers = _paged(sb, "football_player_games",
                     "espn_event_id,home,player_id,player_name,game_date,"
                     "pass_att,pass_yds,pass_td,pass_int,sacks,sack_yds",
                     {"sport": sport})
    # primary passer per (game, side) + every passer-game per QB
    per_side: dict[tuple[str, bool], dict] = {}
    qb_games: dict[str, list[dict]] = defaultdict(list)
    for r in passers:
        if (r.get("pass_att") or 0) <= 0:
            continue
        g = by_eid.get(str(r["espn_event_id"]))
        if not g:
            continue
        r = dict(r)
        r["game_date"] = g["date"]
        qb_games[r["player_id"]].append(r)
        key = (g["espn_id"], bool(r.get("home")))
        cur = per_side.get(key)
        if cur is None or (r["pass_att"] or 0) > (cur["pass_att"] or 0):
            per_side[key] = r
    # per team: [{date, passer_id, passer_name}] for the baseline
    team_games: dict[str, list[dict]] = defaultdict(list)
    for (eid, home), r in per_side.items():
        g = by_eid[eid]
        team_games[g["home"] if home else g["away"]].append(
            {"date": g["date"], "passer_id": r["player_id"],
             "passer_name": r.get("player_name")})
    all_rows = [r for rs in qb_games.values() for r in rs]
    return games, per_side, qb_games, team_games, all_rows


def _phi(z):
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def run(sb, sport, warmup, min_gp, resid_warmup, k_warmup):
    games, per_side, qb_games, team_games, all_rows = load(sb, sport)
    if len(games) < warmup + 40:
        print(f"{sport}: only {len(games)} games")
        return 1
    params = pr.SPORT_PARAMS[sport]
    half_life = params.get("half_life_days")
    window = _WINDOW_DAYS[sport]
    by_date = defaultdict(list)
    for g in games:
        by_date[g["date"].date()].append(g)

    pairs = []                # (proj, actual) — for the shrink state
    fitpairs = []             # (qb_delta, residual) — for k
    brier = {"base": [0.0, 0], "adj": [0.0, 0]}
    change_res = []           # (delta, residual) where |delta| >= 1
    seen_delta = []
    n = 0
    for d in sorted(by_date):
        prior = [g for g in games if g["date"].date() < d]
        if len(prior) < warmup:
            continue
        cutoff = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
        win = [g for g in prior if (cutoff - g["date"]).days <= window]
        ratings = pr.compute_ratings(win, half_life_days=half_life, as_of=cutoff)
        if not ratings:
            continue
        cal = pr.calibrate(win, ratings, params.get("hfa", 0.0),
                           params.get("scale", 1.0))
        hfa = cal["hfa"] if cal else params.get("hfa", 0.0)
        state = gsp.fit(pairs) if len(pairs) >= resid_warmup else None
        kfit = fq.ols_slope([x for x, _ in fitpairs], [y for _, y in fitpairs]) \
            if len(fitpairs) >= k_warmup else None
        k_wf = kfit["k"] if kfit else 0.0
        lg = fq.league_anya(all_rows, cutoff)
        repl = (lg - fq.REPLACEMENT_BELOW_LG) if lg is not None else None
        qcache: dict[str, float] = {}

        def q_of(pid):
            if pid not in qcache:
                qcache[pid] = fq.qb_quality(qb_games.get(pid, []), cutoff, repl)[0]
            return qcache[pid]

        todays = []
        for g in by_date[d]:
            proj = pr.project(ratings, g["home"], g["away"], hfa=hfa)
            if not proj:
                continue
            h = ratings["teams"].get(g["home"]) or {}
            a = ratings["teams"].get(g["away"]) or {}
            if h.get("gp", 0) < min_gp or a.get("gp", 0) < min_gp:
                continue
            actual = g["home_score"] - g["away_score"]
            todays.append((proj["margin"], actual))
            if state is None or repl is None:
                continue
            sh = per_side.get((g["espn_id"], True))
            sa = per_side.get((g["espn_id"], False))
            bh = fq.team_baseline(team_games.get(g["home"], []), q_of, cutoff,
                                  half_life, window)
            ba = fq.team_baseline(team_games.get(g["away"], []), q_of, cutoff,
                                  half_life, window)
            if not (sh and sa and bh and ba):
                continue
            delta = ((q_of(sh["player_id"]) - bh["q"])
                     - (q_of(sa["player_id"]) - ba["q"]))
            shrunk = state["alpha"] + state["beta"] * proj["margin"] + state["mean"]
            resid = actual - shrunk
            fitpairs.append((delta, resid))
            seen_delta.append(delta)
            if abs(delta) >= 1.0:
                change_res.append((delta, resid))
            n += 1
            sd = state["sd"]
            for off in _OFFSETS:
                thr = int(round(proj["margin"])) + off + 0.5
                covered = 1.0 if actual > thr else 0.0
                for name, adj in (("base", 0.0), ("adj", k_wf * delta)):
                    p = 1.0 - _phi((thr - (shrunk + adj)) / sd)
                    p = min(max(p, 0.005), 0.995)
                    brier[name][0] += (p - covered) ** 2
                    brier[name][1] += 1
        pairs.extend(todays)

    fit = fq.ols_slope([x for x, _ in fitpairs], [y for _, y in fitpairs])
    print(f"\n{sport}: {n} graded games, {len(fitpairs)} fit pairs")
    if fit:
        print(f"  k = {fit['k']:.3f} pts per ANY/A  (se {fit['se']:.3f}, "
              f"t {fit['t']:.2f}, n {fit['n']}, delta sd {fit['x_sd']:.2f})")
    if seen_delta:
        big = sum(1 for x in seen_delta if abs(x) >= 1.0)
        print(f"  |delta| >= 1.0 ANY/A (a real QB change): {big} games "
              f"({100.0 * big / len(seen_delta):.1f}%)")
    if change_res:
        sgn = [r * (1 if x > 0 else -1) for x, r in change_res]
        print(f"  on those games the residual toward the better-QB side "
              f"averaged {sum(sgn) / len(sgn):+.2f} pts (n {len(sgn)})")
    for name in ("base", "adj"):
        s, c = brier[name]
        if c:
            print(f"  brier {name:4s}: {s / c:.5f}  (events {c})")
    return 0


def main(argv=None):
    logging.basicConfig(level=logging.WARNING)
    ap = argparse.ArgumentParser()
    ap.add_argument("--sport", default="NFL", choices=("NFL", "NCAAF"))
    ap.add_argument("--warmup", type=int, default=60)
    ap.add_argument("--min-gp", type=int, default=4)
    ap.add_argument("--resid-warmup", type=int, default=80)
    ap.add_argument("--k-warmup", type=int, default=120)
    args = ap.parse_args(argv)
    return run(db.client(), args.sport, args.warmup, args.min_gp,
               args.resid_warmup, args.k_warmup)


if __name__ == "__main__":
    sys.exit(main())
