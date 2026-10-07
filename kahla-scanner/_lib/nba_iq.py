"""Hoops IQ — the NBA possession model (sides + totals).

    points = possessions × points-per-possession

Two ridge regressions, refit on every game date off a recency-weighted
window (numpy, ~3k rows × 66 cols — milliseconds):

  EFFICIENCY  100·pts/poss = μ + off[team] − def[opp] + hfa·home
                              + b2b·own_b2b + ob2b·opp_b2b
  PACE        log(poss)    = μp + pace[home] + pace[away]

Ridge pulls every team term to 0 (league average) — that is the cold-start
prior. Recency weight decays in SEASON TIME (the off-season does not age a
rating), times `carry` per season back (roster churn).

AVAILABILITY. Ratings are built from games the roster actually played. For
each game we also know who is out (the inactive list is published before
tip; historically = who did not log minutes). Each player carries a shrunk,
decayed on-court +/- per minute and an expected-minutes figure; a team's
missing value / missing points feed the calibration regression, whose
coefficients are FITTED walk-forward — never hand-set.

CALIBRATION. actual ~ a + b·raw (+ availability terms), residual sd →
normal tails for P(home win), P(cover line), P(over line). Raw projections
are always shrunk by the fit (every sport in this repo projected too
extreme; never price off the raw number).
"""
from __future__ import annotations

import math
from collections import defaultdict

import numpy as np

SEASON_DAYS = 175          # in-season length used to build season time


def poss_of(b: dict) -> float | None:
    try:
        tov = b.get("totalTurnovers")
        if tov is None:
            tov = b.get("turnovers")
        p = (b["fieldGoalsAttempted"] - b["offensiveRebounds"] + tov
             + 0.44 * b["freeThrowsAttempted"])
        return p if 60 < p < 140 else None
    except Exception:
        return None


def ncdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


class HoopsIQ:
    def __init__(self, hl_days=40.0, carry=0.6, ridge_eff=60.0, ridge_pace=60.0,
                 reg_min=15.0, pm_prior_min=600.0, pl_hl_games=12.0):
        self.hl, self.carry = hl_days, carry
        self.ridge_eff, self.ridge_pace = ridge_eff, ridge_pace
        self.reg_min, self.pm_prior_min, self.pl_hl = reg_min, pm_prior_min, pl_hl_games
        self.games = []            # (stime, season, home, away, poss, hpts, apts, hb2b, ab2b)
        self.teams: dict[str, int] = {}
        self.last_date: dict[str, float] = {}     # team → abs day of last game
        # player state per (team, aid): [w_min, w_pm, w_n, last_day, pts_w]
        self.pl = defaultdict(lambda: [0.0, 0.0, 0.0, -1e9, 0.0])
        self.pl_team: dict[str, str] = {}
        self.roster = defaultdict(set)
        self._fit = None
        self._fit_key = None

    # ── time ─────────────────────────────────────────────────────────────
    @staticmethod
    def season_time(season: int, day: float, season_start: dict) -> float:
        st = season_start.setdefault(season, day)
        return season * SEASON_DAYS + min(day - st, SEASON_DAYS)

    def _tid(self, t):
        if t not in self.teams:
            self.teams[t] = len(self.teams)
            self._fit = None
        return self.teams[t]

    # ── ratings ──────────────────────────────────────────────────────────
    def fit(self, stime_now: float, season_now: int):
        key = (stime_now, len(self.games))
        if self._fit_key == key and self._fit is not None:
            return self._fit
        n = len(self.teams)
        if not self.games or n < 2:
            return None
        g = np.array([(x[0], x[1]) for x in self.games])
        age = stime_now - g[:, 0]
        w = 0.5 ** (age / self.hl) * self.carry ** (season_now - g[:, 1])
        keep = w > 0.005
        idx = np.nonzero(keep)[0]
        w = w[keep]
        m = len(idx)
        H = np.array([self.teams[self.games[i][2]] for i in idx])
        A = np.array([self.teams[self.games[i][3]] for i in idx])
        poss = np.array([self.games[i][4] for i in idx])
        hp = np.array([self.games[i][5] for i in idx])
        ap = np.array([self.games[i][6] for i in idx])
        hb = np.array([self.games[i][7] for i in idx], float)
        ab = np.array([self.games[i][8] for i in idx], float)

        # efficiency: 2 rows per game. cols: μ | off(n) | def(n) | hfa | b2b | ob2b
        k = 2 * n + 4
        X = np.zeros((2 * m, k))
        y = np.empty(2 * m)
        r = np.arange(m)
        X[r, 0] = 1; X[r, 1 + H] = 1; X[r, 1 + n + A] = -1; X[r, 2 * n + 1] = 1
        X[r, 2 * n + 2] = hb; X[r, 2 * n + 3] = ab
        y[:m] = 100 * hp / poss
        r2 = r + m
        X[r2, 0] = 1; X[r2, 1 + A] = 1; X[r2, 1 + n + H] = -1
        X[r2, 2 * n + 2] = ab; X[r2, 2 * n + 3] = hb
        y[m:] = 100 * ap / poss
        W = np.concatenate([w, w])
        R = np.zeros(k); R[1:1 + 2 * n] = self.ridge_eff
        XtW = X.T * W
        be = np.linalg.solve(XtW @ X + np.diag(R) + 1e-9 * np.eye(k), XtW @ y)

        # pace: 1 row per game. cols: μ | pace(n)
        P = np.zeros((m, n + 1)); P[r, 0] = 1
        np.add.at(P, (r, 1 + H), 1); np.add.at(P, (r, 1 + A), 1)
        Rp = np.zeros(n + 1); Rp[1:] = self.ridge_pace
        PtW = P.T * w
        bp = np.linalg.solve(PtW @ P + np.diag(Rp) + 1e-9 * np.eye(n + 1), PtW @ np.log(poss))
        self._fit = {"n": n, "eff": be, "pace": bp, "n_games": m}
        self._fit_key = key
        return self._fit

    def project(self, f, home, away, hb2b, ab2b):
        n = f["n"]; be = f["eff"]; bp = f["pace"]
        h, a = self.teams.get(home), self.teams.get(away)
        off = lambda t: be[1 + t] if t is not None else 0.0
        dfn = lambda t: be[1 + n + t] if t is not None else 0.0
        pc = lambda t: bp[1 + t] if t is not None else 0.0
        poss = math.exp(bp[0] + pc(h) + pc(a))
        eh = be[0] + off(h) - dfn(a) + be[2 * n + 1] + be[2 * n + 2] * hb2b + be[2 * n + 3] * ab2b
        ea = be[0] + off(a) - dfn(h) + be[2 * n + 2] * ab2b + be[2 * n + 3] * hb2b
        ph, pa = poss * eh / 100, poss * ea / 100
        return {"poss": poss, "home_pts": ph, "away_pts": pa,
                "margin": ph - pa, "total": ph + pa}

    # ── availability ─────────────────────────────────────────────────────
    def regulars(self, team, day):
        out = {}
        for aid in self.roster.get(team, ()):
            s = self.pl[(team, aid)]
            if self.pl_team.get(aid) != team:
                continue
            if day - s[3] > 30 or s[2] <= 0:
                continue
            exp_min = s[0] / s[2]
            if exp_min < self.reg_min:
                continue
            v = s[1] / (s[0] + self.pm_prior_min)   # shrunk +/- per minute
            out[aid] = (exp_min, v, s[4] / s[2])
        return out

    def availability(self, team, day, played_ids):
        regs = self.regulars(team, day)
        miss = [regs[a] for a in regs if a not in played_ids]
        return {"miss_val": sum(m * v for m, v, _ in miss),
                "miss_min": sum(m for m, _, _ in miss),
                "miss_pts": sum(p for _, _, p in miss),
                "n_miss": len(miss)}

    def _update_players(self, team, day, rows):
        dec = 0.5 ** (1.0 / self.pl_hl)
        for aid, _name, mn, _st, pm, pts in rows:
            s = self.pl[(team, aid)]
            if mn and mn > 0:
                s[0] = s[0] * dec + mn
                s[1] = s[1] * dec + (pm or 0.0)
                s[2] = s[2] * dec + 1.0
                s[4] = s[4] * dec + (pts or 0.0)
                s[3] = day
                self.pl_team[aid] = team
            else:
                # appeared for this team without minutes: still on roster
                s[0] *= dec; s[1] *= dec; s[2] *= dec; s[4] *= dec
                self.pl_team.setdefault(aid, team)
            self.roster[team].add(aid)

    # ── ingest a finished game ───────────────────────────────────────────
    def add(self, stime, season, day, g, poss, hb2b, ab2b):
        self._tid(g["home"]); self._tid(g["away"])
        self.games.append((stime, season, g["home"], g["away"], poss,
                           g["hs"], g["as"], hb2b, ab2b))
        self._fit = None
        for side, team in (("home", g["home"]), ("away", g["away"])):
            self._update_players(team, day, (g.get("players") or {}).get(side) or [])
            self.last_date[team] = day


def calib_fit(rows, cols, ycol):
    """OLS of y on [1, cols...]; returns (coef, resid_sd)."""
    X = np.array([[1.0] + [r[c] for c in cols] for r in rows])
    y = np.array([r[ycol] for r in rows])
    coef, *_ = np.linalg.lstsq(X, y, rcond=None)
    res = y - X @ coef
    return coef, float(np.sqrt(np.mean(res ** 2)))


def calib_pred(coef, r, cols):
    return float(coef[0] + sum(coef[i + 1] * r[c] for i, c in enumerate(cols)))
