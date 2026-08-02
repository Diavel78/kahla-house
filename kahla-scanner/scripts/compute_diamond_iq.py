"""Serialize the Diamond IQ walk-forward state to a live snapshot.

"The steam engine dies tonight" (user, Aug 2 2026): MLB moneyline bets
come from the MODEL. Flask can't import kahla-scanner, so this script
(daily cron + dispatch) replays mlb_pitcher_games through the ITER1
engine and writes current team offense / bullpen / per-pitcher FIP
factors + league context to diamond_iq_snapshot — everything app.py's
`_diamond_ml` needs to price a game from names alone.

  python -m scripts.compute_diamond_iq
"""
from __future__ import annotations

import logging
import sys
from datetime import date

from _lib import diamond_iq as di
from scripts.backtest_diamond_iq import (load_rows, load_batter_rows,
                                         group_games, _team_update_rows)
from storage import supabase_client as db

log = logging.getLogger(__name__)

# Full-name substring (lowercase) → statsapi tricode, for Flask's
# event_name → snapshot-team lookup. Two-token mascots listed fully so
# 'sox' can't mismatch. A miss on either team = no model price = no bet
# (safe-degrade; the compute log prints the snapshot's actual tricodes).
MASCOT_CODE = {
    "diamondbacks": "AZ", "braves": "ATL", "orioles": "BAL",
    "red sox": "BOS", "cubs": "CHC", "white sox": "CWS", "reds": "CIN",
    "guardians": "CLE", "rockies": "COL", "tigers": "DET",
    "astros": "HOU", "royals": "KC", "angels": "LAA", "dodgers": "LAD",
    "marlins": "MIA", "brewers": "MIL", "twins": "MIN", "mets": "NYM",
    "yankees": "NYY", "athletics": "ATH", "phillies": "PHI",
    "pirates": "PIT", "padres": "SD", "mariners": "SEA", "giants": "SF",
    "cardinals": "STL", "rays": "TB", "rangers": "TEX",
    "blue jays": "TOR", "nationals": "WSH",
}


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    sb = db.client()
    rows = load_rows(sb)
    names: dict = {}
    for r in rows:
        if r.get("pitcher_id") and r.get("pitcher_name"):
            names[r["pitcher_id"]] = r["pitcher_name"]
    b_rows = load_batter_rows(sb)
    games = group_games(rows, b_rows)
    # THE CHAMPION CONFIG (Aug 2 ablation): full lineup-wOBA offense +
    # bullpen fatigue — 55.7-55.8% / Brier 0.2489, the best Diamond IQ
    # has ever tested.
    state = di.DiamondState(opp_adj=True, park_adj=True, hr_shrink=2.5,
                            batter_blend=1.0, bp_fatigue=True)
    last_lineup: dict = {}
    for g in games:
        for t in (g["home"], g["away"]):
            lu = g["teams"][t].get("lineup")
            if lu:
                last_lineup[t] = lu
        state.update(g["date"], _team_update_rows(g))
    d = date.today()
    _, lg_rpg = state.lg_rpg.mean(d, state.team_hl)
    if not lg_rpg:
        log.error("no league state — is mlb_pitcher_games empty?")
        return 2
    lg_fip = state.league_fip(d)
    lg_rates = tuple(state.lg_fip_parts[k].mean(d, state.team_hl)[1] * 27.0
                     for k in ("k", "bb", "hr"))
    _, lg_bp = state.lg_bp.mean(d, state.team_hl)
    _, lgw = state.lg_woba.mean(d, state.team_hl)
    teams = {}
    for t, n in state.team_games.items():
        off = state._shrunk(state.team_off.get(t), d, state.team_hl,
                            lg_rpg, di.TEAM_PRIOR_GAMES)
        bp = state._shrunk(state.team_bp.get(t), d, state.team_hl,
                           lg_bp or 0.16, di.BP_PRIOR_OUTS)
        # ITER2 live: the team's MOST RECENT lineup as tonight's proxy
        # (openers price ~20h out, before lineups post; day-to-day
        # overlap ~90%). ITER3 live: reliever outs over the last 2 days.
        bf = state._lineup_factor(last_lineup.get(t), d, lgw) if lgw else None
        hist = state.bp_recent.get(t) or {}
        outs2 = sum(v for dd, v in hist.items() if 0 < (d - dd).days <= 2)
        teams[t] = {"off": round(off, 4),
                    "bp_factor": round(bp / lg_bp, 4) if lg_bp else 1.0,
                    "off_bf": (round(bf, 4) if bf is not None else None),
                    "bp_outs2": outs2,
                    "games": n}
    pitchers = {}
    for pid in state.sp:
        fip, w = state.starter_fip(pid, d, lg_fip, lg_rates)
        nm = (names.get(pid) or "").strip().lower()
        if nm and w > 0:
            pitchers[nm] = {"fip_factor": round(fip / lg_fip, 4),
                            "outs": round(w, 1)}
    snap = {"as_of": d.isoformat(), "n_games": len(games),
            "engine": ("diamond_iq_iter3 (lineup wOBA b=1.0 + bp fatigue "
                       "+ opp/park + hr_shrink + pyth — 55.7%/0.2489)"),
            "bp_fatigue_coef": di.BP_FATIGUE_COEF,
            "bp_fatigue_norm": di.BP_FATIGUE_NORM,
            "hfa_mult": 0.08,          # tonight's backtest pyth fit ~0.080
            "sp_share": di.SP_SHARE, "pyth_exp": di.PYTH_EXP,
            "min_team_games": di.MIN_TEAM_GAMES,
            "park_pf": di.PARK_PF, "mascot_code": MASCOT_CODE,
            "lg": {"rpg": round(lg_rpg, 4), "fip": round(lg_fip, 4),
                   "bp": (round(lg_bp, 5) if lg_bp else None)},
            "teams": teams, "pitchers": pitchers}
    sb.table("diamond_iq_snapshot").insert({"snapshot": snap}).execute()
    log.info("snapshot written: %d games · %d teams (%s) · %d pitchers",
             len(games), len(teams), ",".join(sorted(teams)), len(pitchers))
    return 0


if __name__ == "__main__":
    sys.exit(main())
