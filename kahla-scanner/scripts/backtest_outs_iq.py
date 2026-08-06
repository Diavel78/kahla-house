"""Outs IQ — walk-forward backtest of a pitcher OUTS-prop model (v1).

The expansion candidate the user called (Aug 5 2026: "we are only
betting ONE prop... there's total bases, outs... we leaving money on
the table"): Polymarket lists ~561 pitcher-outs markets/week at an
average 56c — dead center of the fill band — and mlb_pitcher_games
already records outs for every start, so this backtests AND grades off
the existing spine with zero new ingest.

Model v1 (deliberately simple — the leash IS the product here):
per-pitcher decay-weighted mean/SD of OUTS RECORDED across prior
starts (half-life 365d), shrunk toward the train-period league
distribution by effective-starts weight; P(outs >= L) via normal tail
with continuity correction. Strictly walk-forward at date grain (state
updates only from earlier dates; actual starters — leakage-free).

Split: train < 2026-03-01 (fits league mean/SD + residual SD), eval =
2026 season. Reports Brier of P(outs >= L) vs the train base rate per
line L in 12..21 (4-7 innings, the PMM ladder range) + a calibration
table at L=18 (six innings, the most-quoted line).

  python -m scripts.backtest_outs_iq
"""
from __future__ import annotations

import math
import sys
from collections import defaultdict
from datetime import date

from storage import supabase_client as db

EVAL_START = date(2026, 3, 1)
LINES = (12, 15, 16, 17, 18, 19, 20, 21)
HL_DAYS = 365.0          # decay half-life on prior starts
PRIOR_STARTS = 4.0       # shrinkage weight toward league (in starts)
MIN_STARTS = 3.0         # effective starts required to score a projection


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
                   "pitcher_id,game_date,started,outs",
                   ["game_date", "game_pk"])
    starts = [r for r in prows if r.get("started") and r.get("outs") is not None]
    print(f"loaded pitcher rows: {len(prows)}  starts: {len(starts)}")

    by_day: dict[date, list[dict]] = defaultdict(list)
    for r in starts:
        d = date.fromisoformat(str(r["game_date"])[:10])
        r["_d"] = d
        by_day[d].append(r)
    days = sorted(by_day)

    # ---- train league distribution (starts before EVAL_START) ----
    tr = [float(r["outs"]) for r in starts if r["_d"] < EVAL_START]
    lg_m = sum(tr) / max(1, len(tr))
    lg_s = math.sqrt(sum((x - lg_m) ** 2 for x in tr) / max(1, len(tr) - 1))
    base = {L: sum(1 for x in tr if x >= L) / max(1, len(tr)) for L in LINES}
    print(f"train starts: {len(tr)}  league outs mean {lg_m:.2f} sd {lg_s:.2f}")
    print("train base rates: " +
          " ".join(f"L{L}:{base[L]:.3f}" for L in LINES))

    # ---- walk forward ----
    hist: dict[int, list[tuple[date, float]]] = defaultdict(list)
    briers: dict[int, list[float]] = {L: [] for L in LINES}
    briers_base: dict[int, list[float]] = {L: [] for L in LINES}
    cal: dict[int, list[int]] = defaultdict(lambda: [0, 0])   # L18 calibration
    n_scored = 0
    for d in days:
        rows = by_day[d]
        if d >= EVAL_START:
            for r in rows:
                pid = r["pitcher_id"]
                h = hist.get(pid) or []
                w = m_acc = 0.0
                for (d0, outs0) in h:
                    wt = 0.5 ** ((d - d0).days / HL_DAYS)
                    w += wt
                    m_acc += wt * outs0
                if w < MIN_STARTS:
                    continue
                m_p = m_acc / w
                # decay-weighted variance around the pitcher mean
                v_acc = wv = 0.0
                for (d0, outs0) in h:
                    wt = 0.5 ** ((d - d0).days / HL_DAYS)
                    v_acc += wt * (outs0 - m_p) ** 2
                    wv += wt
                s_p = math.sqrt(v_acc / wv) if wv > 0 else lg_s
                # shrink both toward league by effective starts
                m_hat = (m_p * w + lg_m * PRIOR_STARTS) / (w + PRIOR_STARTS)
                s_hat = math.sqrt(
                    (s_p ** 2 * w + lg_s ** 2 * PRIOR_STARTS)
                    / (w + PRIOR_STARTS))
                actual = float(r["outs"])
                n_scored += 1
                for L in LINES:
                    p = 1.0 - _phi((L - 0.5 - m_hat) / max(0.75, s_hat))
                    y = 1.0 if actual >= L else 0.0
                    briers[L].append((p - y) ** 2)
                    briers_base[L].append((base[L] - y) ** 2)
                    if L == 18:
                        b = min(9, int(p * 10))
                        cal[b][0] += int(y)
                        cal[b][1] += 1
        for r in rows:                    # update AFTER projecting the date
            hist[r["pitcher_id"]].append((d, float(r["outs"])))

    print(f"\neval starts scored: {n_scored}")
    print(f"{'L':>3} {'n':>6} {'BRIER model':>12} {'BRIER base':>11} {'edge':>7}")
    for L in LINES:
        n = len(briers[L])
        if not n:
            continue
        bm = sum(briers[L]) / n
        bb = sum(briers_base[L]) / n
        print(f"{L:>3} {n:>6} {bm:>12.4f} {bb:>11.4f} {bb - bm:>+7.4f}")
    print("\ncalibration at L=18 (P(outs>=18) decile -> observed):")
    for b in sorted(cal):
        won, n = cal[b]
        print(f"  {b/10:.1f}-{(b+1)/10:.1f}: {won}/{n} = {won/max(1,n):.3f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
