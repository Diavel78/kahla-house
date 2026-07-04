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


class DiamondState:
    """Walk-forward rolling state over pitcher-table game rows."""

    def __init__(self, sp_prior: float = SP_PRIOR_OUTS,
                 sp_hl: float = SP_HL_DAYS,
                 team_hl: float = TEAM_HL_DAYS,
                 sp_share: float = SP_SHARE):
        self.sp_prior = sp_prior
        self.sp_hl = sp_hl
        self.team_hl = team_hl
        self.sp_share = sp_share
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
            # shrink each per-out rate toward league by outs
            r = (total * w + (lg_r / 27.0) * self.sp_prior) / (w + self.sp_prior)
            rates.append(r * 27.0)                      # back to per-9
        return self._fip_from_rates(*rates), w

    def project(self, home: str, away: str, d: date,
                home_sp: int | None, away_sp: int | None,
                hfa: float = 0.0, team_only: bool = False) -> dict | None:
        if (self.team_games.get(home, 0) < MIN_TEAM_GAMES
                or self.team_games.get(away, 0) < MIN_TEAM_GAMES):
            return None
        _, lg_rpg = self.lg_rpg.mean(d, self.team_hl)
        if not lg_rpg:
            return None
        lg_fip = self.league_fip(d)
        lg_rates = tuple(self.lg_fip_parts[k].mean(d, self.team_hl)[1] * 27.0
                         for k in ("k", "bb", "hr"))
        _, lg_bp = self.lg_bp.mean(d, self.team_hl)
        pw = TEAM_PRIOR_GAMES
        off_h = self._shrunk(self.team_off.get(home), d, self.team_hl, lg_rpg, pw)
        off_a = self._shrunk(self.team_off.get(away), d, self.team_hl, lg_rpg, pw)

        def prevention(team: str, sp_pid: int | None) -> float:
            """Multiplicative run-prevention factor (1.0 = league)."""
            if team_only:
                return 1.0
            fip, _ = self.starter_fip(sp_pid, d, lg_fip, lg_rates)
            sp_factor = fip / lg_fip if lg_fip > 0 else 1.0
            bp = self._shrunk(self.team_bp.get(team), d, self.team_hl,
                              lg_bp or 0.16, BP_PRIOR_OUTS)
            bp_factor = bp / lg_bp if lg_bp else 1.0
            return self.sp_share * sp_factor + (1 - self.sp_share) * bp_factor

        xr_h = lg_rpg * (off_h / lg_rpg) * prevention(away, away_sp)
        xr_a = lg_rpg * (off_a / lg_rpg) * prevention(home, home_sp)
        return {"xr_home": xr_h, "xr_away": xr_a,
                "margin": xr_h - xr_a + hfa}

    # ---- update (call AFTER predicting the game) ---------------------------
    def update(self, d: date, team_rows: dict[str, dict]):
        """team_rows: {team: {runs_for, pitchers: [row-dicts of this team]}}"""
        for team, r in team_rows.items():
            rf = r.get("runs_for")
            if rf is not None:
                self.team_off.setdefault(team, _Decayed()).add(
                    d, rf, 1.0, self.team_hl)
                self.lg_rpg.add(d, rf, 1.0, self.team_hl)
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
