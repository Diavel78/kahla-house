"""Football prop families — gate 1: does a per-player model beat base rate?

The pitcher-prop playbook (whiff/outs/hits/walks) transplanted to football
off the football_player_games spine: per-player decay-weighted mean/SD of
the stat, shrunk toward league by games played, normal tail P(stat > line).
Walk-forward and leakage-free:

  * ELIGIBILITY comes from PRIOR games only — a player is in a family's
    population when his trailing usage clears the floor (e.g. trailing
    pass attempts >= 15/game says "this is the guy the ladder is posted
    on"). Nothing about the target game decides eligibility except that
    he appears in its boxscore at all (a scratched player's props void;
    an early exit settles low — that variance is REAL and stays in).
  * The projection uses strictly earlier games; the target game updates
    the state only after it is graded.

GATE 1 ONLY: beat the base rate at fixed rungs. Beating the MARKET is
gate 2 and reads our own prop_snapshots tape once football lists props.

  python -m scripts.backtest_football_props                    # Supabase
  python -m scripts.backtest_football_props --json fpg.json    # sandbox
  python -m scripts.backtest_football_props --family pass_yds
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import sys
from collections import defaultdict
from datetime import datetime

log = logging.getLogger(__name__)

# Exponential decay half-life in DAYS. Football is weekly, so ~126 days
# (= 18 weeks) keeps most of the current season live while last season
# decays to roughly a quarter weight by the opener.
_HALF_LIFE_DAYS = 126.0

# Shrinkage: games of league-average pseudo-observations blended into a
# player's mean/SD. Football samples are tiny (17 games/season) — shrink
# harder than baseball's 4-start prior.
_SHRINK_GAMES = 5.0

# A player needs this many PRIOR games in the family population before his
# games grade (the "3 starts" rule).
_MIN_PRIOR = 3

# family: (stat column, usage column, trailing-usage floor, rungs)
# Rungs are the half-point lines a book actually posts for a mid-range
# player; the grid exercises the probability band like the pitcher rungs.
_FAMS = {
    "pass_yds":  ("pass_yds", "pass_att", 15.0,
                  (174.5, 199.5, 224.5, 249.5, 274.5, 299.5)),
    "rush_yds":  ("rush_yds", "rush_att", 8.0,
                  (29.5, 49.5, 69.5, 89.5, 109.5)),
    "receptions": ("rec", "rec_tgts", 4.0,
                   (1.5, 2.5, 3.5, 4.5, 5.5, 6.5)),
    "rec_yds":   ("rec_yds", "rec_tgts", 4.0,
                  (29.5, 44.5, 59.5, 74.5, 89.5)),
}


def _norm_cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def _fetch(sb) -> list[dict]:
    rows: list[dict] = []
    for page in range(120):   # gotcha #40 — PostgREST caps a response at 1,000
        chunk = (sb.table("football_player_games")
                 .select("game_date,player_id,player_name,"
                         "pass_att,pass_yds,rush_att,rush_yds,rec,rec_tgts,rec_yds")
                 .eq("sport", "NFL")
                 .order("game_date").order("id")
                 .range(page * 1000, page * 1000 + 999)
                 .execute().data) or []
        rows.extend(chunk)
        if len(chunk) < 1000:
            break
    return rows


def run_family(rows: list[dict], fam: str) -> None:
    stat_col, use_col, floor, rungs = _FAMS[fam]

    # chronological player histories: player_id -> list[(date, stat, usage)]
    hist: dict[str, list] = defaultdict(list)
    lg_vals: list[float] = []          # league running list for the prior
    stats = {r: {"n": 0, "base": 0, "b_base": 0.0, "b_model": 0.0}
             for r in rungs}
    calib = defaultdict(lambda: [0.0, 0])

    for row in rows:
        pid = row["player_id"]
        d = datetime.fromisoformat(str(row["game_date"])[:10])
        stat = row.get(stat_col)
        usage = row.get(use_col)
        prior = hist[pid]

        # eligibility from PRIOR games only: trailing usage over the last
        # 5 population-relevant games clears the floor.
        graded = False
        if len(prior) >= _MIN_PRIOR and stat is not None:
            recent = prior[-5:]
            tr_use = sum(u for _, _, u in recent if u is not None) / len(recent)
            if tr_use >= floor and lg_vals:
                # decay-weighted player mean/SD
                w = [0.5 ** ((d - pd).days / _HALF_LIFE_DAYS)
                     for pd, _, _ in prior]
                vals = [v for _, v, _ in prior]
                W = sum(w)
                mu_p = sum(wi * v for wi, v in zip(w, vals)) / W
                var_p = sum(wi * (v - mu_p) ** 2 for wi, v in zip(w, vals)) / W
                # league prior from the SAME population
                lg_m = sum(lg_vals) / len(lg_vals)
                lg_v = (sum((v - lg_m) ** 2 for v in lg_vals)
                        / max(len(lg_vals) - 1, 1))
                k = _SHRINK_GAMES
                mu = (W * mu_p + k * lg_m) / (W + k)
                var = (W * var_p + k * lg_v) / (W + k)
                sd = math.sqrt(max(var, 1.0))
                actual = float(stat)
                for L in rungs:
                    p = 1.0 - _norm_cdf((L - mu) / sd)
                    p = min(max(p, 0.01), 0.99)
                    over = 1.0 if actual > L else 0.0
                    s = stats[L]
                    s["n"] += 1
                    s["base"] += over
                    s["b_model"] += (p - over) ** 2
                    b = int(min(p, 0.9999) * 5) / 5.0
                    calib[b][0] += over
                    calib[b][1] += 1
                graded = True

        # update state AFTER grading (walk-forward)
        if stat is not None:
            prior.append((d, float(stat), usage))
            if graded or (usage is not None and usage >= floor):
                lg_vals.append(float(stat))

    print(f"\n== {fam} (stat={stat_col}, usage {use_col}>={floor}, "
          f"hl={_HALF_LIFE_DAYS:.0f}d, shrink k={_SHRINK_GAMES:.0f})")
    any_n = 0
    for L in rungs:
        s = stats[L]
        if not s["n"]:
            continue
        any_n += s["n"]
        br = s["base"] / s["n"]
        b_base = br * (1 - br)          # Brier of always-predict-base-rate
        print(f"  over {L:6.1f}: n={s['n']:5d}  base {br:.3f}  "
              f"brier base {b_base:.4f}  model {s['b_model']/s['n']:.4f}  "
              f"{'BEATS' if s['b_model']/s['n'] < b_base else 'loses'} "
              f"({b_base - s['b_model']/s['n']:+.4f})")
    print(f"  calibration ({any_n} gradings pooled):")
    for k2, v in sorted(calib.items()):
        if v[1] >= 50:
            print(f"    p {k2:.1f}-{k2+0.2:.1f}  actual {v[0]/v[1]:.3f}  n={v[1]}")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default=None,
                    help="football_player_games rows from a JSON dump "
                         "(the sandbox cannot import the supabase client)")
    ap.add_argument("--family", default=None, choices=list(_FAMS))
    args = ap.parse_args()

    if args.json:
        rows = json.load(open(args.json))
        rows.sort(key=lambda r: (str(r.get("game_date")), str(r.get("id", ""))))
    else:
        from storage import supabase_client as db
        rows = _fetch(db.client())
    print(f"{len(rows)} player-game rows")
    if len(rows) < 1000:
        print("not enough spine — run the backfill first")
        return 1
    for fam in ([args.family] if args.family else list(_FAMS)):
        run_family(rows, fam)
    return 0


if __name__ == "__main__":
    sys.exit(main())
