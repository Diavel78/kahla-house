"""Diamond IQ Phase 2 — the MLB pitcher-aware projection engine.

The MLB team core is noise (walk-forward 52.5% — you can't predict
baseball without the starting pitcher). This engine rebuilds run
prevention around the ACTUAL starter (from mlb_pitcher_games, so the
backtest is leakage-free — in reality the starter is the probable,
known well before first pitch):

    starter quality  = rolling per-pitcher FIP from game lines,
                       regressed toward league by innings (the
                       `_starter_runs` shrinkage idea, walk-forward)
    bullpen quality  = rolling non-starter runs-allowed rate per team
    run prevention   = 0.6 · starter + 0.4 · bullpen (the live blend)
    xRuns_A          = lg_rpg · (A offense ratio) · (B prevention factor)
    margin           = xRuns_A − xRuns_B (+ HFA) → win prob via logistic

All rolling state is strictly walk-forward — a game only updates state
AFTER it has been predicted. Pure Python; mirrors the crease_iq /
power_ratings architecture (engine here, driver scripts import it).
"""
from __future__ import annotations

from datetime import date

from _lib.crease_iq import _Decayed, margin_to_prob, fit_params  # noqa: F401

# ---------------------------------------------------------------- constants
TEAM_HL_DAYS = 90.0        # offense / bullpen recency half-life
SP_HL_DAYS = 365.0         # starter skill is stable within ~a season
SP_PRIOR_OUTS = 135.0      # shrinkage: 45 IP of league-average work
BP_PRIOR_OUTS = 180.0      # bullpen shrinkage (team-level, cheap prior)
TEAM_PRIOR_GAMES = 10.0    # offense shrinkage (games of league-avg)
MIN_TEAM_GAMES = 15        # don't predict until both teams warm
SP_SHARE = 0.6             # starter share of run prevention (live blend)
FIP_CONST = 3.15
DEFAULT_HFA = 0.10
DEFAULT_SCALE = 1.9
PYTH_EXP = 0.287           # pythagenpat: k = (run environment) ** this

# ITER1 (Aug 2026, user directive: "no steam engine — make it pass the
# backtest"): venue park factors, tricode-keyed to match mlb_pitcher_games
# .team (mirrors handicapper_web._MLB_PARK_FACTORS, which is mascot-keyed
# for the live dossier). Unknown tricode → 100 (neutral, harmless).
PARK_PF: dict[str, float] = {
    "COL": 112, "BOS": 104, "CIN": 104, "NYY": 103, "PHI": 102,
    "BAL": 101, "TEX": 101, "CHC": 101, "AZ": 101, "ARI": 101,
    "KC": 101, "KCR": 101, "LAA": 100, "ATL": 100, "TOR": 100,
    "WSH": 100, "WSN": 100, "MIN": 100, "CWS": 100, "CHW": 100,
    "HOU": 99, "LAD": 99, "STL": 99, "MIL": 99, "PIT": 98,
    "NYM": 98, "CLE": 98, "MIA": 97, "TB": 97, "TBR": 97,
    "DET": 97, "OAK": 97, "ATH": 97, "SD": 96, "SDP": 96,
    "SF": 95, "SFG": 95, "SEA": 94,
}


def pyth_prob(xr_h: float, xr_a: float) -> float:
    """Pythagenpat home win prob — the run-ENVIRONMENT-aware mapping the
    margin→logistic ignores (a 0.5-run edge at Coors is worth less than
    the same edge at Petco; the exponent scales with total scoring)."""
    if xr_h <= 0 or xr_a <= 0:
        return 0.5
    k = max(0.5, (xr_h + xr_a) ** PYTH_EXP)
    ph = xr_h ** k
    return ph / (ph + xr_a ** k)


class DiamondState:
    """Walk-forward rolling state over pitcher-table game rows."""

    def __init__(self, sp_prior: float = SP_PRIOR_OUTS,
                 sp_hl: float = SP_HL_DAYS,
                 team_hl: float = TEAM_HL_DAYS,
                 sp_share: float = SP_SHARE,
                 opp_adj: bool = False, park_adj: bool = False,
                 hr_shrink: float = 1.0):
        self.sp_prior = sp_prior
        self.sp_hl = sp_hl
        self.team_hl = team_hl
        self.sp_share = sp_share
        # ITER1 levers (all default OFF so v1 stays reproducible):
        # opp_adj — offense updates normalized by the opponent's prevention
        #   factor AT UPDATE TIME (walk-forward state, pre-game), removing
        #   schedule bias from raw runs-for; park_adj — runs deflated by the
        #   venue park factor at update, re-inflated for the projection's
        #   run environment; hr_shrink — extra shrinkage multiplier on the
        #   HR/9 component (the noisiest FIP input).
        self.opp_adj = opp_adj
        self.park_adj = park_adj
        self.hr_shrink = hr_shrink
        self.team_off: dict[str, _Decayed] = {}     # runs scored / game
        self.team_bp: dict[str, _Decayed] = {}      # bullpen RA9 (per out)
        self.sp: dict[int, dict[str, _Decayed]] = {}  # per-pitcher K/BB/HR/outs
        self.team_games: dict[str, int] = {}
        self.lg_rpg = _Decayed()                    # league runs / team-game
        self.lg_fip_parts = {k: _Decayed() for k in ("k", "bb", "hr")}
        self.lg_bp = _Decayed()                     # league bullpen runs/out

    # ---- reads ------------------------------------------------------------
    def _shrunk(self, acc: _Decayed | None, d: date, hl: float,
                lg: float, prior_w: float) -> float:
        if acc is None:
            return lg
        w, m = acc.mean(d, hl)
        return (m * w + lg * prior_w) / (w + prior_w) if (w + prior_w) > 0 else lg

    def league_fip(self, d: date) -> float:
        rates = {}
        for k, acc in self.lg_fip_parts.items():
            _, rates[k] = acc.mean(d, self.team_hl)
        return self._fip_from_rates(rates["k"], rates["bb"], rates["hr"])

    @staticmethod
    def _fip_from_rates(k9, bb9, hr9) -> float:
        return (13.0 * hr9 + 3.0 * bb9 - 2.0 * k9) / 9.0 + FIP_CONST

    def starter_fip(self, pid: int | None, d: date, lg_fip: float,
                    lg_rates: tuple[float, float, float]) -> tuple[float, float]:
        """(shrunk FIP, decayed outs seen). Unknown starter → league."""
        st = self.sp.get(pid) if pid else None
        if st is None:
            return lg_fip, 0.0
        w, _ = st["outs"].mean(d, self.sp_hl)          # decayed outs
        if w <= 0:
            return lg_fip, 0.0
        rates = []
        for key, lg_r in zip(("k", "bb", "hr"), lg_rates):
            ww, total = st[key].mean(d, self.sp_hl)     # per-out rate
            # shrink each per-out rate toward league by outs; HR/9 is the
            # noisiest component (xFIP's whole thesis) so it can carry an
            # extra shrinkage multiplier (ITER1 hr_shrink lever)
            prior = self.sp_prior * (self.hr_shrink if key == "hr" else 1.0)
            r = (total * w + (lg_r / 27.0) * prior) / (w + prior)
            rates.append(r * 27.0)                      # back to per-9
        return self._fip_from_rates(*rates), w

    def _prevention(self, team: str | None, sp_pid: int | None,
                    d: date) -> float:
        """Multiplicative run-prevention factor (1.0 = league). Shared by
        project() and the ITER1 opponent-adjusted offense update (which
        needs the opponent's pre-game factor to normalize runs-for).
        Safe pre-warm: missing league state → 1.0."""
        _, lg_rpg = self.lg_rpg.mean(d, self.team_hl)
        if not lg_rpg or not team:
            return 1.0
        lg_fip = self.league_fip(d)
        lg_rates = tuple(self.lg_fip_parts[k].mean(d, self.team_hl)[1] * 27.0
                         for k in ("k", "bb", "hr"))
        _, lg_bp = self.lg_bp.mean(d, self.team_hl)
        fip, _ = self.starter_fip(sp_pid, d, lg_fip, lg_rates)
        sp_factor = fip / lg_fip if lg_fip > 0 else 1.0
        bp = self._shrunk(self.team_bp.get(team), d, self.team_hl,
                          lg_bp or 0.16, BP_PRIOR_OUTS)
        bp_factor = bp / lg_bp if lg_bp else 1.0
        return self.sp_share * sp_factor + (1 - self.sp_share) * bp_factor

    def project(self, home: str, away: str, d: date,
                home_sp: int | None, away_sp: int | None,
                hfa: float = 0.0, team_only: bool = False) -> dict | None:
        if (self.team_games.get(home, 0) < MIN_TEAM_GAMES
                or self.team_games.get(away, 0) < MIN_TEAM_GAMES):
            return None
        _, lg_rpg = self.lg_rpg.mean(d, self.team_hl)
        if not lg_rpg:
            return None
        pw = TEAM_PRIOR_GAMES
        off_h = self._shrunk(self.team_off.get(home), d, self.team_hl, lg_rpg, pw)
        off_a = self._shrunk(self.team_off.get(away), d, self.team_hl, lg_rpg, pw)

        def prevention(team: str, sp_pid: int | None) -> float:
            return 1.0 if team_only else self._prevention(team, sp_pid, d)

        xr_h = lg_rpg * (off_h / lg_rpg) * prevention(away, away_sp)
        xr_a = lg_rpg * (off_a / lg_rpg) * prevention(home, home_sp)
        # Run ENVIRONMENT values: with park_adj the stored offense is
        # park-neutral, so re-inflate both sides by the venue (home team's
        # park) for anything environment-aware (the pythagenpat exponent).
        pf = (PARK_PF.get(home, 100.0) / 100.0) if self.park_adj else 1.0
        return {"xr_home": xr_h, "xr_away": xr_a,
                "xr_home_env": xr_h * pf, "xr_away_env": xr_a * pf,
                "margin": xr_h - xr_a + hfa}

    # ---- update (call AFTER predicting the game) ---------------------------
    def update(self, d: date, team_rows: dict[str, dict]):
        """team_rows: {team: {runs_for, pitchers: [row-dicts of this team],
        opp?, opp_sp?, venue_team?}} — the optional keys feed the ITER1
        opponent/park offense normalization. MUST be called AFTER the
        game was predicted (the opponent prevention read is pre-game
        state — walk-forward safe by construction)."""
        for team, r in team_rows.items():
            rf = r.get("runs_for")
            if rf is not None:
                val = float(rf)
                if self.park_adj:
                    pf = PARK_PF.get(r.get("venue_team") or "", 100.0) / 100.0
                    if pf > 0:
                        val /= pf
                if self.opp_adj:
                    prev = self._prevention(r.get("opp"), r.get("opp_sp"), d)
                    val /= max(0.7, min(1.4, prev))
                self.team_off.setdefault(team, _Decayed()).add(
                    d, val, 1.0, self.team_hl)
                self.lg_rpg.add(d, float(rf), 1.0, self.team_hl)
            self.team_games[team] = self.team_games.get(team, 0) + 1
            for p in (r.get("pitchers") or []):
                outs = p.get("outs") or 0
                if outs <= 0:
                    continue
                pid = p.get("pitcher_id")
                if p.get("started") and pid:
                    st = self.sp.setdefault(pid, {
                        "outs": _Decayed(), "k": _Decayed(),
                        "bb": _Decayed(), "hr": _Decayed()})
                    st["outs"].add(d, 1.0, outs, self.sp_hl)   # weight = outs
                    for key, col in (("k", "strikeouts"), ("bb", "walks"),
                                     ("hr", "home_runs")):
                        st[key].add(d, (p.get(col) or 0) / outs, outs, self.sp_hl)
                        self.lg_fip_parts[key].add(
                            d, (p.get(col) or 0) / outs, outs, self.team_hl)
                elif not p.get("started"):
                    runs = p.get("runs")
                    if runs is not None:
                        self.team_bp.setdefault(team, _Decayed()).add(
                            d, runs / outs, outs, self.team_hl)
                        self.lg_bp.add(d, runs / outs, outs, self.team_hl)
