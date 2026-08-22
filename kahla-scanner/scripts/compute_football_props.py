#!/usr/bin/env python3
"""Serialize the football-props model state -> football_props_snapshot.

The betting-side half of the NFL props lane (user, Aug 22 2026: "NFL
props after that" — ordered the day the K lane died). MIRRORS the
gate-1-passed model in backtest_football_props.py EXACTLY — per-player
decay-weighted mean/SD (126d half-life), shrunk _SHRINK_GAMES games
toward a usage-floored league prior, normal tail. All 22 rungs across
the four families beat base rate on the walk-forward; receptions was
the strongest (+.004-.025).

The snapshot ships RAW per-player game rows (last _KEEP_GAMES per
player) plus the league priors, and the app-side tail mirror does the
decay/shrink at bet time — the Whiff IQ arrangement, so a stale
snapshot degrades to "no number", never a wrong one.

NAME-KEYED, like whiff_iq_snapshot: PMM questions name players by NAME,
not id. Two active players sharing a name are DROPPED (the whiff
ambiguous-duplicate rule) — a wrong-player projection is worse than no
projection.

The CAPTURE side (question regexes, the autobet leg) deliberately does
NOT exist yet: zero football prop rows have ever landed on our tape, so
the shapes would be guesses — and blind shape-guessing on the money
path failed twice on Aug 22 alone (the anchorless-rung bet, the tuple
URL). Wire it from real prop_snapshots captures when football props
list. Run: probe (default, no write) | --commit.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from storage.supabase_client import get_client          # noqa: E402

# ── model constants — MIRROR backtest_football_props.py, keep in step ──
HALF_LIFE_DAYS = 126.0
SHRINK_GAMES = 5.0
MIN_PRIOR = 3          # games of history before a player is priceable
KEEP_GAMES = 20        # per-player history shipped in the snapshot
LG_WINDOW_D = 400      # league-prior population window (covers a season)
ACTIVE_WINDOW_D = 400  # a player with no game inside this is not shipped

# fam -> (stat column, usage column, usage floor)
FAMS = {
    "pass_yds":   ("pass_yds", "pass_att", 15.0),
    "rush_yds":   ("rush_yds", "rush_att", 8.0),
    "receptions": ("rec", "rec_tgts", 4.0),
    "rec_yds":    ("rec_yds", "rec_tgts", 4.0),
}
# per-game stat vector shipped per row (order is the app mirror's contract)
STAT_COLS = ("pass_yds", "pass_att", "rush_yds", "rush_att",
             "rec", "rec_tgts", "rec_yds")


def _fetch(sb) -> list[dict]:
    rows: list[dict] = []
    cols = "player_id,player_name,team,game_date," + ",".join(STAT_COLS)
    for page in range(120):   # gotcha #40 — PostgREST caps a response at 1,000
        got = (sb.table("football_player_games").select(cols)
               .eq("sport", "NFL").order("game_date").order("id")
               .range(page * 1000, page * 1000 + 999).execute().data) or []
        rows.extend(got)
        if len(got) < 1000:
            break
    return rows


def build_state(rows: list[dict]) -> dict:
    today = datetime.now(timezone.utc).date()
    lg_cut = (today - timedelta(days=LG_WINDOW_D)).isoformat()
    act_cut = (today - timedelta(days=ACTIVE_WINDOW_D)).isoformat()

    by_pid: dict = defaultdict(list)
    pid_name: dict = {}
    pid_team: dict = {}
    for r in rows:
        pid = r.get("player_id")
        if not pid:
            continue
        by_pid[pid].append(r)
        nm = (r.get("player_name") or "").strip()
        if nm:
            pid_name[pid] = nm
        if r.get("team"):
            pid_team[pid] = r["team"]

    # league priors: usage-floored population inside the window
    lg: dict = {}
    for fam, (stat_c, use_c, floor) in FAMS.items():
        vals = [float(r[stat_c]) for r in rows
                if str(r.get("game_date") or "") >= lg_cut
                and r.get(stat_c) is not None
                and r.get(use_c) is not None and float(r[use_c]) >= floor]
        if len(vals) < 50:
            continue
        m = sum(vals) / len(vals)
        v = sum((x - m) ** 2 for x in vals) / max(len(vals) - 1, 1)
        lg[fam] = {"m": round(m, 3), "v": round(v, 3), "n": len(vals)}

    # name index with the ambiguous-duplicate DROP: only players ACTIVE in
    # the window count as claimants (a 2023 retiree must not blank his
    # namesake), but a name two active players share is gone.
    claimants: dict = defaultdict(list)
    for pid, gr in by_pid.items():
        if not gr or str(gr[-1].get("game_date") or "") < act_cut:
            continue
        nm = pid_name.get(pid)
        if nm:
            claimants[nm].append(pid)

    players: dict = {}
    dropped_dupes = 0
    for nm, pids in claimants.items():
        if len(pids) != 1:
            dropped_dupes += 1
            continue
        pid = pids[0]
        games = []
        for r in by_pid[pid][-KEEP_GAMES:]:
            games.append([str(r.get("game_date") or "")[:10]]
                         + [(float(r[c]) if r.get(c) is not None else None)
                            for c in STAT_COLS])
        if len(games) >= MIN_PRIOR:
            players[nm] = {"team": pid_team.get(pid), "games": games}

    return {"built_for": today.isoformat(),
            "half_life_days": HALF_LIFE_DAYS,
            "shrink_games": SHRINK_GAMES,
            "min_prior": MIN_PRIOR,
            "stat_cols": list(STAT_COLS),
            "fams": {f: {"stat": c[0], "use": c[1], "floor": c[2]}
                     for f, c in FAMS.items()},
            "lg": lg, "players": players,
            "dropped_dupe_names": dropped_dupes}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()
    sb = get_client()
    rows = _fetch(sb)
    if not rows:
        print("no football_player_games rows — refusing to write an empty "
              "snapshot")
        return 1
    state = build_state(rows)
    blob = json.dumps(state)
    print(f"rows={len(rows)} players={len(state['players'])} "
          f"lg_fams={sorted(state['lg'])} "
          f"dupes_dropped={state['dropped_dupe_names']} "
          f"size={len(blob) / 1e6:.1f}MB")
    for fam, p in state["lg"].items():
        print(f"  lg {fam}: mean={p['m']} sd={math.sqrt(p['v']):.1f} "
              f"n={p['n']}")
    if not args.commit:
        print("probe only — rerun with --commit to write")
        return 0
    sb.table("football_props_snapshot").upsert({
        "id": 1, "state": state,
        "engine": "fbprops-v1 (backtest_football_props mirror)",
        "built_at": datetime.now(timezone.utc).isoformat(),
    }).execute()
    print("snapshot written")
    return 0


if __name__ == "__main__":
    sys.exit(main())
