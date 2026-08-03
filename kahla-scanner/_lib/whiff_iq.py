"""Whiff IQ — pitcher strikeout-prop model (v1, Aug 2026).

Prices the PMM K ladder ("Will {starter} record at least L pitching
strikeouts?") from spines we already own:
  - mlb_pitcher_games — the pitcher's own K + batters-faced history
    (actual starters, leakage-free)
  - mlb_batter_games  — the OPPOSING lineup's whiff propensity (team
    K per PA, known pre-game from team identity alone — no lineup wait)

The projection has three parts:
  1. LEASH — expected batters faced: decay-weighted mean of the
     pitcher's recent starts' BF, shrunk toward the league starter mean.
  2. MATCHUP K RATE — odds-ratio (log5) blend: pitcher K/BF × opponent
     team K/PA relative to league. Both decay-weighted + shrunk.
  3. DISTRIBUTION — P(K ≥ L) from a binomial in the matchup rate MIXED
     over a discretized-normal BF distribution (leash uncertainty is
     most of the variance at the ladder's quoted rungs).

Pure Python, no deps — same posture as diamond_iq/crease_iq. The
walk-forward backtest (scripts/backtest_whiff_iq.py) is the judge.
"""
from __future__ import annotations

import math
from datetime import date

# Decay half-lives (days). Pitcher skill moves slowly; team lineup
# composition (trades, call-ups) moves faster.
HL_PITCHER = 365.0
HL_TEAM = 150.0

# Shrinkage priors (in BF / PA equivalents at full weight).
PRIOR_BF = 150.0          # pitcher K% prior weight
PRIOR_PA = 1000.0         # team K% prior weight
PRIOR_STARTS = 4.0        # leash prior weight (starts)

# League fallbacks — overwritten by fitted values from the train pass.
LG_K_BF = 0.22            # starter K per batter faced
LG_BF = 22.0              # starter batters faced
BF_SD = 4.5               # residual sd of BF around the leash projection
BF_LO, BF_HI = 6, 42      # BF mixture support (clip + renormalize)


def _decay(age_days: float, half_life: float) -> float:
    if age_days <= 0:
        return 1.0
    return 0.5 ** (age_days / half_life)


def _odds(p: float) -> float:
    p = min(max(p, 1e-6), 1.0 - 1e-6)
    return p / (1.0 - p)


def log5_rate(p_pitcher: float, p_opp: float, p_lg: float) -> float:
    """Odds-ratio matchup rate: pitcher vs this lineup, league-relative."""
    o = _odds(p_pitcher) * _odds(p_opp) / _odds(p_lg)
    return o / (1.0 + o)


class WhiffState:
    """Walk-forward state. feed_* strictly AFTER projecting a date."""

    def __init__(self):
        # pitcher_id -> [(date, bf, k)] (starts only — the leash is a
        # starter concept; relief cameos would poison it)
        self.pitcher: dict[int, list[tuple[date, int, int]]] = {}
        # team -> [(date, pa, k)] per game aggregate (batting side)
        self.team: dict[str, list[tuple[date, int, int]]] = {}
        # league running sums (undecayed — stable denominators)
        self.lg_k = 0
        self.lg_bf = 0
        self.lg_bf_sum = 0.0
        self.lg_starts = 0

    # ---- feeds -------------------------------------------------------
    def feed_pitcher_game(self, pitcher_id: int, d: date, started: bool,
                          bf: int, k: int) -> None:
        if not started or bf <= 0:
            return
        self.pitcher.setdefault(pitcher_id, []).append((d, bf, k))
        self.lg_k += k
        self.lg_bf += bf
        self.lg_bf_sum += bf
        self.lg_starts += 1

    def feed_team_batting(self, team: str, d: date, pa: int, k: int) -> None:
        if pa <= 0:
            return
        self.team.setdefault(team, []).append((d, pa, k))

    # ---- league ------------------------------------------------------
    def league_k_bf(self) -> float:
        if self.lg_bf < 2000:
            return LG_K_BF
        return self.lg_k / self.lg_bf

    def league_bf(self) -> float:
        if self.lg_starts < 100:
            return LG_BF
        return self.lg_bf_sum / self.lg_starts

    # ---- components --------------------------------------------------
    def pitcher_rates(self, pitcher_id: int, asof: date
                      ) -> tuple[float, float, int]:
        """(k_per_bf, expected_bf, n_starts) — decayed + shrunk."""
        lg_k = self.league_k_bf()
        lg_bf = self.league_bf()
        rows = self.pitcher.get(pitcher_id) or []
        wk = wbf = wn = 0.0
        for d, bf, k in rows:
            w = _decay((asof - d).days, HL_PITCHER)
            wk += w * k
            wbf += w * bf
            wn += w
        k_rate = (wk + PRIOR_BF * lg_k) / (wbf + PRIOR_BF)
        e_bf = (wbf + PRIOR_STARTS * lg_bf) / (wn + PRIOR_STARTS)
        return k_rate, e_bf, len(rows)

    def team_k_pa(self, team: str, asof: date) -> float:
        lg = self.league_k_bf()
        rows = self.team.get(team) or []
        wk = wpa = 0.0
        for d, pa, k in rows:
            w = _decay((asof - d).days, HL_TEAM)
            wk += w * k
            wpa += w * pa
        return (wk + PRIOR_PA * lg) / (wpa + PRIOR_PA)

    # ---- projection --------------------------------------------------
    def project(self, pitcher_id: int, opp_team: str, asof: date,
                use_opponent: bool = True) -> dict:
        """{'e_bf','k_rate','e_k','tail':{L: P(K>=L)}} for L in 1..12."""
        p_k, e_bf, n = self.pitcher_rates(pitcher_id, asof)
        lg = self.league_k_bf()
        if use_opponent:
            opp = self.team_k_pa(opp_team, asof)
            rate = log5_rate(p_k, opp, lg)
        else:
            rate = p_k
        pmf = k_pmf(e_bf, rate)
        tail: dict[int, float] = {}
        acc = 0.0
        for L in range(len(pmf) - 1, 0, -1):
            acc += pmf[L]
            if L <= 12:
                tail[L] = acc
        return {"e_bf": e_bf, "k_rate": rate,
                "e_k": e_bf * rate, "n_starts": n, "tail": tail}


def k_pmf(e_bf: float, rate: float, bf_sd: float | None = None
          ) -> list[float]:
    """pmf of K: Binomial(bf, rate) mixed over a discretized normal on
    bf ∈ [BF_LO, BF_HI]. Index = strikeouts."""
    sd = BF_SD if bf_sd is None else bf_sd
    weights: list[tuple[int, float]] = []
    tot = 0.0
    for bf in range(BF_LO, BF_HI + 1):
        z = (bf - e_bf) / sd
        w = math.exp(-0.5 * z * z)
        weights.append((bf, w))
        tot += w
    pmf = [0.0] * (BF_HI + 1)
    for bf, w in weights:
        w /= tot
        # iterative binomial pmf over k = 0..bf
        q = 1.0 - rate
        p_k = q ** bf                      # k = 0
        pmf[0] += w * p_k
        for k in range(1, bf + 1):
            p_k *= (bf - k + 1) / k * (rate / q)
            pmf[k] += w * p_k
    return pmf
