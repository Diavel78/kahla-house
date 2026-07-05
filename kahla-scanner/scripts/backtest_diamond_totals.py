"""Diamond IQ TOTALS — walk-forward backtest for the O/U rebuild.

The old proj_total tier was blacklisted July 4 2026 (75-85, −13.7u,
CLV≈0). The autopsy said WHY: built from the same ingredients as the
line, it systematically out-extremed the market at both tails (overs at
avg line 11.4!) and its only profitable corner was a market bias (unders
below 8), not a projection edge.

This rebuild is evaluated the way the old model never was:

  1. RUNS CALIBRATION (full pitcher spine, fit 2025 / eval 2026):
     predicted total (Diamond IQ xr_home+xr_away, real starter FIP +
     bullpen + offense) vs actual — bias, RMSE vs a naive league-mean
     baseline. A model that can't beat "league average runs" is dead on
     arrival.
  2. MARKET EVAL (games with an exchange total from pm_snapshots,
     ~June 12 onward, accruing daily): does sign(model − line) predict
     sign(actual − line)? Reported BY LINE BUCKET (the autopsy's
     lesson), plus the LINE-ANCHORED form final = line + β·(model−line)
     — the structural fix that forbids out-extreming the market.

  python -m scripts.backtest_diamond_totals
"""
from __future__ import annotations

import logging
import sys
from collections import defaultdict
from datetime import date

from _lib import diamond_iq as di
from scripts.backtest_diamond_iq import group_games, load_rows, _team_update_rows
from storage import supabase_client as db

log = logging.getLogger(__name__)

EVAL_START = date(2026, 3, 1)

# Full name -> boxscore abbreviation (markets.event_name is "Away @ Home"
# full names; mlb_pitcher_games uses abbreviations).
_ABBR = {
    "arizona diamondbacks": "AZ", "atlanta braves": "ATL", "baltimore orioles": "BAL",
    "boston red sox": "BOS", "chicago cubs": "CHC", "chicago white sox": "CWS",
    "cincinnati reds": "CIN", "cleveland guardians": "CLE", "colorado rockies": "COL",
    "detroit tigers": "DET", "houston astros": "HOU", "kansas city royals": "KC",
    "los angeles angels": "LAA", "los angeles dodgers": "LAD", "miami marlins": "MIA",
    "milwaukee brewers": "MIL", "minnesota twins": "MIN", "new york mets": "NYM",
    "new york yankees": "NYY", "athletics": "ATH", "oakland athletics": "ATH",
    "philadelphia phillies": "PHI", "pittsburgh pirates": "PIT", "san diego padres": "SD",
    "san francisco giants": "SF", "seattle mariners": "SEA", "st. louis cardinals": "STL",
    "tampa bay rays": "TB", "texas rangers": "TEX", "toronto blue jays": "TOR",
    "washington nationals": "WSH",
}


def _abbr(name: str) -> str | None:
    return _ABBR.get((name or "").strip().lower())


def market_lines(sb) -> dict:
    """(date_iso, away_abbr, home_abbr) -> at-the-money exchange total.
    Last pre-game PMM cents per (game, line); the ATM line is the one whose
    final cents sit closest to 50."""
    mk, page = {}, 0
    while True:
        rows = (sb.table("markets").select("id,event_name,event_start")
                .eq("sport", "MLB")
                .range(page * 1000, page * 1000 + 999).execute().data) or []
        for m in rows:
            en = m.get("event_name") or ""
            if " @ " not in en:
                continue
            aw, hm = [s.strip() for s in en.split(" @ ", 1)]
            a, h = _abbr(aw), _abbr(hm)
            if a and h and m.get("event_start"):
                mk[m["id"]] = (m["event_start"][:10], a, h, m["event_start"])
        if len(rows) < 1000:
            break
        page += 1

    latest: dict = {}   # (mid, line) -> (captured_at, cents)
    page = 0
    while True:
        rows = (sb.table("pm_snapshots")
                .select("market_id,line,cents,captured_at,side")
                .eq("market_type", "total").eq("source", "pmm")
                .eq("side", "over")
                .range(page * 1000, page * 1000 + 999).execute().data) or []
        for r in rows:
            mid = r.get("market_id")
            info = mk.get(mid)
            if not info or r.get("line") is None or r.get("cents") is None:
                continue
            if (r.get("captured_at") or "") > info[3]:
                continue           # in-play rows don't count as the pre-game line
            key = (mid, float(r["line"]))
            cur = latest.get(key)
            if cur is None or r["captured_at"] > cur[0]:
                latest[key] = (r["captured_at"], float(r["cents"]))
        if len(rows) < 1000:
            break
        page += 1

    atm: dict = {}      # mid -> (|cents-50|, line)
    for (mid, line), (_, cents) in latest.items():
        d = abs(cents - 50.0)
        if mid not in atm or d < atm[mid][0]:
            atm[mid] = (d, line)
    out = {}
    for mid, (_, line) in atm.items():
        dte, a, h, _ = mk[mid]
        out[(dte, a, h)] = line
    return out


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    sb = db.client()
    games = group_games(load_rows(sb))
    log.info("pitcher spine: %d games", len(games))
    lines = market_lines(sb)
    log.info("market totals (ATM exchange lines): %d games", len(lines))

    state = di.DiamondState()
    lg_hist: list = []            # trailing actual totals (naive baseline)
    cal, mkt = [], []
    for g in games:
        hs = g["teams"][g["home"]]["starter"]
        as_ = g["teams"][g["away"]]["starter"]
        proj = state.project(g["home"], g["away"], g["date"], hs, as_, hfa=0.0)
        actual = (g["teams"][g["away"]]["runs_allowed"]
                  + g["teams"][g["home"]]["runs_allowed"])
        if proj is not None:
            pred = proj["xr_home"] + proj["xr_away"]
            naive = (sum(lg_hist[-450:]) / len(lg_hist[-450:])) if len(lg_hist) >= 100 else None
            if g["date"] >= EVAL_START and naive is not None:
                cal.append((pred, naive, actual))
                line = lines.get((g["date"].isoformat(), g["away"], g["home"]))
                if line is not None:
                    mkt.append((pred, float(line), actual))
        lg_hist.append(actual)
        state.update(g["date"], _team_update_rows(g))

    # ---- 1. runs calibration ------------------------------------------------
    n = len(cal)
    print(f"\n=== RUNS CALIBRATION (2026 eval, n={n}) ===")
    if n:
        bias = sum(p - a for p, _, a in cal) / n
        rmse = (sum((p - a) ** 2 for p, _, a in cal) / n) ** 0.5
        nrmse = (sum((v - a) ** 2 for _, v, a in cal) / n) ** 0.5
        print(f"  model bias {bias:+.2f} runs · RMSE {rmse:.2f} vs naive-league {nrmse:.2f}"
              f"  ({'BEATS' if rmse < nrmse else 'LOSES TO'} naive)")

    # ---- 2. market eval -----------------------------------------------------
    n = len(mkt)
    print(f"\n=== MARKET EVAL (exchange ATM total, n={n}) ===")
    if not n:
        print("  no paired games")
        return 0
    buckets = defaultdict(lambda: [0, 0, 0.0])
    for pred, line, actual in mkt:
        if pred == line or actual == line:
            continue
        bk = ("<8" if line < 8 else "8-9" if line < 9 else "9-10.5" if line < 10.5 else ">=10.5")
        hit = (pred > line) == (actual > line)
        b = buckets[bk]
        b[0] += hit
        b[1] += 1
        b[2] += (actual - line) if pred > line else (line - actual)
    tot_hit = sum(b[0] for b in buckets.values())
    tot_n = sum(b[1] for b in buckets.values())
    print(f"  RAW directional (does sign(model−line) predict sign(actual−line)):"
          f" {tot_hit}/{tot_n} = {tot_hit/tot_n:.1%}" if tot_n else "  (none)")
    for bk in ("<8", "8-9", "9-10.5", ">=10.5"):
        if bk in buckets:
            h, m, edge = buckets[bk]
            print(f"    line {bk:>7}: {h}/{m} = {h/m:.1%} · mean signed (actual−line) toward pick {edge/m:+.2f}")
    # line-anchored shrinkage: fit beta on the first half, eval second half
    half = n // 2
    fit, ev = mkt[:half], mkt[half:]
    num = sum((p - l) * (a - l) for p, l, a in fit)
    den = sum((p - l) ** 2 for p, l, a in fit)
    beta = (num / den) if den else 0.0
    if ev:
        r_shr = (sum((l + beta * (p - l) - a) ** 2 for p, l, a in ev) / len(ev)) ** 0.5
        r_line = (sum((l - a) ** 2 for p, l, a in ev) / len(ev)) ** 0.5
        print(f"  LINE-ANCHORED: fitted beta={beta:+.2f} (β≤0 → model residual is pure noise)")
        print(f"    RMSE line-only {r_line:.2f} vs line+β·(model−line) {r_shr:.2f} on held-out half"
              f" ({'model residual ADDS info' if r_shr < r_line else 'market line alone is better'})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
