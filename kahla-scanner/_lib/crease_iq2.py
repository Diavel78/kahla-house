"""Crease IQ v2 — the NHL game model behind the hockey pick sheet.

Built Sep 30 2026 (opening night, Rob: "get that model way better ...
we have a great one for football, we can do better"). Replaces the P2/P3
logistic-on-margin engines (Brier 0.2467 at best) with a SCORE model:

    per game, per side:  E[non-empty-net regulation goals]
        = exp(b0 + b1·offense + b2·opp_defense + b3·home
              + b4·log(opp goalie GA/xG) + b5·b2b_self + b6·b2b_opp
              + b7·special-teams xG differential)

    offense / defense = log(rate / league), rate = w_xg·xG + (1−w_xg)·goals
        (non-EN, all situations, exp-decayed per team, shrunk to league)

    joint regulation score = bivariate Poisson (shared λ3) with the
        diagonal inflated to the observed tie rate — hockey ties far more
        often (22%) than two independent Poissons say (16%): score effects
    + the EMPTY-NET transition measured from our own games (a one-goal
        lead becomes two 40% of the time; two becomes three 64%)
    + OT/shootout: home wins the coin flip ~52%; a tie adds exactly one
        goal to the final score (OT goal or the shootout winner's +1)

One joint distribution prices EVERYTHING the sheet shows: moneyline,
expected final margin (the "spread" Rob asked to see), puck lines, and
totals at any line. Walk-forward backtest (fit on prior seasons only,
scored on each held-out season; the 2025-26 season is fully clean):

    ML Brier  0.2378 over 4,184 games 2023-26 (58.2%) vs base 0.2483
    2025-26 alone 0.2456 (54.7%) — a hard season (base 0.2496)
    old Crease IQ P3 xG core: 0.2467 on that same season
    puck line −1.5 calibrated (pred .331 / act .329)
    TOTALS: no measurable edge over the base rate (Brier .2462 vs .2463
        at 5.5) — the total is printed as our number, and a total pick
        is capped at LEAN. Don't promote totals without a new backtest.

Pure Python (no numpy) so it runs on the box, Actions and Vercel alike.
Research harness: scripts/backtest_crease_iq2.py.
"""
from __future__ import annotations

import math
from collections import defaultdict
from datetime import date

# ------------------------------------------------------------------ xG model
# Frozen logistic P(goal | unblocked attempt), fit on 2022-23 + 2023-24
# (242,969 attempts, logloss .2219 train / .2209 on 2024-26 held out).
# Feature order = nhl_xg.shot_features(...) + _expand() extras.
_XG_W = [-3.101266, 1.236965, -1.14076, 0.066547, 0.027839, 0.092114,
         0.238255, 0.212724, -0.067768, -0.12052, -0.052914, -0.075026,
         -0.000397, 0.04153, -1.694973, -0.318251, -0.284012, 0.112147,
         0.499131]
_XG_MU = [33.198744, 14.774864, 0.577723, 0.151628, 0.535735, 0.119958,
          0.147932, 0.072911, 0.087139, 0.0202, 0.00838, 0.059975,
          -0.092292, 3.336411, 0.489084, 17.52376, 4.965519, 17.419017]
_XG_SD = [19.373432, 15.394217, 0.394106, 0.438435, 0.498721, 0.324912,
          0.355033, 0.25999, 0.282038, 0.140684, 0.091156, 0.23744,
          1.242227, 0.672737, 0.632235, 13.226063, 12.910632, 4.51626]
_SHOT_TYPES = ("wrist", "slap", "snap", "backhand", "tip-in",
               "deflected", "wrap-around")


def xg_prob(x, y, shot_type, skater_diff, rebound, score_diff) -> float | None:
    if x is None or y is None:
        return None
    dx = 89.0 - abs(float(x))
    dy = float(y)
    d = math.hypot(dx, dy)
    ang = math.atan2(abs(dy), dx)
    st = (shot_type or "").strip().lower()
    sk = float(max(-2, min(2, skater_diff)))
    f = ([d, d * d / 100.0, ang, sk]
         + [1.0 if st == t else 0.0 for t in _SHOT_TYPES]
         + [float(rebound), float(max(-2, min(2, score_diff)))]
         + [math.log1p(d), ang * ang, d * ang, (1.0 if sk > 0 else 0.0) * d,
            min(d, 20.0)])
    z = _XG_W[0]
    for j, v in enumerate(f):
        z += _XG_W[j + 1] * (v - _XG_MU[j]) / _XG_SD[j]
    z = max(-30.0, min(30.0, z))
    return 1.0 / (1.0 + math.exp(-z))


def _skater_diff(situation, goalie_is_home) -> int:
    s = str(situation or "")
    if len(s) != 4 or not s.isdigit() or goalie_is_home is None:
        return 0
    away_sk, home_sk = int(s[1]), int(s[2])
    return (away_sk - home_sk) if goalie_is_home else (home_sk - away_sk)


def _tsec(p, tip) -> int:
    try:
        mm, ss = str(tip).split(":")
        return (int(p) - 1) * 1200 + int(mm) * 60 + int(ss)
    except (TypeError, ValueError):
        return 0


# --------------------------------------------------------- game assembly
def build_games(goalie_rows: list[dict], shot_rows: list[dict]) -> list[dict]:
    """nhl_goalie_games + nhl_shot_events → one record per finished game:
    regulation goals (EN split out), final score, xG for/against (regular
    periods, EN excluded), per-goalie xG faced / non-EN goals, starters."""
    games: dict = {}
    goalie_team = {}
    for r in goalie_rows:
        g = games.setdefault(r["game_id"], {"game_id": r["game_id"],
                                            "date": r["game_date"],
                                            "type": r.get("game_type"),
                                            "teams": {}})
        t = g["teams"].setdefault(r["team"], {"home": bool(r.get("home")),
                                              "starter": None, "win": False,
                                              "goalies": {}})
        if r.get("starter"):
            t["starter"] = r["goalie_id"]
        if (r.get("decision") or "").upper() == "W":
            t["win"] = True
        t["goalies"].setdefault(r["goalie_id"], {"xg": 0.0, "g": 0})
        goalie_team[(r["game_id"], r["goalie_id"])] = r["team"]

    # team_id → tricode (shooter = the team NOT in net)
    votes: dict = defaultdict(lambda: defaultdict(int))
    for s in shot_rows:
        gl, tid = s.get("goalie_id"), s.get("team_id")
        if not gl or tid is None:
            continue
        gt = goalie_team.get((s["game_id"], gl))
        g = games.get(s["game_id"])
        if gt and g:
            others = [t for t in g["teams"] if t != gt]
            if len(others) == 1:
                votes[tid][others[0]] += 1
    tri = {k: max(v, key=v.get) for k, v in votes.items()}

    by_game: dict = defaultdict(list)
    for s in shot_rows:
        by_game[s["game_id"]].append(s)
    for gid, rows in by_game.items():
        g = games.get(gid)
        if not g or len(g["teams"]) != 2:
            continue
        for T in g["teams"].values():
            T.update({"xg": 0.0, "xg_ev": 0.0, "reg": 0, "ot": 0, "en": 0})
        rows.sort(key=lambda r: _tsec(r.get("period") or 1,
                                      r.get("time_in_period")))
        last_sog: dict = {}
        goals: dict = defaultdict(int)
        for s in rows:
            per = s.get("period") or 1
            if g.get("type") == 2 and per >= 5:
                continue                                  # shootout
            shooter = tri.get(s.get("team_id"))
            T = g["teams"].get(shooter) if shooter else None
            if T is None:
                continue
            t = _tsec(per, s.get("time_in_period"))
            lab = 1 if s.get("is_goal") else 0
            gl = s.get("goalie_id")
            if gl:
                gt = goalie_team.get((gid, gl))
                gh = g["teams"].get(gt, {}).get("home") if gt else None
                reb = 1.0 if (shooter in last_sog
                              and 0 < t - last_sog[shooter] <= 3) else 0.0
                opp = sum(v for k, v in goals.items() if k != shooter)
                p = xg_prob(s.get("x"), s.get("y"), s.get("shot_type"),
                            _skater_diff(s.get("situation"), gh), reb,
                            goals[shooter] - opp)
                if p is not None and gt:
                    if per <= 3:
                        T["xg"] += p
                        if _skater_diff(s.get("situation"), gh) == 0:
                            T["xg_ev"] += p
                    gg = g["teams"][gt]["goalies"].setdefault(gl, {"xg": 0.0, "g": 0})
                    gg["xg"] += p
                    gg["g"] += lab
            if lab:
                if per <= 3:
                    T["reg"] += 1
                    if not gl:
                        T["en"] += 1
                else:
                    T["ot"] += 1
            if s.get("event_type") == "shot-on-goal":
                last_sog[shooter] = t
            if lab:
                goals[shooter] += 1

    out = []
    for gid, g in games.items():
        if len(g["teams"]) != 2 or "reg" not in next(iter(g["teams"].values())):
            continue
        (a, A), (b, B) = g["teams"].items()
        h, H, w, W = (a, A, b, B) if A["home"] else (b, B, a, A)
        if H["home"] == W["home"] or (H["win"] == W["win"]):
            continue
        fh, fa = H["reg"] + H["ot"], W["reg"] + W["ot"]
        if fh == fa:                                      # shootout
            fh += 1 if H["win"] else 0
            fa += 1 if W["win"] else 0
        out.append({
            "game_id": gid, "date": g["date"], "home": h, "away": w,
            "home_won": 1 if H["win"] else 0,
            "reg_h": H["reg"], "reg_a": W["reg"], "en_h": H["en"], "en_a": W["en"],
            "final_h": fh, "final_a": fa,
            "xg_h": H["xg"], "xg_a": W["xg"], "xgev_h": H["xg_ev"], "xgev_a": W["xg_ev"],
            "starter_h": H["starter"], "starter_a": W["starter"],
            "goalies_h": H["goalies"], "goalies_a": W["goalies"],
        })
    out.sort(key=lambda r: (r["date"], r["game_id"]))
    return out


# ------------------------------------------------------------ rolling state
PARAMS = dict(team_hl=60.0, goalie_hl=365.0, team_prior=10.0,
              goalie_prior=40.0, min_games=8, w_xg=0.7, ridge=1.0)


class _Acc:
    __slots__ = ("w", "s", "last")

    def __init__(self):
        self.w = 0.0
        self.s = 0.0
        self.last: date | None = None

    def _f(self, d, hl):
        return 1.0 if self.last is None else 0.5 ** (max(0, (d - self.last).days) / hl)

    def add(self, d, v, wt, hl):
        f = self._f(d, hl)
        self.w, self.s, self.last = self.w * f + wt, self.s * f + v * wt, d

    def get(self, d, hl, prior, pw):
        f = self._f(d, hl)
        return (self.s * f + prior * pw) / (self.w * f + pw)


class State:
    def __init__(self, P: dict | None = None):
        self.P = dict(PARAMS, **(P or {}))
        self.T: dict = defaultdict(lambda: defaultdict(_Acc))
        self.G: dict = defaultdict(_Acc)
        self.gxg: dict = defaultdict(float)       # career xG faced (context)
        self.n: dict = defaultdict(int)
        self.last: dict = {}
        self.lg = defaultdict(_Acc)

    def league(self, d):
        thl = self.P["team_hl"]
        lx = self.lg["xg"].get(d, thl, 2.9, 1e-9) if self.lg["xg"].last else 2.9
        lgg = self.lg["g"].get(d, thl, 3.0, 1e-9) if self.lg["g"].last else 3.0
        return lx, lgg

    def team(self, t, d):
        P = self.P
        lx, lgg = self.league(d)
        thl, tp = P["team_hl"], P["team_prior"]
        acc = self.T[t]
        return {"xgf": acc["xgf"].get(d, thl, lx, tp),
                "xga": acc["xga"].get(d, thl, lx, tp),
                "gf": acc["gf"].get(d, thl, lgg, tp),
                "ga": acc["ga"].get(d, thl, lgg, tp),
                "st": acc["st"].get(d, thl, 0.0, tp),
                "n": self.n.get(t, 0)}

    def goalie(self, gid, d) -> float:
        if not gid or gid not in self.G:
            return 1.0
        return self.G[gid].get(d, self.P["goalie_hl"], 1.0, self.P["goalie_prior"])

    def rest(self, t, d) -> int:
        return (d - self.last[t]).days if t in self.last else 5

    def features(self, home, away, d, g_home, g_away) -> dict | None:
        if self.n.get(home, 0) < self.P["min_games"] or self.n.get(away, 0) < self.P["min_games"]:
            return None
        lx, lgg = self.league(d)
        return {"lx": lx, "lg": lgg, "h": self.team(home, d), "a": self.team(away, d),
                "gk_h": self.goalie(g_home, d), "gk_a": self.goalie(g_away, d),
                "b2b_h": 1.0 if self.rest(home, d) == 1 else 0.0,
                "b2b_a": 1.0 if self.rest(away, d) == 1 else 0.0}

    def update(self, g: dict):
        d = date.fromisoformat(g["date"]) if isinstance(g["date"], str) else g["date"]
        thl, ghl = self.P["team_hl"], self.P["goalie_hl"]
        for t, xf, xa, gf, ga, enf, ena, gls, evf, eva in (
                (g["home"], g["xg_h"], g["xg_a"], g["reg_h"], g["reg_a"], g["en_h"], g["en_a"],
                 g["goalies_h"], g["xgev_h"], g["xgev_a"]),
                (g["away"], g["xg_a"], g["xg_h"], g["reg_a"], g["reg_h"], g["en_a"], g["en_h"],
                 g["goalies_a"], g["xgev_a"], g["xgev_h"])):
            A = self.T[t]
            A["xgf"].add(d, xf, 1, thl)
            A["xga"].add(d, xa, 1, thl)
            A["gf"].add(d, gf - enf, 1, thl)
            A["ga"].add(d, ga - ena, 1, thl)
            A["st"].add(d, (xf - evf) - (xa - eva), 1, thl)
            self.lg["xg"].add(d, xf, 1, thl)
            self.lg["g"].add(d, gf - enf, 1, thl)
            self.n[t] = self.n.get(t, 0) + 1
            self.last[t] = d
            for gid, v in (gls or {}).items():
                x = float(v.get("xg") or 0.0)
                if x > 0.05:
                    self.G[int(gid)].add(d, (v.get("g") or 0) / x, x, ghl)
                    self.gxg[int(gid)] += x


def design_row(f: dict, side: str, w_xg: float) -> list[float]:
    o = "a" if side == "h" else "h"
    off, de = f[side], f[o]
    oq = w_xg * math.log(off["xgf"] / f["lx"]) + (1 - w_xg) * math.log(max(off["gf"], .5) / f["lg"])
    dq = w_xg * math.log(de["xga"] / f["lx"]) + (1 - w_xg) * math.log(max(de["ga"], .5) / f["lg"])
    return [1.0, oq, dq, 1.0 if side == "h" else 0.0,
            math.log(max(f["gk_" + o], 0.5)),
            f["b2b_" + side], f["b2b_" + o],
            off["st"] - de["st"]]


def _solve(A, b):
    n = len(b)
    M = [row[:] + [b[i]] for i, row in enumerate(A)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(M[r][c]))
        M[c], M[p] = M[p], M[c]
        if abs(M[c][c]) < 1e-12:
            return [0.0] * n
        for r in range(n):
            if r != c:
                k = M[r][c] / M[c][c]
                for j in range(c, n + 1):
                    M[r][j] -= k * M[c][j]
    return [M[i][n] / M[i][i] for i in range(n)]


def fit_poisson(X: list[list[float]], y: list[float], ridge: float = 1.0) -> list[float]:
    k = len(X[0])
    b = [0.0] * k
    b[0] = math.log(max(sum(y) / len(y), 1e-6))
    for _ in range(25):
        H = [[0.0] * k for _ in range(k)]
        gr = [0.0] * k
        for x, yy in zip(X, y):
            mu = math.exp(sum(bi * xi for bi, xi in zip(b, x)))
            r = yy - mu
            for i in range(k):
                gr[i] += x[i] * r
                xi_mu = x[i] * mu
                for j in range(i, k):
                    H[i][j] += xi_mu * x[j]
        for i in range(k):
            for j in range(i):
                H[i][j] = H[j][i]
            if i:
                H[i][i] += ridge
                gr[i] -= ridge * b[i]
        step = _solve(H, gr)
        b = [bi + si for bi, si in zip(b, step)]
        if max(abs(s) for s in step) < 1e-7:
            break
    return b


# ---------------------------------------------------- score distribution
_N = 13


def _pmf(lam: float) -> list[float]:
    out, p = [], math.exp(-lam)
    for k in range(_N):
        out.append(p)
        p *= lam / (k + 1)
    return out


def score_matrix(lh: float, la: float, l3: float, delta: float, en: dict) -> list[list[float]]:
    """Joint regulation score (home i, away j) incl. empty-net goals."""
    l3 = min(l3, 0.9 * min(lh, la))
    A, B, C = _pmf(lh - l3), _pmf(la - l3), _pmf(l3)
    M = [[0.0] * _N for _ in range(_N)]
    for c in range(_N):
        if C[c] < 1e-12:
            break
        for i in range(_N - c):
            for j in range(_N - c):
                M[i + c][j + c] += C[c] * A[i] * B[j]
    if delta > 0:
        tr = sum(M[i][i] for i in range(_N)) or 1.0
        for i in range(_N):
            for j in range(_N):
                M[i][j] *= (1 - delta)
            M[i][i] += delta * (M[i][i] / (1 - delta)) / tr
    out = [[0.0] * _N for _ in range(_N)]
    for i in range(_N):
        for j in range(_N):
            p = M[i][j]
            m = i - j
            if m == 0:
                out[i][j] += p
                continue
            for k, q in enumerate(en[str(min(abs(m), 3))]):
                if m > 0:
                    out[min(i + k, _N - 1)][j] += p * q
                else:
                    out[i][min(j + k, _N - 1)] += p * q
    return out


def en_transition(games: list[dict]) -> dict:
    c: dict = defaultdict(lambda: [0, 0, 0])
    for g in games:
        m = (g["reg_h"] - g["en_h"]) - (g["reg_a"] - g["en_a"])
        if m == 0:
            continue
        k = min(g["en_h"] if m > 0 else g["en_a"], 2)
        c[min(abs(m), 3)][k] += 1
    out = {}
    for m in (1, 2, 3):
        n = sum(c[m]) or 1
        out[str(m)] = [v / n for v in c[m]] if sum(c[m]) else [1.0, 0.0, 0.0]
    return out


# --------------------------------------------------------------- the model
def fit(games: list[dict], P: dict | None = None) -> tuple[State, dict]:
    """Walk every finished game; fit the goal GLM + distribution params on
    the walk-forward features. Returns (state as of the last game, params)."""
    st = State(P)
    X, Y, pairs = [], [], []
    for g in games:
        d = date.fromisoformat(g["date"])
        f = st.features(g["home"], g["away"], d, g["starter_h"], g["starter_a"])
        if f is not None:
            X.append(design_row(f, "h", st.P["w_xg"]))
            Y.append(g["reg_h"] - g["en_h"])
            X.append(design_row(f, "a", st.P["w_xg"]))
            Y.append(g["reg_a"] - g["en_a"])
            pairs.append(g)
        st.update(g)
    b = fit_poisson(X, Y, st.P["ridge"])
    lam = [math.exp(sum(bi * xi for bi, xi in zip(b, x))) for x in X]
    # λ3 by likelihood on a recent slice (cheap grid)
    tail = list(range(max(0, len(pairs) - 2600), len(pairs)))
    best = (None, 0.0)
    en0 = {"1": [1, 0, 0], "2": [1, 0, 0], "3": [1, 0, 0]}
    for l3 in (0.0, 0.05, 0.1, 0.15, 0.2, 0.25, 0.3):
        ll = 0.0
        for i in tail:
            M = score_matrix(lam[2 * i], lam[2 * i + 1], l3, 0.0, en0)
            ll += math.log(max(M[min(int(Y[2 * i]), _N - 1)][min(int(Y[2 * i + 1]), _N - 1)], 1e-12))
        if best[0] is None or ll > best[0]:
            best = (ll, l3)
    l3 = best[1]
    pt = sum(sum(score_matrix(lam[2 * i], lam[2 * i + 1], l3, 0.0, en0)[k][k]
                 for k in range(_N)) for i in tail) / len(tail)
    at = sum(1 for i in tail if Y[2 * i] == Y[2 * i + 1]) / len(tail)
    delta = max(0.0, (at - pt) / (1 - pt))
    en = en_transition(games)
    ot = [g for g in games if g["reg_h"] == g["reg_a"]]
    ot_home = sum(g["home_won"] for g in ot) / len(ot) if ot else 0.5
    # logit recalibration of ML on the tail
    probs = []
    for i in tail:
        M = score_matrix(lam[2 * i], lam[2 * i + 1], l3, delta, en)
        probs.append((_win_from(M, ot_home), pairs[i]["home_won"]))
    best_k, best_b = 1.0, None
    for kk in [x / 50 for x in range(30, 91)]:
        br = 0.0
        for p, yv in probs:
            p = min(max(p, 1e-6), 1 - 1e-6)
            q = 1 / (1 + math.exp(-kk * math.log(p / (1 - p))))
            br += (q - yv) ** 2
        if best_b is None or br < best_b:
            best_k, best_b = kk, br
    params = {"coef": b, "lam3": l3, "delta": delta, "en": en, "ot_home": ot_home,
              "k": best_k, "n_fit": len(pairs), "w_xg": st.P["w_xg"],
              "tie_rate": at}
    return st, params


def _win_from(M, ot_home) -> float:
    reg = sum(M[i][j] for i in range(_N) for j in range(_N) if i > j)
    tie = sum(M[i][i] for i in range(_N))
    return reg + tie * ot_home


def price(st: State, params: dict, home: str, away: str, d: date,
          g_home: int | None, g_away: int | None,
          totals=(5.5, 6.5), pucks=(1.5,)) -> dict | None:
    f = st.features(home, away, d, g_home, g_away)
    if f is None:
        return None
    b = params["coef"]
    lh = math.exp(sum(bi * xi for bi, xi in zip(b, design_row(f, "h", params["w_xg"]))))
    la = math.exp(sum(bi * xi for bi, xi in zip(b, design_row(f, "a", params["w_xg"]))))
    M = score_matrix(lh, la, params["lam3"], params["delta"], params["en"])
    p = min(max(_win_from(M, params["ot_home"]), 1e-6), 1 - 1e-6)
    p = 1 / (1 + math.exp(-params["k"] * math.log(p / (1 - p))))
    oh = params["ot_home"]
    # final score distribution: a regulation tie adds one goal (OT/SO)
    exp_margin = exp_total = 0.0
    final: dict = defaultdict(float)             # (margin, total) → prob
    for i in range(_N):
        for j in range(_N):
            q = M[i][j]
            if q < 1e-12:
                continue
            if i == j:
                final[(1, i + j + 1)] += q * oh
                final[(-1, i + j + 1)] += q * (1 - oh)
            else:
                final[(i - j, i + j)] += q
    for (m, t), q in final.items():
        exp_margin += m * q
        exp_total += t * q
    out = {
        "lam_home": round(lh, 3), "lam_away": round(la, 3),
        "win_home": round(p, 4),
        "exp_margin": round(exp_margin, 2),          # home − away, final
        "exp_total": round(exp_total, 2),
        "p_ot": round(sum(M[i][i] for i in range(_N)), 4),
        "over": {}, "home_cover": {}, "away_cover": {},
        "goalie_home": round(f["gk_h"], 3), "goalie_away": round(f["gk_a"], 3),
        "b2b_home": bool(f["b2b_h"]), "b2b_away": bool(f["b2b_a"]),
    }
    for L in totals:
        out["over"][str(L)] = round(sum(q for (m, t), q in final.items() if t > L), 4)
    for L in pucks:
        out["home_cover"][str(-L)] = round(sum(q for (m, t), q in final.items() if m > L), 4)
        out["away_cover"][str(-L)] = round(sum(q for (m, t), q in final.items() if -m > L), 4)
        out["home_cover"][str(L)] = round(1 - out["away_cover"][str(-L)], 4)
        out["away_cover"][str(L)] = round(1 - out["home_cover"][str(-L)], 4)
    return out


def over_prob(priced_final: dict, line: float) -> float | None:
    return (priced_final.get("over") or {}).get(str(line))
