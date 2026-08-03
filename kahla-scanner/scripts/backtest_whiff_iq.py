"""Whiff IQ — walk-forward backtest of the pitcher K-prop model.

Replays mlb_pitcher_games + mlb_batter_games chronologically: every
START is projected from state built STRICTLY from earlier dates, then
the date's games update state (date-grain feed = no intra-day leakage,
doubleheader-safe). The starter identity is the ACTUAL starter — the
same leakage-free luxury the Diamond IQ backtest rides.

Split: train < 2026-03-01, eval = the 2026 season. BF_SD (the leash
residual spread that widens the K distribution) is fitted on train
residuals, then frozen for eval scoring.

Reports, per ladder rung L ∈ 3..8 (the PMM quote range):
  Brier of P(K ≥ L)  — FULL vs NO-OPPONENT control vs train base rate
plus MAE of E[K] vs the naive train-mean-K baseline, and a calibration
table at L=6 (the most-quoted rung).

  python -m scripts.backtest_whiff_iq
"""
from __future__ import annotations

import sys
from collections import defaultdict
from datetime import date

from _lib import whiff_iq as wq
from storage import supabase_client as db

EVAL_START = date(2026, 3, 1)
LINES = (3, 4, 5, 6, 7, 8)


def _paged(sb, table: str, cols: str, order: list[str]) -> list[dict]:
    out, page = [], 0
    while True:      # explicit paging — the 1,000-row lesson
        q = sb.table(table).select(cols)
        for c in order:
            q = q.order(c)
        rows = (q.range(page * 1000, page * 1000 + 999)
                .execute().data) or []
        out.extend(rows)
        if len(rows) < 1000:
            break
        page += 1
    return out


def main() -> int:
    sb = db.client()
    prows = _paged(sb, "mlb_pitcher_games",
                   "game_pk,pitcher_id,game_date,team,opponent,started,"
                   "outs,batters_faced,strikeouts", ["game_date", "game_pk"])
    brows = _paged(sb, "mlb_batter_games",
                   "game_pk,team,game_date,ab,walks,hbp,sac_flies,"
                   "strikeouts", ["game_date", "game_pk"])
    print(f"loaded pitcher rows: {len(prows)}  batter rows: {len(brows)}")

    # per-date structures
    p_by_day: dict[date, list[dict]] = defaultdict(list)
    for r in prows:
        p_by_day[date.fromisoformat(r["game_date"])].append(r)
    # team-game batting aggregates per date
    t_by_day: dict[date, dict[tuple, dict]] = defaultdict(dict)
    for r in brows:
        d = date.fromisoformat(r["game_date"])
        key = (r["game_pk"], r["team"])
        agg = t_by_day[d].setdefault(key, {"pa": 0, "k": 0})
        pa = ((r.get("ab") or 0) + (r.get("walks") or 0)
              + (r.get("hbp") or 0) + (r.get("sac_flies") or 0))
        agg["pa"] += pa
        agg["k"] += r.get("strikeouts") or 0

    st = wq.WhiffState()
    preds: list[dict] = []
    for d in sorted(set(p_by_day) | set(t_by_day)):
        # 1) project every start of the day from PRIOR state
        for r in p_by_day.get(d, []):
            if not r.get("started"):
                continue
            bf = r.get("batters_faced") or 0
            if bf <= 0:
                continue
            full = st.project(r["pitcher_id"], r["opponent"], d)
            noopp = st.project(r["pitcher_id"], r["opponent"], d,
                               use_opponent=False)
            preds.append({
                "date": d, "eval": d >= EVAL_START,
                "e_bf": full["e_bf"], "rate": full["k_rate"],
                "rate_noopp": noopp["k_rate"], "n_starts": full["n_starts"],
                "k": r.get("strikeouts") or 0, "bf": bf,
            })
        # 2) THEN feed the day's games into state
        for r in p_by_day.get(d, []):
            st.feed_pitcher_game(r["pitcher_id"], d, bool(r.get("started")),
                                 r.get("batters_faced") or 0,
                                 r.get("strikeouts") or 0)
        for (_pk, team), agg in t_by_day.get(d, {}).items():
            st.feed_team_batting(team, d, agg["pa"], agg["k"])

    train = [p for p in preds if not p["eval"]]
    ev = [p for p in preds if p["eval"]]
    print(f"starts: train {len(train)}  eval {len(ev)}")
    if not train or not ev:
        print("insufficient data")
        return 1

    # fit BF_SD from train leash residuals (e_bf is sd-independent)
    var = sum((p["bf"] - p["e_bf"]) ** 2 for p in train) / len(train)
    bf_sd = max(var ** 0.5, 1.0)
    print(f"fitted BF_SD = {bf_sd:.2f} (default {wq.BF_SD})")

    # train base rates + mean K (the naive baselines)
    base = {L: sum(1 for p in train if p["k"] >= L) / len(train)
            for L in LINES}
    mean_k = sum(p["k"] for p in train) / len(train)

    # score eval
    def tails(p, rate_key):
        pmf = wq.k_pmf(p["e_bf"], p[rate_key], bf_sd)
        out, acc = {}, 0.0
        for L in range(len(pmf) - 1, 0, -1):
            acc += pmf[L]
            if L in LINES:
                out[L] = acc
        return out

    briers = {L: {"full": 0.0, "noopp": 0.0, "base": 0.0, "n": 0}
              for L in LINES}
    mae_full = mae_naive = 0.0
    cal6 = defaultdict(lambda: [0.0, 0, 0])   # bucket -> [Σp, Σy, n]
    for p in ev:
        tf = tails(p, "rate")
        tn = tails(p, "rate_noopp")
        for L in LINES:
            y = 1.0 if p["k"] >= L else 0.0
            briers[L]["full"] += (tf[L] - y) ** 2
            briers[L]["noopp"] += (tn[L] - y) ** 2
            briers[L]["base"] += (base[L] - y) ** 2
            briers[L]["n"] += 1
        e_k = p["e_bf"] * p["rate"]
        mae_full += abs(e_k - p["k"])
        mae_naive += abs(mean_k - p["k"])
        b = min(int(tf[6] * 10), 9)
        cal6[b][0] += tf[6]
        cal6[b][1] += 1 if p["k"] >= 6 else 0
        cal6[b][2] += 1

    print(f"\nMAE E[K]: model {mae_full / len(ev):.3f}  "
          f"naive(train mean {mean_k:.2f}) {mae_naive / len(ev):.3f}")
    print("\nline  n     base-rate  Brier full  Brier no-opp  Brier base")
    for L in LINES:
        b = briers[L]
        n = b["n"]
        er = sum(1 for p in ev if p["k"] >= L) / len(ev)
        print(f"K>={L}  {n}  {er:.3f}      "
              f"{b['full'] / n:.4f}      {b['noopp'] / n:.4f}        "
              f"{b['base'] / n:.4f}")
    print("\ncalibration @ K>=6 (eval):")
    for b in sorted(cal6):
        sp, sy, n = cal6[b]
        print(f"  p∈[{b / 10:.1f},{(b + 1) / 10:.1f}): "
              f"pred {sp / n:.3f} vs real {sy / n:.3f} (n={n})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
