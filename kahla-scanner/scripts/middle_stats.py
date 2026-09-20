#!/usr/bin/env python3
"""HOW OFTEN DOES A MIDDLE ACTUALLY HIT? (Rob, Sep 19 2026 — setting the pair
caps with data instead of my estimate.)

A middle pair wins BOTH legs when the game lands inside the gap between its two
rungs. The cap question is simply: what is that worth? This measures it against
real finals and real closing lines, so the per-sport ceiling stops being a guess.

NFL closing lines come from nflverse `games.csv` (public, no key, 1999→present:
`spread_line` positive = home favored, `total_line`). College has no free line
history we already ingest, so its numbers are UNCONDITIONAL margin/total
frequencies — read them as "how often each number comes up", not "how often the
middle at the market line hits".

What it prints per sport:
  • the margin and total frequency tables (which numbers actually repeat)
  • for NFL, the PAIR TEST: for every game, the 1-point middle that brackets
    the closing line (e.g. line 6.5 → rungs 6.5/7.5, wins on exactly 7) and the
    2-point version, and how often each hit.

Usage:  .venv/bin/python kahla-scanner/scripts/middle_stats.py [--seasons 2015]
"""
from __future__ import annotations

import argparse
import csv
import io
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
from dotenv import load_dotenv                                  # noqa: E402
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), ".env"))

NFLVERSE_URL = "https://github.com/nflverse/nfldata/raw/master/data/games.csv"
QTY = 15


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def nfl_rows(seasons_from: int):
    import httpx
    r = httpx.get(NFLVERSE_URL, follow_redirects=True, timeout=60.0,
                  headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    out = []
    for row in csv.DictReader(io.StringIO(r.text)):
        season = _f(row.get("season"))
        hs, as_ = _f(row.get("home_score")), _f(row.get("away_score"))
        sl, tl = _f(row.get("spread_line")), _f(row.get("total_line"))
        if None in (season, hs, as_) or season < seasons_from:
            continue
        out.append({"margin": hs - as_, "total": hs + as_,
                    "spread_line": sl, "total_line": tl})
    return out


def freq_table(vals, label, top=8):
    c = Counter(vals)
    n = len(vals)
    print(f"\n  {label} — {n:,} games")
    for k, cnt in c.most_common(top):
        print(f"    {k:>6}: {cnt:5,}  {100.0 * cnt / n:5.2f}%")


def pair_test(rows, mt):
    """The middle our seeder would build, graded on real finals.

    A 1-point middle brackets the closing line: line 6.5 → rungs 6.5 and 7.5,
    which wins both legs when the game lands on exactly 7. An integer line (7)
    brackets the same way (6.5/7.5). The 2-point version widens by one rung.
    Returns (n, hit1, hit2)."""
    n = hit1 = hit2 = 0
    for r in rows:
        line = r["spread_line"] if mt == "spread" else r["total_line"]
        val = r["margin"] if mt == "spread" else r["total"]
        if line is None:
            continue
        n += 1
        target = round(line)                    # the number the market centers on
        if abs(val - target) < 0.5:             # landed exactly on it
            hit1 += 1
        if abs(val - target) < 1.5:             # 2-point window around it
            hit2 += 1
    return n, hit1, hit2


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seasons", type=int, default=2015)
    args = ap.parse_args()

    rows = nfl_rows(args.seasons)
    print(f"\n  NFL — {len(rows):,} finals since {args.seasons}")
    freq_table([int(abs(r["margin"])) for r in rows], "margin (either team)")
    freq_table([int(r["total"]) for r in rows], "total points")

    for mt in ("spread", "total"):
        n, h1, h2 = pair_test(rows, mt)
        if not n:
            continue
        p1, p2 = 100.0 * h1 / n, 100.0 * h2 / n
        print(f"\n  PAIR TEST — NFL {mt}, {n:,} games with a closing line")
        print(f"    1-point middle (rungs either side of the line): "
              f"{h1:,} hits = {p1:.1f}%  → worth {p1:.1f}¢, "
              f"${p1 * QTY / 100:.2f} per 15-lot")
        print(f"    2-point middle (one rung wider):               "
              f"{h2:,} hits = {p2:.1f}%  → worth {p2:.1f}¢, "
              f"${p2 * QTY / 100:.2f} per 15-lot")

    # WHERE the middle sits is the whole game (Sep 19 2026): the pair test
    # above brackets the CLOSING LINE, and football margins pile on 3 and 7 —
    # so a middle on a 3-point line hits far more often than one on a 5.5. The
    # seeder must prefer the middle at the line, not the cheapest rung on the
    # ladder.
    print("\n  HIT RATE BY LINE — NFL 1-point middle bracketing the line")
    print(f"    {'line':>5} {'games':>6} {'hit':>6} {'worth':>7}")
    from collections import defaultdict
    by = defaultdict(lambda: [0, 0])
    for r in rows:
        if r["spread_line"] is None:
            continue
        k = int(abs(round(r["spread_line"])))
        by[k][0] += 1
        if abs(abs(r["margin"]) - k) < 0.5:
            by[k][1] += 1
    for k in sorted(by):
        n0, h0 = by[k]
        if n0 < 40:
            continue
        print(f"    {k:>5} {n0:>6,} {100.0*h0/n0:>5.1f}% {100.0*h0/n0:>6.1f}\u00a2")

    try:
        import app
        sb = app.get_supabase()
        rows_c = []
        pg = 0
        while True:
            page = (sb.table("game_results").select("home_score,away_score")
                    .eq("sport", "NCAAF")
                    .range(pg * 1000, pg * 1000 + 999).execute().data) or []
            rows_c += page
            if len(page) < 1000:
                break
            pg += 1
        if rows_c:
            print(f"\n  NCAAF — {len(rows_c):,} finals (unconditional: no free "
                  f"line history ingested, so read these as 'how often each "
                  f"number comes up')")
            freq_table([int(abs(float(r["home_score"]) - float(r["away_score"])))
                        for r in rows_c], "margin (either team)")
            freq_table([int(float(r["home_score"]) + float(r["away_score"]))
                        for r in rows_c], "total points")
    except Exception as e:
        print(f"\n  NCAAF skipped: {e}")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
