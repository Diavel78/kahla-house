"""Diamond IQ Phase 2 — walk-forward backtest of the pitcher-aware MLB model.

Replays mlb_pitcher_games chronologically: every game is predicted from
state built STRICTLY from earlier games, then the game updates state —
leakage-free, including the starter identity (the actual starter, which
in reality is the probable known before first pitch).

The table is self-sufficient: team A's runs scored = runs allowed by
team B's pitchers; the winner falls out of the run comparison (no ties
in baseball). Doubleheaders are distinct game_pks.

Season split: params fit on 2025, evaluated held-out on 2026. Reports
accuracy / Brier / calibration vs the home baseline AND the dead
team-only core (52.5%), plus a TEAM-ONLY control run to isolate what
the pitcher layer buys.

  python -m scripts.backtest_diamond_iq
"""
from __future__ import annotations

import logging
import sys
from collections import defaultdict
from datetime import date

from _lib import crease_iq as ci
from _lib import diamond_iq as di
from storage import supabase_client as db

log = logging.getLogger(__name__)

EVAL_START = date(2026, 3, 1)     # 2026 season = held-out eval window


def load_rows(sb) -> list[dict]:
    out, page = [], 0
    while True:      # explicit paging — the 1,000-row lesson
        rows = (sb.table("mlb_pitcher_games").select("*")
                .order("game_date").order("game_pk")
                .range(page * 1000, page * 1000 + 999).execute().data) or []
        out.extend(rows)
        if len(rows) < 1000:
            break
        page += 1
    return out


def group_games(rows: list[dict]) -> list[dict]:
    by_game: dict = defaultdict(list)
    for r in rows:
        by_game[r["game_pk"]].append(r)
    games = []
    for pk, rs in by_game.items():
        teams: dict[str, dict] = {}
        home_team = away_team = None
        for r in rs:
            t = r["team"]
            if not t:
                continue
            d = teams.setdefault(t, {"runs_allowed": 0, "pitchers": [],
                                     "starter": None})
            d["runs_allowed"] += r.get("runs") or 0
            d["pitchers"].append(r)
            if r.get("started"):
                d["starter"] = r["pitcher_id"]
            if r.get("home"):
                home_team = t
            else:
                away_team = t
        if not home_team or not away_team or home_team == away_team:
            continue
        h_runs = teams[away_team]["runs_allowed"]   # home scores what away allows
        a_runs = teams[home_team]["runs_allowed"]
        if h_runs == a_runs:
            continue      # suspended/odd rows; baseball has no ties
        games.append({
            "game_pk": pk, "date": date.fromisoformat(rs[0]["game_date"]),
            "home": home_team, "away": away_team,
            "home_won": 1 if h_runs > a_runs else 0,
            "teams": teams,
        })
    games.sort(key=lambda g: (g["date"], g["game_pk"]))
    return games


def _team_update_rows(g: dict) -> dict[str, dict]:
    h, a = g["home"], g["away"]
    return {
        h: {"runs_for": g["teams"][a]["runs_allowed"],
            "pitchers": g["teams"][h]["pitchers"]},
        a: {"runs_for": g["teams"][h]["runs_allowed"],
            "pitchers": g["teams"][a]["pitchers"]},
    }


def run(games: list[dict], team_only: bool, known_starter: bool = True) -> dict:
    state = di.DiamondState()
    train, evald = [], []
    for g in games:
        hs = g["teams"][g["home"]]["starter"] if known_starter else None
        as_ = g["teams"][g["away"]]["starter"] if known_starter else None
        proj = state.project(g["home"], g["away"], g["date"], hs, as_,
                             hfa=0.0, team_only=team_only)
        if proj is not None:
            rec = (proj["margin"], g["home_won"])
            (evald if g["date"] >= EVAL_START else train).append(rec)
        state.update(g["date"], _team_update_rows(g))

    hfa, scale = ci.fit_params(train) if train else (di.DEFAULT_HFA,
                                                     di.DEFAULT_SCALE)
    n = len(evald)
    if n == 0:
        return {"n": 0}
    probs = [(ci.margin_to_prob(m + hfa, scale), y) for m, y in evald]
    acc = sum(1 for p, y in probs if (p >= 0.5) == (y == 1)) / n
    brier = sum((p - y) ** 2 for p, y in probs) / n
    base = sum(y for _, y in probs) / n
    base_brier = sum((base - y) ** 2 for _, y in probs) / n
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


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    sb = db.client()
    rows = load_rows(sb)
    games = group_games(rows)
    log.info("loaded %d pitcher rows → %d games (%s → %s)", len(rows),
             len(games), games[0]["date"] if games else "-",
             games[-1]["date"] if games else "-")
    log.info("eval season (>= %s): %d games", EVAL_START,
             sum(1 for g in games if g["date"] >= EVAL_START))

    variants = [
        ("FULL (offense + starter FIP + bullpen)", False, True),
        ("NO-STARTER (league-avg SP — probable unknown)", False, False),
        ("TEAM-ONLY (the dead core control)", True, True),
    ]
    for name, team_only, known in variants:
        r = run(games, team_only, known)
        print(f"\n=== {name} ===")
        if r["n"] == 0:
            print("  no eval games")
            continue
        print(f"  eval n={r['n']} (train n={r['n_train']}) · "
              f"fit hfa={r['hfa']:.2f} scale={r['scale']:.2f}")
        print(f"  accuracy {r['acc']:.1%} vs home-base {r['home_base']:.1%}")
        print(f"  Brier {r['brier']:.4f} vs base-rate {r['base_brier']:.4f}"
              f"  (dead team core: 52.5%)")
        print(f"  calibration (favorite buckets): {r['calibration']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
