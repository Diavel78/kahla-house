"""Crease IQ Phase 2 — the NHL goalie-aware projection engine.

The team core is DEAD (walk-forward 53.3% vs 52.7% home baseline, Brier
worse than coinflip, n=912 — re-confirmed twice) and the diagnosis is
the goalie. This engine rebuilds goals-against from first principles:

    expected shots for A  = lg_shots · (A shot generation) · (B shot suppression)
    expected goals for A  = shots_A · lg_miss · (A shooting skill) · (B goalie miss rate)
    margin                = xg_A − xg_B (+ HFA), → win prob via fitted logistic

Goalie quality is a rolling SHOT-WEIGHTED save% with shrinkage toward
league average (a cold goalie with 60 career shots barely moves off the
prior; a 1,500-shot season does) and exponential recency decay. All
rolling state is strictly walk-forward — a game only updates state
AFTER it has been predicted, so the backtest is leakage-free. The
starter identity comes from `nhl_goalie_games.starter` (the actual
starter, known at puck drop in reality via the confirmation window —
the live projected/confirmed feed is P1b, September).

Pure Python, no deps. Mirrors the power_ratings / ufc_model
architecture: engine here, driver scripts import it; Flask (P3) ports
the projection math since it can't import the kahla-scanner subproject.
"""
from __future__ import annotations

import math
from datetime import date

# ---------------------------------------------------------------- constants
TEAM_HL_DAYS = 60.0        # recency half-life for team shot/shooting rates
GOALIE_HL_DAYS = 180.0     # goalies decay slower — skill is more stable
GOALIE_PRIOR_SHOTS = 400.0  # shrinkage: shots of league-average "prior work"
TEAM_PRIOR_GAMES = 8.0     # shrinkage for team rates (games of league-avg)
MIN_TEAM_GAMES = 10        # don't predict until both teams have this many
DEFAULT_HFA = 0.15         # goals; refit from train window
DEFAULT_SCALE = 1.05       # logistic scale (goals); refit from train window


def _decay(w: float, days: float, hl: float) -> float:
    return w * (0.5 ** (days / hl)) if days > 0 else w


class _Decayed:
    """Exponentially-decayed (weight, weighted-sum) accumulator."""
    __slots__ = ("w", "s", "last")

    def __init__(self):
        self.w = 0.0
        self.s = 0.0
        self.last: date | None = None

    def add(self, d: date, value: float, weight: float, hl: float):
        if self.last is not None:
            dt = (d - self.last).days
            self.w = _decay(self.w, dt, hl)
            self.s = _decay(self.s, dt, hl)
        self.last = d
        self.w += weight
        self.s += value * weight

    def mean(self, d: date, hl: float) -> tuple[float, float]:
        """(decayed weight, mean) as of date d."""
        w, s = self.w, self.s
        if self.last is not None:
            dt = (d - self.last).days
            w = _decay(w, dt, hl)
            s = _decay(s, dt, hl)
        return w, (s / w if w > 0 else 0.0)


class CreaseState:
    """Walk-forward rolling state over goalie-table game rows.

    Constants are overridable per instance so the backtest can sweep
    them (selection happens on a train-internal split, never the eval
    season)."""

    def __init__(self, team_hl: float = TEAM_HL_DAYS,
                 goalie_hl: float = GOALIE_HL_DAYS,
                 goalie_prior: float = GOALIE_PRIOR_SHOTS,
                 team_prior: float = TEAM_PRIOR_GAMES,
                 min_games: int = MIN_TEAM_GAMES):
        self.team_hl = team_hl
        self.goalie_hl = goalie_hl
        self.goalie_prior = goalie_prior
        self.team_prior = team_prior
        self.min_games = min_games
        self.team_sf: dict[str, _Decayed] = {}    # shots for / game
        self.team_sa: dict[str, _Decayed] = {}    # shots against / game
        self.team_sh: dict[str, _Decayed] = {}    # shooting% (per shot)
        self.goalie_sv: dict[int, _Decayed] = {}  # save% (per shot)
        self.team_games: dict[str, int] = {}
        self.lg_shots = _Decayed()                # league shots / team-game
        self.lg_sh = _Decayed()                   # league shooting%

    # ---- reads (as of date d, decayed) -----------------------------------
    def _rate(self, table: dict, key, d: date, hl: float,
              lg_mean: float, prior_w: float) -> float:
        acc = table.get(key)
        if acc is None:
            return lg_mean
        w, m = acc.mean(d, hl)
        return (m * w + lg_mean * prior_w) / (w + prior_w) if (w + prior_w) > 0 else lg_mean

    def league(self, d: date) -> tuple[float, float]:
        _, shots = self.lg_shots.mean(d, self.team_hl)
        _, sh = self.lg_sh.mean(d, self.team_hl)
        return (shots or 28.0), (sh or 0.095)

    def goalie_sv_pct(self, goalie_id: int | None, d: date, lg_sv: float) -> tuple[float, float]:
        """(shrunk sv%, decayed shots seen). Unknown goalie → league avg."""
        acc = self.goalie_sv.get(goalie_id) if goalie_id else None
        if acc is None:
            return lg_sv, 0.0
        w, m = acc.mean(d, self.goalie_hl)
        sv = (m * w + lg_sv * self.goalie_prior) / (w + self.goalie_prior)
        return sv, w

    def project(self, home: str, away: str, d: date,
                home_goalie: int | None, away_goalie: int | None,
                hfa: float = DEFAULT_HFA,
                goalie_only: bool = False) -> dict | None:
        """Projected xG margin (home − away). None until both teams warm."""
        if (self.team_games.get(home, 0) < self.min_games
                or self.team_games.get(away, 0) < self.min_games):
            return None
        lg_shots, lg_sh = self.league(d)
        lg_sv = 1.0 - lg_sh
        pw = self.team_prior
        sf_h = self._rate(self.team_sf, home, d, self.team_hl, lg_shots, pw)
        sf_a = self._rate(self.team_sf, away, d, self.team_hl, lg_shots, pw)
        sa_h = self._rate(self.team_sa, home, d, self.team_hl, lg_shots, pw)
        sa_a = self._rate(self.team_sa, away, d, self.team_hl, lg_shots, pw)
        shots_h = lg_shots * (sf_h / lg_shots) * (sa_a / lg_shots)
        shots_a = lg_shots * (sf_a / lg_shots) * (sa_h / lg_shots)
        sv_a_goalie, gw_a = self.goalie_sv_pct(away_goalie, d, lg_sv)
        sv_h_goalie, gw_h = self.goalie_sv_pct(home_goalie, d, lg_sv)
        if goalie_only:
            xg_h = shots_h * (1.0 - sv_a_goalie)
            xg_a = shots_a * (1.0 - sv_h_goalie)
        else:
            sh_h = self._rate(self.team_sh, home, d, self.team_hl, lg_sh,
                              pw * lg_shots)
            sh_a = self._rate(self.team_sh, away, d, self.team_hl, lg_sh,
                              pw * lg_shots)
            xg_h = shots_h * lg_sh * (sh_h / lg_sh) * ((1 - sv_a_goalie) / lg_sh)
            xg_a = shots_a * lg_sh * (sh_a / lg_sh) * ((1 - sv_h_goalie) / lg_sh)
        return {"xg_home": xg_h, "xg_away": xg_a,
                "margin": xg_h - xg_a + hfa,
                "home_goalie_sv": sv_h_goalie, "away_goalie_sv": sv_a_goalie,
                "home_goalie_shots": gw_h, "away_goalie_shots": gw_a}

    # ---- update (call AFTER predicting the game) -------------------------
    def update(self, d: date, team_rows: dict[str, dict]):
        """team_rows: {team: {sf, sa, gf, ga, goalies: [(id, shots, saves)]}}"""
        for team, r in team_rows.items():
            sf, sa, gf = r.get("sf"), r.get("sa"), r.get("gf")
            if sf is None or sa is None:
                continue
            self.team_sf.setdefault(team, _Decayed()).add(d, sf, 1.0, self.team_hl)
            self.team_sa.setdefault(team, _Decayed()).add(d, sa, 1.0, self.team_hl)
            if gf is not None and sf > 0:
                self.team_sh.setdefault(team, _Decayed()).add(
                    d, gf / sf, sf, self.team_hl)
                self.lg_sh.add(d, gf / sf, sf, self.team_hl)
            self.lg_shots.add(d, sf, 1.0, self.team_hl)
            self.team_games[team] = self.team_games.get(team, 0) + 1
            for gid, shots, saves in (r.get("goalies") or []):
                if gid and shots and shots > 0 and saves is not None:
                    self.goalie_sv.setdefault(gid, _Decayed()).add(
                        d, saves / shots, shots, self.goalie_hl)


def margin_to_prob(margin: float, scale: float = DEFAULT_SCALE) -> float:
    return 1.0 / (1.0 + math.exp(-margin / scale))


def fit_params(preds: list[tuple[float, int]]) -> tuple[float, float]:
    """Grid-fit (hfa_adjust, scale) minimizing Brier over train
    (raw_margin_without_hfa, home_won) pairs."""
    best = (DEFAULT_HFA, DEFAULT_SCALE, 1e9)
    for hfa in [x / 100.0 for x in range(0, 71, 5)]:
        for scale in [x / 100.0 for x in range(60, 501, 20)]:
            b = sum((margin_to_prob(m + hfa, scale) - y) ** 2
                    for m, y in preds) / max(len(preds), 1)
            if b < best[2]:
                best = (hfa, scale, b)
    return best[0], best[1]
