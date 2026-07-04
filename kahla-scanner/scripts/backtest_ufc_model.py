"""Walk-forward backtest of the Fight IQ UFC model — the evidence gate.

Replays every bout chronologically (ratings from PRIOR fights only),
fits the probability scale + rounds beta on the TRAIN window, then
evaluates once on the held-out window. Reports the leakage-free CORE
and the FULL stack (reach/stance/TD layers use current career values —
documented mild lookahead) separately.

No historical closing odds exist in our data, so there is no market
baseline here — the external anchors are published MMA models (60-65%
winner accuracy) and the live paperlog once Phase 3 wires the dossier.

  python -m scripts.backtest_ufc_model                 # 2015+ recs, eval 2022+
  python -m scripts.backtest_ufc_model --eval-since 2024-01-01
"""
from __future__ import annotations

import argparse
import logging
import sys

from storage import supabase_client as db
from _lib import ufc_model as M

log = logging.getLogger(__name__)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--collect-since", default="2015-01-01")
    ap.add_argument("--eval-since", default="2022-01-01")
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING)

    sb = db.client()
    fights, fighters = M.load_from_db(sb)
    print("=" * 64)
    print("FIGHT IQ BACKTEST (walk-forward, neutral orientation)")
    print("=" * 64)
    print(f"{len(fights)} bouts · {len(fighters)} detailed fighters")

    _, recs, _aggs = M.replay(fights, fighters, collect_since=args.collect_since)
    train = [r for r in recs if r["date"] < args.eval_since]
    test = [r for r in recs if r["date"] >= args.eval_since]
    print(f"graded records: {len(recs)} (train {len(train)}, eval {len(test)})")
    if not train or not test:
        print("not enough records — check the ufc_fights backfill")
        return 2

    beta = M.fit_beta(train)
    for key, label in (("d_core", "CORE (leakage-free)"),
                       ("d_full", "FULL (static-stat layers)")):
        scale = M.fit_scale(train, key=key)
        ev = M.evaluate(test, scale, beta, key=key)
        print(f"\n== {label} ==  scale={scale} beta={beta}")
        print(f"  winner : acc {ev['winner_acc']:.3f} · Brier {ev['winner_brier']:.4f}"
              f" · n {ev['n']}  (coinflip Brier 0.2500)")
        for k, v in ev["calibration"].items():
            print(f"    {k}: win {v['win']:.1%} (n={v['n']})")
        if "dist_brier" in ev:
            print(f"  distance: Brier {ev['dist_brier']:.4f} vs base-rate "
                  f"{ev['dist_brier_base']:.4f} · n {ev['dist_n']} · "
                  f"dist-rate {ev['dist_rate']:.3f}")
        for line, m in (ev.get("rounds") or {}).items():
            print(f"  over {line}: Brier {m['brier']:.4f} vs base "
                  f"{m['brier_base']:.4f} · n {m['n']} · over-rate {m['over_rate']}")
    print("\nRead: winner calibration buckets should rise monotonically;")
    print("distance/rounds Brier below the base-rate column = the flagship gate.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
