"""Crease IQ v2 — walk-forward backtest of the production engine
(_lib/crease_iq2.py) exactly as the sheet runs it.

For each held-out season: fit on every game BEFORE it, then walk the
season pricing each game before its result updates the state. Reports
moneyline Brier/accuracy/calibration, puck line −1.5 and totals 5.5/6.5
(Brier vs base rate), expected-total MAE.

  python -m scripts.backtest_crease_iq2 [--cache DIR]

--cache DIR reads goalies.json / shots.json from DIR instead of the DB
(the pull takes minutes; research sessions keep a local copy).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections import defaultdict
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _lib import crease_iq2 as ci2  # noqa: E402

log = logging.getLogger("backtest_crease_iq2")

SEASONS = ("2023-09-01", "2024-09-01", "2025-09-01")


def load(cache: str | None):
    if cache:
        return (json.load(open(os.path.join(cache, "goalies.json"))),
                json.load(open(os.path.join(cache, "shots.json"))))
    from scripts.nhl_sheet_data import load_goalie_rows, load_shot_rows
    return load_goalie_rows(), load_shot_rows()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", default=None)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    goalies, shots = load(args.cache)
    games = ci2.build_games(goalies, shots)
    log.info("%d finished games", len(games))
    rows = []
    for i, start in enumerate(SEASONS):
        end = SEASONS[i + 1] if i + 1 < len(SEASONS) else "2099-01-01"
        train = [g for g in games if g["date"] < start]
        st, params = ci2.fit(train)
        log.info("season %s: coef=%s l3=%.2f delta=%.3f k=%.2f ot_home=%.3f",
                 start[:4], [round(c, 3) for c in params["coef"]],
                 params["lam3"], params["delta"], params["k"], params["ot_home"])
        for g in games:
            if not (start <= g["date"] < end):
                continue
            pr = ci2.price(st, params, g["home"], g["away"],
                           date.fromisoformat(g["date"]),
                           g["starter_h"], g["starter_a"])
            if pr:
                rows.append((start, pr, g))
            st.update(g)

    def brier(pairs):
        n = len(pairs)
        b = sum((p - y) ** 2 for p, y in pairs) / n
        base = sum(y for _, y in pairs) / n
        return b, sum((base - y) ** 2 for _, y in pairs) / n, n

    print("\n=== MONEYLINE ===")
    for s in SEASONS + ("ALL",):
        pr = [(r[1]["win_home"], r[2]["home_won"]) for r in rows if s in ("ALL", r[0])]
        b, bb, n = brier(pr)
        acc = sum(1 for p, y in pr if (p >= .5) == (y == 1)) / n
        print(f"  {s[:4]}: n={n} Brier {b:.4f} (base {bb:.4f}) acc {acc:.1%}")
    cal = defaultdict(lambda: [0, 0, 0.0])
    for _, pr, g in rows:
        p = pr["win_home"]
        fp, fw = max(p, 1 - p), (g["home_won"] if p >= .5 else 1 - g["home_won"])
        k = ("50-55" if fp < .55 else "55-60" if fp < .6 else "60-65" if fp < .65
             else "65-70" if fp < .7 else "70+")
        cal[k][0] += fw
        cal[k][1] += 1
        cal[k][2] += fp
    for k in sorted(cal):
        w, n, s = cal[k]
        print(f"    fav {k}: pred {s / n:.3f} act {w / n:.3f} n={n}")

    print("\n=== PUCK LINE / TOTALS (all eval seasons) ===")
    for lab, fn, act in (
            ("home -1.5", lambda p: p["home_cover"]["-1.5"], lambda g: g["final_h"] - g["final_a"] >= 2),
            ("away -1.5", lambda p: p["away_cover"]["-1.5"], lambda g: g["final_a"] - g["final_h"] >= 2),
            ("over 5.5", lambda p: p["over"]["5.5"], lambda g: g["final_h"] + g["final_a"] > 5.5),
            ("over 6.5", lambda p: p["over"]["6.5"], lambda g: g["final_h"] + g["final_a"] > 6.5)):
        pr = [(fn(r[1]), 1 if act(r[2]) else 0) for r in rows]
        b, bb, n = brier(pr)
        print(f"  {lab}: Brier {b:.4f} vs base {bb:.4f} · pred {sum(p for p, _ in pr) / n:.3f}"
              f" act {sum(y for _, y in pr) / n:.3f}")
    et = [(r[1]["exp_total"], r[2]["final_h"] + r[2]["final_a"]) for r in rows]
    mean_a = sum(a for _, a in et) / len(et)
    print(f"  total MAE {sum(abs(p - a) for p, a in et) / len(et):.3f} vs constant "
          f"{sum(abs(mean_a - a) for _, a in et) / len(et):.3f}")
    em = [(r[1]["exp_margin"], r[2]["final_h"] - r[2]["final_a"]) for r in rows]
    print(f"  margin MAE {sum(abs(p - a) for p, a in em) / len(em):.3f} vs zero "
          f"{sum(abs(a) for _, a in em) / len(em):.3f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
