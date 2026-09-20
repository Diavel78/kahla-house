#!/usr/bin/env python3
"""THE PAIR FINDER — a shopping list, never an order (Rob, Sep 19 2026).

Walks the venue's rent list, prices every middle it can build out of a game's
ladder, and prints what a pair WOULD cost and what its cap WOULD be. It places
nothing, writes nothing, and is deliberately NOT wired into a lane: the money
daemon runs from the repo working tree, so unused code here can never act.

PRICES COME FROM THE TAPE, NOT THE VENUE. `pm_snapshots` already carries both
sides of every football rung with bid/ask cents, logged every ~2 minutes by the
tape daemon. A first cut read books over REST instead and tripped the venue's
rate limiter on its first run — the quote table lives inside the daemon's
process, so a standalone script has none. Reading the tape costs nothing and is
fresh enough for a shopping list.

THE CAP RULE (derived, not hand-set): a middle is worth what the ladder says it
is worth — two legs that cover every outcome sum to 100 + P(middle), so
    cap = min(sport ceiling, mid1 + mid2 + slack)
"never pay more than the middle is worth, plus a cent or two". The per-sport
ceiling is the worst-case fence: cost − 100 is the most a pair can lose.

SHAPES (all sign work in one place). pm_snapshots stores each SIDE with its own
line, so no inversion is needed to price a leg:
    spread  leg A = away at line La   (slug neg-|La| if La<0 else pos-La, BUY_LONG)
            leg B = home at line Lh   (slug for away line −Lh, BUY_SHORT = the NO)
            middle exists iff La + Lh > 0; width = La + Lh
            e.g. GB −4.5 + NYJ +5.5 → width 1.0 → wins both on GB by exactly 5
    total   leg A = over L1 (total-L1, BUY_LONG), leg B = under L2 (total-L2, BUY_SHORT)
            middle exists iff L2 > L1; width = L2 − L1

Usage (repo root, daemon's interpreter):
    .venv/bin/python kahla-scanner/scripts/pair_finder.py [--sport NFL,NCAAF]
        [--hours 200] [--games 40] [--fresh-min 20] [--json]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
from dotenv import load_dotenv                                  # noqa: E402
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), ".env"))
import app                                                      # noqa: E402

# Ceilings by sport (Rob, Sep 19 2026 — football is king now; hockey is
# preseason with moneylines only, so no pair can exist there yet; basketball
# tips in ~a month; baseball is near the playoffs). Football/hoops ladders run
# every half point, so their real pairs land at 101-104. MLB/NHL gaps are a
# whole run or goal, which the market fairly prices at 113-118 — hence a higher
# fence, not a looser rule.
CEILING = {"NFL": 110.0, "NCAAF": 110.0, "NBA": 110.0, "NCAAB": 110.0,
           "MLB": 116.0, "NHL": 119.0}
PRIORITY = ["NFL", "NCAAF", "NHL", "NBA", "NCAAB", "MLB"]
SLACK_C = 1.5             # we may pay the middle's worth plus this
MAX_WIDTH = 3.0           # points of middle worth considering
MIN_WIDTH = 1.0           # a 0.5-point "middle" is a tie/push — not a middle
LEG_BAND = (15.0, 85.0)   # no 95¢ legs, no lottery tickets
QTY = 15


def slug_for(prefix: str, mt: str, side: str, line: float):
    """(slug, intent) for one leg. A home spread leg is the NO side of the
    market whose AWAY line is its mirror — same market, opposite side."""
    if mt == "total":
        return (f"{prefix}-total-{int(line)}pt5",
                "BUY_LONG" if side == "over" else "BUY_SHORT")
    away_line = line if side == "away" else -line
    rung = (f"neg-{int(abs(away_line))}pt5" if away_line < 0
            else f"pos-{int(away_line)}pt5")
    return f"{prefix}-{rung}", ("BUY_LONG" if side == "away" else "BUY_SHORT")


def best_pair(rungs, mt, ceiling):
    """rungs: [(side, line, bid, ask)] → the best candidate or None. Ranked by
    worst case (cost − 100), then by how close the legs sit to 50 — the deepest
    part of any ladder, where a resting order actually gets company."""
    a_side, b_side = ("away", "home") if mt == "spread" else ("over", "under")
    A = [r for r in rungs if r[0] == a_side]
    B = [r for r in rungs if r[0] == b_side]
    out = []
    for _, la, ba, aa in A:
        for _, lb, bb, ab in B:
            width = (la + lb) if mt == "spread" else (lb - la)
            if not (MIN_WIDTH <= width <= MAX_WIDTH):
                continue
            # WHAT THE MIDDLE ACTUALLY COVERS. A spread pair wins both legs
            # when the away margin M satisfies −la < M < lb; a total pair when
            # la < T < lb. THE TIE TRAP (caught on the first full run, Sep 19
            # 2026): away +0.5 with home +0.5 is width 1.0 and reads as a
            # middle, but the only number it covers is 0 — an NFL tie (~0.2%,
            # and college has none). Those pairs cost 98-100 not because they
            # are gifts but because they are simply both sides of one line:
            # a spread capture, not a middle. Keep them out of this list.
            hits = [k for k in range(int(-la) - 2, int(lb) + 3)
                    if -la < k < lb] if mt == "spread" else \
                   [k for k in range(int(la) - 1, int(lb) + 2) if la < k < lb]
            if not [k for k in hits if k != 0]:
                continue
            if None in (ba, aa, bb, ab):
                continue
            if not (LEG_BAND[0] <= ba <= LEG_BAND[1]
                    and LEG_BAND[0] <= bb <= LEG_BAND[1]):
                continue
            cost = ba + bb                       # both legs joined at the touch
            mid_sum = (ba + aa) / 2.0 + (bb + ab) / 2.0
            cap = min(ceiling, mid_sum + SLACK_C)
            out.append({"a_line": la, "b_line": lb, "width": round(width, 1),
                        "hits": hits,
                        "a_c": ba, "b_c": bb, "cost_c": round(cost, 1),
                        "cap_c": round(cap, 1),
                        "middle_c": round(mid_sum - 100.0, 1),
                        "worst_usd": round((cost - 100.0) * QTY / 100.0, 2),
                        "fits": cost <= cap + 1e-9})
    fits = [o for o in out if o["fits"]]
    if not fits:
        return None
    fits.sort(key=lambda o: (o["cost_c"], abs(o["a_c"] - 50) + abs(o["b_c"] - 50)))
    return fits[0]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sport", default="NFL,NCAAF")
    ap.add_argument("--hours", type=float, default=200.0)
    ap.add_argument("--games", type=int, default=40)
    ap.add_argument("--fresh-min", type=float, default=20.0)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    sports = [s.strip().upper() for s in args.sport.split(",") if s.strip()]

    sb = app.get_supabase()
    now = datetime.now(timezone.utc)
    app._rent_enrolled_football(sb)                 # warms the key → market map
    keys = dict(app._RENTLIST_CACHE.get("keys") or {})

    # the venue's enrolled rungs, grouped (market_id, market_type) → prefix
    cut = (now - timedelta(hours=24)).isoformat()
    enrolled: dict = {}
    prefixes: dict = {}
    for fam in ("asc-", "tsc-"):
        pg = 0
        while True:                                  # gotcha #40: page, always
            page = (sb.table("rent_list_slugs").select("slug")
                    .gte("last_seen", cut).like("slug", fam + "%")
                    .range(pg * 1000, pg * 1000 + 999).execute().data) or []
            for r in page:
                s = r["slug"]
                m = app._RENT_SLUG_RE.match(s)
                if not m:
                    continue
                mt = "spread" if m.group(1) == "asc" else "total"
                tail = s[len(f"{m.group(1)}-{m.group(2)}-{m.group(3)}-"
                            f"{m.group(4)}-{m.group(5)}"):]
                if not re.fullmatch(r"-(neg|pos)-\d+pt5|-total-\d+pt5", tail):
                    continue                        # 1h / 2q / tt variants out
                mid = keys.get(f"{m.group(2)}-{m.group(3)}-{m.group(4)}-{m.group(5)}")
                if mid:
                    enrolled.setdefault((mid, mt), set()).add(s)
                    prefixes[(mid, mt)] = s[:len(s) - len(tail)]
            if len(page) < 1000:
                break
            pg += 1

    mids = sorted({k[0] for k in enrolled})
    rows = []
    for i in range(0, len(mids), 300):
        rows += (sb.table("markets").select("id,event_name,event_start,sport")
                 .in_("id", mids[i:i + 300]).execute().data) or []
    games = {r["id"]: r for r in rows
             if r.get("sport") in sports and r.get("event_start")
             and now < app._parse_iso(r["event_start"]) < now + timedelta(hours=args.hours)}
    order = {s: i for i, s in enumerate(PRIORITY)}
    todo = sorted(games.values(),
                  key=lambda r: (order.get(r["sport"], 9), r["event_start"]))[:args.games]

    have = {(p["game_prefix"], p["market_type"])
            for p in ((sb.table("pair_hedges").select("game_prefix,market_type")
                       .eq("enabled", True).execute().data) or [])}

    # the tape: latest bid/ask per (market, type, side, line)
    fresh = (now - timedelta(minutes=args.fresh_min)).isoformat()
    tape: dict = {}
    for i in range(0, len(todo), 40):
        chunk = [g["id"] for g in todo[i:i + 40]]
        pg = 0
        while True:
            page = (sb.table("pm_snapshots")
                    .select("market_id,market_type,side,line,bid_c,ask_c,captured_at")
                    .in_("market_id", chunk).eq("source", "pmm")
                    .gte("captured_at", fresh).order("captured_at")
                    .range(pg * 1000, pg * 1000 + 999).execute().data) or []
            for r in page:
                if r["market_type"] not in ("spread", "total") or r["line"] is None:
                    continue
                tape[(r["market_id"], r["market_type"], r["side"],
                      float(r["line"]))] = (r["bid_c"], r["ask_c"])
            if len(page) < 1000:
                break
            pg += 1

    found = []
    for g in todo:
        for mt in ("spread", "total"):
            if (g["id"], mt) not in enrolled:
                continue
            prefix = prefixes[(g["id"], mt)]
            if (prefix, mt) in have:
                found.append({"game": g["event_name"], "mt": mt, "skip": "pair exists"})
                continue
            rungs = [(side, line, b, a)
                     for (mid, m2, side, line), (b, a) in tape.items()
                     if mid == g["id"] and m2 == mt and b is not None and a is not None
                     and slug_for(prefix, mt, side, line)[0] in enrolled[(g["id"], mt)]]
            if len(rungs) < 2:
                found.append({"game": g["event_name"], "mt": mt,
                              "skip": f"only {len(rungs)} priced rungs on the tape"})
                continue
            cand = best_pair(rungs, mt, CEILING.get(g["sport"], 110.0))
            if not cand:
                found.append({"game": g["event_name"], "mt": mt,
                              "skip": f"no middle under the cap ({len(rungs)} rungs)"})
                continue
            es = app._parse_iso(g["event_start"])
            a_slug, a_int = slug_for(prefix, mt, "away" if mt == "spread" else "over",
                                     cand["a_line"])
            b_slug, b_int = slug_for(prefix, mt, "home" if mt == "spread" else "under",
                                     cand["b_line"])
            rent_a = app._rent_ok(a_slug, es, now, sb)[0]
            rent_b = app._rent_ok(b_slug, es, now, sb)[0]
            cand.update(game=g["event_name"], sport=g["sport"], mt=mt, prefix=prefix,
                        kickoff=g["event_start"], a_slug=a_slug, b_slug=b_slug,
                        a_intent=a_int, b_intent=b_int, rent_a=rent_a, rent_b=rent_b,
                        qualifies=bool(cand["fits"] and rent_a and rent_b))
            found.append(cand)

    if args.json:
        print(json.dumps(found, indent=1, default=str))
        return 0
    ok = [f for f in found if f.get("qualifies")]
    print(f"\n  PAIR FINDER — {len(ok)} qualifying of {len(found)} looks "
          f"({', '.join(sports)}, next {args.hours:.0f}h, tape ≤{args.fresh_min:.0f}m old)\n")
    print(f"  {'game':32} {'mkt':6} {'legs':30} {'cost':>5} {'cap':>5} "
          f"{'mid':>5} {'worst':>6}  {'wins on':7} rent")
    for f in found:
        if f.get("skip"):
            print(f"  {f['game'][:32]:32} {f['mt']:6} — {f['skip']}")
            continue
        legs = (f"{f['a_line']:+.1f}/{f['b_line']:+.1f} @ {f['a_c']:.1f}+{f['b_c']:.1f}"
                if f["mt"] == "spread" else
                f"O{f['a_line']:.1f}/U{f['b_line']:.1f} @ {f['a_c']:.1f}+{f['b_c']:.1f}")
        print(f"  {f['game'][:32]:32} {f['mt']:6} {legs:30} {f['cost_c']:5.1f} "
              f"{f['cap_c']:5.1f} {f['middle_c']:5.1f} ${f['worst_usd']:5.2f}  "
              f"{','.join(str(h) for h in f['hits']):7} "
              f"{'y' if f['rent_a'] else 'n'}{'y' if f['rent_b'] else 'n'}"
              f"{'' if f['qualifies'] else '   (no)'}")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
