"""Pitcher-props walk-forward backtest — hits allowed / ER allowed /
walks allowed (the aisles behind Outs IQ; user Aug 6: "These are
available on Polymarket? Why aren't we betting them").

Same v1 form as Outs IQ, one stat at a time: per-pitcher decay-weighted
mean/SD of the stat across prior starts (HL 365d), shrunk toward the
train-period league distribution by effective starts; P(stat >= L) via
normal tail with continuity correction (crude for low-count stats like
walks — the Brier table decides whether crude is enough). Strictly
walk-forward at date grain, actual starters, train < 2026-03-01.

  python -m scripts.backtest_pitcher_props
"""
from __future__ import annotations

import math
import sys
from collections import defaultdict
from datetime import date

from storage import supabase_client as db

EVAL_START = date(2026, 3, 1)
STATS = (
    ("hits", (2, 3, 4, 5, 6, 7, 8)),
    ("earned_runs", (1, 2, 3, 4, 5)),
    ("walks", (1, 2, 3, 4)),
)
HL_DAYS = 365.0
PRIOR_STARTS = 4.0
MIN_STARTS = 3.0


def _paged(sb, table: str, cols: str, order: list[str]) -> list[dict]:
    out, page = [], 0
    while True:          # explicit paging — the 1,000-row lesson
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


def _phi(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def main() -> int:
    sb = db.client()
    prows = _paged(sb, "mlb_pitcher_games",
                   "pitcher_id,game_date,started,hits,earned_runs,walks",
                   ["game_date", "game_pk"])
    starts = [r for r in prows if r.get("started")]
    print(f"loaded pitcher rows: {len(prows)}  starts: {len(starts)}")
    by_day: dict[date, list[dict]] = defaultdict(list)
    for r in starts:
        d = date.fromisoformat(str(r["game_date"])[:10])
        r["_d"] = d
        by_day[d].append(r)
    days = sorted(by_day)

    for stat, lines in STATS:
        vals = [float(r[stat]) for r in starts
                if r["_d"] < EVAL_START and r.get(stat) is not None]
        lg_m = sum(vals) / max(1, len(vals))
        lg_s = math.sqrt(sum((x - lg_m) ** 2 for x in vals)
                         / max(1, len(vals) - 1))
        base = {L: sum(1 for x in vals if x >= L) / max(1, len(vals))
                for L in lines}
        print(f"\n=== {stat}: train n={len(vals)} mean {lg_m:.2f} "
              f"sd {lg_s:.2f} ===")
        hist: dict[int, list[tuple[date, float]]] = defaultdict(list)
        briers = {L: [] for L in lines}
        briers_base = {L: [] for L in lines}
        n_scored = 0
        for d in days:
            rows = by_day[d]
            if d >= EVAL_START:
                for r in rows:
                    if r.get(stat) is None:
                        continue
                    h = hist.get(r["pitcher_id"]) or []
                    w = m_acc = 0.0
                    for (d0, v0) in h:
                        wt = 0.5 ** ((d - d0).days / HL_DAYS)
                        w += wt
                        m_acc += wt * v0
                    if w < MIN_STARTS:
                        continue
                    m_p = m_acc / w
                    v_acc = 0.0
                    for (d0, v0) in h:
                        wt = 0.5 ** ((d - d0).days / HL_DAYS)
                        v_acc += wt * (v0 - m_p) ** 2
                    s_p = math.sqrt(v_acc / w) if w > 0 else lg_s
                    m_hat = ((m_p * w + lg_m * PRIOR_STARTS)
                             / (w + PRIOR_STARTS))
                    s_hat = math.sqrt(
                        (s_p ** 2 * w + lg_s ** 2 * PRIOR_STARTS)
                        / (w + PRIOR_STARTS))
                    actual = float(r[stat])
                    n_scored += 1
                    for L in lines:
                        p = 1.0 - _phi((L - 0.5 - m_hat) / max(0.6, s_hat))
                        y = 1.0 if actual >= L else 0.0
                        briers[L].append((p - y) ** 2)
                        briers_base[L].append((base[L] - y) ** 2)
            for r in rows:
                if r.get(stat) is not None:
                    hist[r["pitcher_id"]].append((d, float(r[stat])))
        print(f"eval starts scored: {n_scored}")
        print(f"{'L':>3} {'BRIER model':>12} {'BRIER base':>11} {'edge':>8}")
        for L in lines:
            n = len(briers[L])
            if not n:
                continue
            bm = sum(briers[L]) / n
            bb = sum(briers_base[L]) / n
            print(f"{L:>3} {bm:>12.4f} {bb:>11.4f} {bb - bm:>+8.4f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
