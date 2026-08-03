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


def load_batter_rows(sb) -> list[dict]:
    out, page = [], 0
    while True:      # explicit paging — the 1,000-row lesson
        rows = (sb.table("mlb_batter_games")
                .select("game_pk,team,batter_id,batting_order,started,"
                        "ab,hits,doubles,triples,home_runs,walks,hbp,"
                        "sac_flies")
                .order("game_pk").order("batter_id")
                .range(page * 1000, page * 1000 + 999).execute().data) or []
        out.extend(rows)
        if len(rows) < 1000:
            break
        page += 1
    return out


def load_xwoba_days(sb) -> list[tuple]:
    """[(date, [day rows])] sorted — the walk-forward xwOBA feed."""
    out, page = [], 0
    while True:      # explicit paging — the 1,000-row lesson
        rows = (sb.table("mlb_xwoba_batter_days")
                .select("game_date,batter_id,pa,xwoba_sum")
                .order("game_date").order("batter_id")
                .range(page * 1000, page * 1000 + 999).execute().data) or []
        out.extend(rows)
        if len(rows) < 1000:
            break
        page += 1
    by_day: dict = defaultdict(list)
    for r in out:
        by_day[date.fromisoformat(r["game_date"])].append(r)
    return sorted(by_day.items())


def load_platoon_days(sb) -> list[tuple]:
    """[(date, [day rows])] sorted — the walk-forward platoon feed."""
    out, page = [], 0
    while True:      # explicit paging — the 1,000-row lesson
        rows = (sb.table("mlb_platoon_batter_days")
                .select("game_date,batter_id,vs_hand,pa,woba_sum")
                .order("game_date").order("batter_id").order("vs_hand")
                .range(page * 1000, page * 1000 + 999).execute().data) or []
        out.extend(rows)
        if len(rows) < 1000:
            break
        page += 1
    by_day: dict = defaultdict(list)
    for r in out:
        by_day[date.fromisoformat(r["game_date"])].append(r)
    return sorted(by_day.items())


def load_pitcher_throws(sb) -> dict[int, str]:
    """Static pitcher hand map (biology — safe to preload, no leakage)."""
    out, page = {}, 0
    while True:      # explicit paging — the 1,000-row lesson
        rows = (sb.table("mlb_pitcher_throws").select("pitcher_id,throws")
                .range(page * 1000, page * 1000 + 999).execute().data) or []
        for r in rows:
            out[r["pitcher_id"]] = r["throws"]
        if len(rows) < 1000:
            break
        page += 1
    return out


def group_games(rows: list[dict], batter_rows: list[dict] | None = None
                ) -> list[dict]:
    b_by_game: dict = defaultdict(list)
    for b in (batter_rows or []):
        b_by_game[b["game_pk"]].append(b)
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
                                     "starter": None, "batters": [],
                                     "lineup": None})
            d["runs_allowed"] += r.get("runs") or 0
            d["pitchers"].append(r)
            if r.get("started"):
                d["starter"] = r["pitcher_id"]
            if r.get("home"):
                home_team = t
            else:
                away_team = t
        for b in b_by_game.get(pk, []):
            t = teams.get(b.get("team"))
            if t is None:
                continue
            t["batters"].append(b)
        for t in teams.values():
            starters = sorted((b for b in t["batters"]
                               if b.get("started") and b.get("batting_order")),
                              key=lambda b: b["batting_order"])
            if len(starters) >= 8:
                t["lineup"] = [b["batter_id"] for b in starters[:9]]
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
    hs = g["teams"][h]["starter"]
    as_ = g["teams"][a]["starter"]
    return {
        h: {"runs_for": g["teams"][a]["runs_allowed"],
            "pitchers": g["teams"][h]["pitchers"],
            "batters": g["teams"][h].get("batters") or [],
            "opp": a, "opp_sp": as_, "venue_team": h},
        a: {"runs_for": g["teams"][h]["runs_allowed"],
            "pitchers": g["teams"][a]["pitchers"],
            "batters": g["teams"][a].get("batters") or [],
            "opp": h, "opp_sp": hs, "venue_team": h},
    }


def _fit_pyth_hfa(train: list[tuple]) -> float:
    """Grid-fit the multiplicative home bump on the TRAIN window only
    (Brier-minimizing) — the pythag path's analog of ci.fit_params."""
    best, best_b = 0.0, None
    for i in range(0, 25):
        hm = i * 0.005
        b = sum((di.pyth_prob(xh * (1 + hm), xa) - y) ** 2
                for xh, xa, y in train) / max(1, len(train))
        if best_b is None or b < best_b:
            best, best_b = hm, b
    return best


def run(games: list[dict], team_only: bool, known_starter: bool = True,
        opp_adj: bool = False, park_adj: bool = False,
        hr_shrink: float = 1.0, pyth: bool = False,
        batter_blend: float = 0.0, bp_fatigue: bool = False,
        xwoba_blend: float = 0.0, xdays: list | None = None,
        platoon_blend: float = 0.0, platoon_prior: float | None = None,
        pdays: list | None = None, throws: dict | None = None) -> dict:
    state = di.DiamondState(opp_adj=opp_adj, park_adj=park_adj,
                            hr_shrink=hr_shrink, batter_blend=batter_blend,
                            bp_fatigue=bp_fatigue, xwoba_blend=xwoba_blend,
                            platoon_blend=platoon_blend,
                            platoon_prior=(platoon_prior
                                           if platoon_prior is not None
                                           else di.PLATOON_PRIOR_PA))
    state.set_pitcher_throws(throws or {})
    train, evald = [], []
    xi, xdays = 0, (xdays or [])
    pi, pdays = 0, (pdays or [])
    for g in games:
        # feed xwOBA + platoon days strictly BEFORE this game date
        # (walk-forward)
        while xi < len(xdays) and xdays[xi][0] < g["date"]:
            state.feed_xwoba(xdays[xi][0], xdays[xi][1])
            xi += 1
        while pi < len(pdays) and pdays[pi][0] < g["date"]:
            state.feed_platoon(pdays[pi][0], pdays[pi][1])
            pi += 1
        hs = g["teams"][g["home"]]["starter"] if known_starter else None
        as_ = g["teams"][g["away"]]["starter"] if known_starter else None
        proj = state.project(g["home"], g["away"], g["date"], hs, as_,
                             hfa=0.0, team_only=team_only,
                             home_lineup=g["teams"][g["home"]].get("lineup"),
                             away_lineup=g["teams"][g["away"]].get("lineup"))
        if proj is not None:
            rec = (proj["margin"], proj["xr_home_env"],
                   proj["xr_away_env"], g["home_won"])
            (evald if g["date"] >= EVAL_START else train).append(rec)
        state.update(g["date"], _team_update_rows(g))

    n = len(evald)
    if n == 0:
        return {"n": 0}
    if pyth:
        p_train = [(xh, xa, y) for _m, xh, xa, y in train]
        hfa = _fit_pyth_hfa(p_train) if p_train else 0.03
        scale = float("nan")
        probs = [(di.pyth_prob(xh * (1 + hfa), xa), y)
                 for _m, xh, xa, y in evald]
    else:
        m_train = [(m, y) for m, _xh, _xa, y in train]
        hfa, scale = ci.fit_params(m_train) if m_train else (di.DEFAULT_HFA,
                                                             di.DEFAULT_SCALE)
        probs = [(ci.margin_to_prob(m + hfa, scale), y)
                 for m, _xh, _xa, y in evald]
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
    b_rows = load_batter_rows(sb)
    games = group_games(rows, b_rows)
    with_lineup = sum(1 for g in games
                      if g["teams"][g["home"]].get("lineup")
                      and g["teams"][g["away"]].get("lineup"))
    log.info("loaded %d pitcher rows + %d batter rows → %d games "
             "(%s → %s), %d with full lineups", len(rows), len(b_rows),
             len(games), games[0]["date"] if games else "-",
             games[-1]["date"] if games else "-", with_lineup)
    log.info("eval season (>= %s): %d games", EVAL_START,
             sum(1 for g in games if g["date"] >= EVAL_START))

    xdays = load_xwoba_days(sb)
    log.info("xwOBA feed: %d days (%s → %s)", len(xdays),
             xdays[0][0] if xdays else "-", xdays[-1][0] if xdays else "-")
    pdays = load_platoon_days(sb)
    throws = load_pitcher_throws(sb)
    log.info("platoon feed: %d days (%s → %s) · %d pitcher hands (%d LHP)",
             len(pdays), pdays[0][0] if pdays else "-",
             pdays[-1][0] if pdays else "-", len(throws),
             sum(1 for h in throws.values() if h == "L"))
    _champ = {"pyth": True, "opp_adj": True, "park_adj": True,
              "hr_shrink": 2.5, "batter_blend": 1.0, "bp_fatigue": True}
    variants = [
        ("ITER3 CHAMPION (lineup wOBA + fatigue) — control", dict(_champ)),
        ("ITER5 platoon blend 0.35 (prior 600)",
         dict(_champ, platoon_blend=0.35)),
        ("ITER5 platoon blend 0.7 (prior 600)",
         dict(_champ, platoon_blend=0.7)),
        ("ITER5 platoon blend 1.0 (prior 600)",
         dict(_champ, platoon_blend=1.0)),
        ("ITER5 platoon blend 1.0, prior 300 (lighter shrink)",
         dict(_champ, platoon_blend=1.0, platoon_prior=300.0)),
    ]
    for name, kw in variants:
        team_only = bool(kw.get("team_only"))
        extra = {k: v for k, v in kw.items() if k != "team_only"}
        r = run(games, team_only, True, xdays=xdays, pdays=pdays,
                throws=throws, **extra)
        print(f"\n=== {name} ===")
        if r["n"] == 0:
            print("  no eval games")
            continue
        print(f"  eval n={r['n']} (train n={r['n_train']}) · "
              f"fit hfa={r['hfa']:.3f} scale={r['scale']:.2f}")
        print(f"  accuracy {r['acc']:.1%} vs home-base {r['home_base']:.1%}")
        print(f"  Brier {r['brier']:.4f} vs base-rate {r['base_brier']:.4f}"
              f"  (dead team core: 52.5%)")
        print(f"  calibration (favorite buckets): {r['calibration']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
