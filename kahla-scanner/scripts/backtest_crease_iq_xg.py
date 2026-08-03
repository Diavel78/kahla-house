"""Crease IQ P3 — walk-forward backtest of the SHOT-QUALITY (xG) model.

The P2 verdict said the lever is shot quality, not more team math. This
replays the same nhl_goalie_games universe with per-shot xG from
nhl_shot_events:

    xG model    : logistic P(goal) on distance/angle/type/strength,
                  fit on the TRAIN season only, frozen for eval.
    team layer  : xG generation / suppression rates (replacing raw
                  shot counts × shooting%).
    goalie layer: GSAx as a RATIO — goals allowed / xG faced, shrunk
                  toward 1.0 by xG faced — the shot-quality-adjusted
                  version of the sv% term that graded out in P2.
    finishing   : goals for / xG for (shooter talent above location).

Empty-net attempts are excluded everywhere. Same eval protocol as P2:
hfa+scale fit on train preds, scored once on the 2025-26 season, with
the P2 champion re-run as the side-by-side control.

  python -m scripts.backtest_crease_iq_xg
"""
from __future__ import annotations

import logging
import sys
from collections import defaultdict
from datetime import date

from _lib import crease_iq as ci
from _lib import nhl_xg as xgm
from scripts.backtest_crease_iq import (load_rows, group_games,
                                        _team_update_rows, run as p2_run,
                                        EVAL_START)
from storage import supabase_client as db

log = logging.getLogger(__name__)


def load_shots(sb) -> list[dict]:
    out, page = [], 0
    while True:      # explicit paging — the 1,000-row lesson
        rows = (sb.table("nhl_shot_events")
                .select("game_id,game_date,event_type,is_goal,situation,"
                        "goalie_id,x,y,shot_type")
                .order("game_date").order("game_id").order("event_id")
                .range(page * 1000, page * 1000 + 999).execute().data) or []
        out.extend(rows)
        if len(rows) < 1000:
            break
        page += 1
    return out


def build_shot_layer(shots: list[dict], goalie_home: dict) -> tuple:
    """(train_samples, eval_samples, per_game) — feature/label pairs for
    the xG fit split by season, and per-game aggregates keyed by game_id
    (built AFTER the model exists via score_games)."""
    train, evald, usable = [], [], []
    for s in shots:
        gid = s.get("goalie_id")
        if not gid:
            continue                      # empty net → excluded everywhere
        gh = goalie_home.get((s["game_id"], gid))
        if gh is None:
            continue                      # goalie not in the spine game
        sk = xgm.skater_diff_from_situation(s.get("situation"), gh)
        f = xgm.shot_features(s.get("x"), s.get("y"), s.get("shot_type"), sk)
        if f is None:
            continue
        y = 1 if s.get("is_goal") else 0
        rec = (s["game_id"], date.fromisoformat(s["game_date"]), gh, gid, f, y)
        usable.append(rec)
        (train if rec[1] < EVAL_START else evald).append((f, y))
    return train, evald, usable


def score_games(model, usable) -> dict:
    """game_id → {'home_xg','away_xg','home_gf','away_gf',
    'goalies': {gid: [xg_faced, goals_allowed]}} (non-EN attempts).
    Shooter side = opposite the goalie's side."""
    per_game: dict = {}
    for gid_game, _d, goalie_is_home, goalie_id, f, y in usable:
        p = model.predict(f)
        g = per_game.setdefault(gid_game, {"home_xg": 0.0, "away_xg": 0.0,
                                           "home_gf": 0, "away_gf": 0,
                                           "goalies": {}})
        side = "away" if goalie_is_home else "home"    # the SHOOTING side
        g[side + "_xg"] += p
        g[side + "_gf"] += y
        gl = g["goalies"].setdefault(goalie_id, [0.0, 0])
        gl[0] += p
        gl[1] += y
    return per_game


class XGState:
    """Walk-forward xG-rate state (teams keyed by tricode, like P2)."""

    def __init__(self, team_hl=60.0, goalie_hl=180.0, goalie_prior_xg=30.0,
                 team_prior=8.0, min_games=10):
        self.team_hl = team_hl
        self.goalie_hl = goalie_hl
        self.goalie_prior_xg = goalie_prior_xg
        self.team_prior = team_prior
        self.min_games = min_games
        self.xgf: dict = {}
        self.xga: dict = {}
        self.finish: dict = {}            # goals/xG for, weighted by xG
        self.goalie: dict = {}            # goals-allowed/xG-faced ratio
        self.team_games: dict = {}
        self.lg_xg = ci._Decayed()        # league xG / team-game
        self.lg_finish = ci._Decayed()

    def _rate(self, table, key, d, hl, lg, prior_w):
        acc = table.get(key)
        if acc is None:
            return lg
        w, m = acc.mean(d, hl)
        return (m * w + lg * prior_w) / (w + prior_w) if (w + prior_w) > 0 else lg

    def project(self, home, away, d, home_goalie, away_goalie,
                use_goalie=True, use_finish=True) -> dict | None:
        if (self.team_games.get(home, 0) < self.min_games
                or self.team_games.get(away, 0) < self.min_games):
            return None
        _, lg_xg = self.lg_xg.mean(d, self.team_hl)
        if not lg_xg:
            return None
        _, lg_fin = self.lg_finish.mean(d, self.team_hl)
        lg_fin = lg_fin or 1.0
        pw = self.team_prior
        xgf_h = self._rate(self.xgf, home, d, self.team_hl, lg_xg, pw)
        xgf_a = self._rate(self.xgf, away, d, self.team_hl, lg_xg, pw)
        xga_h = self._rate(self.xga, home, d, self.team_hl, lg_xg, pw)
        xga_a = self._rate(self.xga, away, d, self.team_hl, lg_xg, pw)
        xg_h = lg_xg * (xgf_h / lg_xg) * (xga_a / lg_xg)
        xg_a = lg_xg * (xgf_a / lg_xg) * (xga_h / lg_xg)
        g_h, g_a = xg_h, xg_a
        if use_finish:
            g_h *= (self._rate(self.finish, home, d, self.team_hl, lg_fin,
                               pw * 2.5) / lg_fin)
            g_a *= (self._rate(self.finish, away, d, self.team_hl, lg_fin,
                               pw * 2.5) / lg_fin)
        if use_goalie:
            g_h *= self._goalie_ratio(away_goalie, d)   # away goalie faces home
            g_a *= self._goalie_ratio(home_goalie, d)
        return {"margin": g_h - g_a}

    def _goalie_ratio(self, goalie_id, d) -> float:
        acc = self.goalie.get(goalie_id) if goalie_id else None
        if acc is None:
            return 1.0
        w, m = acc.mean(d, self.goalie_hl)
        return (m * w + 1.0 * self.goalie_prior_xg) / (w + self.goalie_prior_xg)

    def update(self, d, home, away, agg):
        """agg = per_game entry for this game (may be None → team games
        still count so warm-up matches the universe)."""
        if agg:
            pairs = ((home, agg["home_xg"], agg["home_gf"], agg["away_xg"]),
                     (away, agg["away_xg"], agg["away_gf"], agg["home_xg"]))
            for team, xf, gf, xa in pairs:
                self.xgf.setdefault(team, ci._Decayed()).add(d, xf, 1.0, self.team_hl)
                self.xga.setdefault(team, ci._Decayed()).add(d, xa, 1.0, self.team_hl)
                self.lg_xg.add(d, xf, 1.0, self.team_hl)
                if xf > 0.5:
                    self.finish.setdefault(team, ci._Decayed()).add(
                        d, gf / xf, xf, self.team_hl)
                    self.lg_finish.add(d, gf / xf, xf, self.team_hl)
            for gid, (xf, ga) in (agg.get("goalies") or {}).items():
                if xf > 0.05:
                    self.goalie.setdefault(gid, ci._Decayed()).add(
                        d, ga / xf, xf, self.goalie_hl)
        for t in (home, away):
            self.team_games[t] = self.team_games.get(t, 0) + 1


def collect_xg(games, per_game, use_goalie, use_finish, known_starter=True,
               **params):
    state = XGState(**params)
    preds = []
    for g in games:
        hg = g["teams"][g["home"]]["starter"] if known_starter else None
        ag = g["teams"][g["away"]]["starter"] if known_starter else None
        proj = state.project(g["home"], g["away"], g["date"], hg, ag,
                             use_goalie=use_goalie, use_finish=use_finish)
        if proj is not None:
            preds.append((g["date"], proj["margin"], g["home_won"]))
        state.update(g["date"], g["home"], g["away"], per_game.get(g["game_id"]))
    return preds


def score(preds) -> dict:
    train = [(m, y) for d, m, y in preds if d < EVAL_START]
    evald = [(m, y) for d, m, y in preds if d >= EVAL_START]
    if not evald:
        return {"n": 0}
    hfa, scale = ci.fit_params(train) if train else (ci.DEFAULT_HFA,
                                                     ci.DEFAULT_SCALE)
    probs = [(ci.margin_to_prob(m + hfa, scale), y) for m, y in evald]
    n = len(probs)
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
    goalie_home = {(r["game_id"], r["goalie_id"]): bool(r.get("home"))
                   for r in rows if r.get("goalie_id")}
    log.info("goalie spine: %d rows → %d games", len(rows), len(games))
    shots = load_shots(sb)
    log.info("shot spine: %d unblocked attempts", len(shots))
    train_s, eval_s, usable = build_shot_layer(shots, goalie_home)
    log.info("xG samples: train %d · eval %d · usable %d "
             "(EN + unmatched dropped: %d)", len(train_s), len(eval_s),
             len(usable), len(shots) - len(usable))
    model = xgm.fit_xg(train_s)
    if model is None:
        log.error("xG fit failed — not enough train samples")
        return 2
    print("\n=== xG MODEL (fit on train season, frozen) ===")
    print("  train calibration:")
    for line in xgm.calibration_table(model, train_s):
        print("   ", line)
    print("  EVAL-season calibration (frozen model — the honesty check):")
    for line in xgm.calibration_table(model, eval_s):
        print("   ", line)
    per_game = score_games(model, usable)
    log.info("scored %d games with xG aggregates", len(per_game))

    variants = [
        ("xG CORE (team xG rates only — no goalie, no finishing)",
         dict(use_goalie=False, use_finish=False)),
        ("xG + GOALIE GSAx-ratio (prior 30 xG)",
         dict(use_goalie=True, use_finish=False)),
        ("xG + FINISHING (no goalie)",
         dict(use_goalie=False, use_finish=True)),
        ("xG FULL (goalie + finishing)",
         dict(use_goalie=True, use_finish=True)),
        ("xG FULL, goalie prior 60 (heavier shrink)",
         dict(use_goalie=True, use_finish=True, goalie_prior_xg=60.0)),
        ("xG FULL, NO-STARTER control (league goalie)",
         dict(use_goalie=True, use_finish=True, known_starter=False)),
    ]
    for name, kw in variants:
        known = kw.pop("known_starter", True)
        r = score(collect_xg(games, per_game, known_starter=known, **kw))
        print(f"\n=== {name} ===")
        if r["n"] == 0:
            print("  no eval games")
            continue
        print(f"  eval n={r['n']} (train n={r['n_train']}) · "
              f"fit hfa={r['hfa']:.2f} scale={r['scale']:.2f}")
        print(f"  accuracy {r['acc']:.1%} vs home-base {r['home_base']:.1%}")
        print(f"  Brier {r['brier']:.4f} vs base-rate {r['base_brier']:.4f}")
        print(f"  calibration: {r['calibration']}")

    r = p2_run(games, False, True)
    print("\n=== P2 CHAMPION control (shots × sv%, same eval) ===")
    print(f"  accuracy {r['acc']:.1%} · Brier {r['brier']:.4f} "
          f"(P2 verdict: 54.4-55.0% / ~0.247)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
