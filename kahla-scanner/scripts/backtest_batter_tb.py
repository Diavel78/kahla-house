"""Batter total-bases walk-forward backtest (the first BATTER prop).

TB per game is zero-moded and skewed, so no normal tail here: the model
is the decayed EMPIRICAL rate — per batter, the decay-weighted (HL
365d) frequency of clearing each line across prior STARTED games,
shrunk toward the train-period league rate by effective games.
Strictly walk-forward at date grain. Train < 2026-03-01 (league rates),
eval = 2026. TB = hits + doubles + 2*triples + 3*home_runs.

  python -m scripts.backtest_batter_tb
"""
from __future__ import annotations

import sys
from collections import defaultdict
from datetime import date

from storage import supabase_client as db

EVAL_START = date(2026, 3, 1)
LINES = (2, 3, 4)
HL_DAYS = 365.0
PRIOR_GAMES = 15.0
MIN_GAMES = 10.0


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


def main() -> int:
    sb = db.client()
    rows = _paged(sb, "mlb_batter_games",
                  "batter_id,game_date,started,hits,doubles,triples,"
                  "home_runs", ["game_date", "game_pk"])
    games = []
    for r in rows:
        if not r.get("started"):
            continue
        tb = ((r.get("hits") or 0) + (r.get("doubles") or 0)
              + 2 * (r.get("triples") or 0) + 3 * (r.get("home_runs") or 0))
        d = date.fromisoformat(str(r["game_date"])[:10])
        games.append((d, r["batter_id"], float(tb)))
    games.sort(key=lambda g: g[0])
    print(f"loaded batter rows: {len(rows)}  started games: {len(games)}")

    tr = [tb for (d, _b, tb) in games if d < EVAL_START]
    base = {L: sum(1 for x in tr if x >= L) / max(1, len(tr)) for L in LINES}
    print(f"train games: {len(tr)}  base rates: "
          + " ".join(f"TB{L}+:{base[L]:.3f}" for L in LINES))

    by_day: dict[date, list[tuple]] = defaultdict(list)
    for g in games:
        by_day[g[0]].append(g)
    hist: dict[int, list[tuple[date, float]]] = defaultdict(list)
    briers = {L: [] for L in LINES}
    briers_base = {L: [] for L in LINES}
    n_scored = 0
    for d in sorted(by_day):
        rows_d = by_day[d]
        if d >= EVAL_START:
            for (_d, bid, tb) in rows_d:
                h = hist.get(bid) or []
                w = 0.0
                hits_l = {L: 0.0 for L in LINES}
                for (d0, tb0) in h:
                    wt = 0.5 ** ((d - d0).days / HL_DAYS)
                    w += wt
                    for L in LINES:
                        if tb0 >= L:
                            hits_l[L] += wt
                if w < MIN_GAMES:
                    continue
                n_scored += 1
                for L in LINES:
                    p = ((hits_l[L] + PRIOR_GAMES * base[L])
                         / (w + PRIOR_GAMES))
                    y = 1.0 if tb >= L else 0.0
                    briers[L].append((p - y) ** 2)
                    briers_base[L].append((base[L] - y) ** 2)
        for (_d, bid, tb) in rows_d:
            hist[bid].append((d, tb))
    print(f"\neval batter-games scored: {n_scored}")
    print(f"{'L':>3} {'BRIER model':>12} {'BRIER base':>11} {'edge':>8}")
    for L in LINES:
        n = len(briers[L])
        if not n:
            continue
        bm = sum(briers[L]) / n
        bb = sum(briers_base[L]) / n
        print(f"{L:>3} {bm:>12.4f} {bb:>11.4f} {bb - bm:>+8.4f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
