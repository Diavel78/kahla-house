"""Crease IQ Phase 2 — walk-forward backtest of the goalie-aware NHL model.

Replays nhl_goalie_games chronologically: every game is predicted from
state built STRICTLY from earlier games (then the game updates state),
so there is no leakage — including the starter identity, which the
table records as who actually started (known pre-puck-drop in reality
via the confirmation window).

The table is self-sufficient: team A's shots-for = shots faced by team
B's goalies, goals from the GA lines (shootout deciders excluded by the
boxscore — the win label comes from `decision` instead of the score).

Season split: params (HFA + logistic scale) fit on the 2024-25 season,
evaluated held-out on 2025-26. Reports accuracy / Brier / calibration
vs the home baseline AND the dead team core (53.3%, Brier 0.2576),
plus a no-starter-knowledge variant (league-avg goalie) to isolate how
much the goalie feed itself buys.

  python -m scripts.backtest_crease_iq            # full report
  python -m scripts.backtest_crease_iq --sweep    # hyperparameter sweep;
        # configs are SELECTED on a train-internal split (fit < 2025-03-01,
        # select 2025-03..09) and only the winner is scored on the eval
        # season — the eval never picks the config.
"""
from __future__ import annotations

import argparse
import itertools
import logging
import sys
from collections import defaultdict
from datetime import date

from _lib import crease_iq as ci
from storage import supabase_client as db

log = logging.getLogger(__name__)

EVAL_START = date(2025, 9, 1)     # 2025-26 season = held-out eval window


def load_rows(sb) -> list[dict]:
    out, page = [], 0
    while True:      # explicit paging — the 1,000-row lesson
        rows = (sb.table("nhl_goalie_games").select("*")
                .order("game_date").order("game_id")
                .range(page * 1000, page * 1000 + 999).execute().data) or []
        out.extend(rows)
        if len(rows) < 1000:
            break
        page += 1
    return out


def group_games(rows: list[dict]) -> list[dict]:
    """Goalie rows → one record per game with per-team aggregates."""
    by_game: dict = defaultdict(list)
    for r in rows:
        by_game[r["game_id"]].append(r)
    games = []
    for gid, rs in by_game.items():
        teams: dict[str, dict] = {}
        home_team = away_team = None
        winner = None
        for r in rs:
            t = r["team"]
            d = teams.setdefault(t, {"sa": 0, "saves": 0, "ga": 0,
                                     "goalies": [], "starter": None})
            d["sa"] += r.get("shots_against") or 0
            d["saves"] += r.get("saves") or 0
            d["ga"] += r.get("goals_against") or 0
            d["goalies"].append((r["goalie_id"], r.get("shots_against"),
                                 r.get("saves")))
            if r.get("starter"):
                d["starter"] = r["goalie_id"]
            if r.get("home"):
                home_team = t
            else:
                away_team = t
            if (r.get("decision") or "").upper() == "W":
                winner = t
        if not home_team or not away_team or winner is None:
            continue
        games.append({
            "game_id": gid, "date": date.fromisoformat(rs[0]["game_date"]),
            "home": home_team, "away": away_team,
            "home_won": 1 if winner == home_team else 0,
            "teams": teams,
        })
    games.sort(key=lambda g: (g["date"], g["game_id"]))
    return games


def _team_update_rows(g: dict) -> dict[str, dict]:
    h, a = g["home"], g["away"]
    th, ta = g["teams"][h], g["teams"][a]
    # A's shots-for/goals-for = what B's goalies faced/allowed
    return {
        h: {"sf": ta["sa"], "sa": th["sa"], "gf": ta["ga"], "ga": th["ga"],
            "goalies": th["goalies"]},
        a: {"sf": th["sa"], "sa": ta["sa"], "gf": th["ga"], "ga": ta["ga"],
            "goalies": ta["goalies"]},
    }


def collect(games: list[dict], goalie_only: bool, known_starter: bool,
            **params) -> list[tuple]:
    """Walk-forward (date, raw_margin, home_won) preds for every warm game."""
    state = ci.CreaseState(**params)
    preds = []
    for g in games:
        hg = g["teams"][g["home"]]["starter"] if known_starter else None
        ag = g["teams"][g["away"]]["starter"] if known_starter else None
        proj = state.project(g["home"], g["away"], g["date"], hg, ag,
                             hfa=0.0, goalie_only=goalie_only)
        if proj is not None:
            preds.append((g["date"], proj["margin"], g["home_won"]))
        state.update(g["date"], _team_update_rows(g))
    return preds


def run(games: list[dict], goalie_only: bool, known_starter: bool,
        **params) -> dict:
    preds = collect(games, goalie_only, known_starter, **params)
    train = [(m, y) for d, m, y in preds if d < EVAL_START]
    evald = [(m, y) for d, m, y in preds if d >= EVAL_START]

    hfa, scale = ci.fit_params(train) if train else (ci.DEFAULT_HFA,
                                                     ci.DEFAULT_SCALE)
    n = len(evald)
    if n == 0:
        return {"n": 0}
    probs = [(ci.margin_to_prob(m + hfa, scale), y) for m, y in evald]
    acc = sum(1 for p, y in probs if (p >= 0.5) == (y == 1)) / n
    brier = sum((p - y) ** 2 for p, y in probs) / n
    base = sum(y for _, y in probs) / n           # home-win base rate
    base_brier = sum((base - y) ** 2 for _, y in probs) / n
    # calibration
    buckets = defaultdict(lambda: [0, 0])
    for p, y in probs:
        fav_p = max(p, 1 - p)
        fav_won = y if p >= 0.5 else 1 - y
        k = ("50-55" if fav_p < 0.55 else "55-60" if fav_p < 0.60
             else "60-70" if fav_p < 0.70 else "70+")
        buckets[k][0] += fav_won
        buckets[k][1] += 1
    cal = {k: f"{v[0]}/{v[1]} = {v[0]/v[1]:.1%}" for k, v in sorted(buckets.items())}
    return {"n": n, "n_train": len(train), "hfa": hfa, "scale": scale,
            "acc": acc, "brier": brier, "home_base": base,
            "base_brier": base_brier, "calibration": cal}


SELECT_START = date(2025, 3, 1)   # trainB: config-selection slice


def sweep(games: list[dict]) -> None:
    """Grid sweep. Fit hfa/scale on trainA (< SELECT_START), select the
    config by Brier on trainB (SELECT_START..EVAL_START), then score ONLY
    the winner on the eval season."""
    grid = list(itertools.product(
        [150.0, 300.0, 400.0, 600.0],      # goalie_prior shots
        [90.0, 180.0, 365.0],              # goalie_hl days
        [40.0, 60.0, 90.0],                # team_hl days
        [True, False],                     # goalie_only
    ))
    scored = []
    for gp, ghl, thl, gonly in grid:
        preds = collect(games, gonly, True,
                        goalie_prior=gp, goalie_hl=ghl, team_hl=thl)
        tr_a = [(m, y) for d, m, y in preds if d < SELECT_START]
        tr_b = [(m, y) for d, m, y in preds
                if SELECT_START <= d < EVAL_START]
        if len(tr_a) < 100 or len(tr_b) < 100:
            continue
        hfa, scale = ci.fit_params(tr_a)
        brier_b = sum((ci.margin_to_prob(m + hfa, scale) - y) ** 2
                      for m, y in tr_b) / len(tr_b)
        scored.append((brier_b, gp, ghl, thl, gonly))
    scored.sort()
    print("\n=== SWEEP (selected on trainB Brier — eval untouched) ===")
    for b, gp, ghl, thl, gonly in scored[:8]:
        print(f"  trainB Brier {b:.4f} · prior={gp:.0f} goalie_hl={ghl:.0f} "
              f"team_hl={thl:.0f} goalie_only={gonly}")
    b, gp, ghl, thl, gonly = scored[0]
    print(f"\nWINNER on eval season (scored once):")
    r = run(games, gonly, True, goalie_prior=gp, goalie_hl=ghl, team_hl=thl)
    print(f"  eval n={r['n']} · accuracy {r['acc']:.1%} vs home-base "
          f"{r['home_base']:.1%} · Brier {r['brier']:.4f} vs base "
          f"{r['base_brier']:.4f}")
    print(f"  calibration: {r['calibration']}")
    rc = run(games, gonly, False, goalie_prior=gp, goalie_hl=ghl, team_hl=thl)
    print(f"  no-starter control at same config: {rc['acc']:.1%} / "
          f"Brier {rc['brier']:.4f}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sweep", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    sb = db.client()
    rows = load_rows(sb)
    games = group_games(rows)
    log.info("loaded %d goalie rows → %d games (%s → %s)", len(rows),
             len(games), games[0]["date"] if games else "-",
             games[-1]["date"] if games else "-")
    n_eval_season = sum(1 for g in games if g["date"] >= EVAL_START)
    log.info("eval season (>= %s): %d games", EVAL_START, n_eval_season)

    if args.sweep:
        sweep(games)
        return 0

    variants = [
        ("FULL (shots + shooting + starter goalie)", False, True),
        ("GOALIE-ONLY (shots + starter goalie, no shooting skill)", True, True),
        ("NO-STARTER (league-avg goalie — the control)", False, False),
    ]
    for name, goalie_only, known in variants:
        r = run(games, goalie_only, known)
        print(f"\n=== {name} ===")
        if r["n"] == 0:
            print("  no eval games")
            continue
        print(f"  eval n={r['n']} (train n={r['n_train']}) · "
              f"fit hfa={r['hfa']:.2f} scale={r['scale']:.2f}")
        print(f"  accuracy {r['acc']:.1%} vs home-base {r['home_base']:.1%}")
        print(f"  Brier {r['brier']:.4f} vs base-rate {r['base_brier']:.4f}"
              f"  (dead team core: 53.3% / 0.2576)")
        print(f"  calibration (favorite buckets): {r['calibration']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
