"""Handicapper Bot — backtest dossier replay.

Reconstructs what the dossier WOULD have looked like at a given cutoff
time before kickoff, using historical book_snapshots. Lets us evaluate
"if I'd been asked about this game N hours pre-game, what data would
I have had?".

Limitations to be honest about:
  • Action Network splits and ESPN injuries are NOT historical — we'd
    need to have snapshotted them at the time. We skip them entirely
    in backtest mode (so the dossier shows odds + movement only).
  • Probable pitchers / lineups are also not historical via the free
    APIs we use. Skipped.
  • So the backtest signal set is: PIN devigged fair, retail entry +
    edge, sharp side + sharp score, opener vs current PIN movement.
    That's the core of every pick anyway — the live extras are
    confirming-signal layer.

The grading loop:
  1. Pick a sport + lookback window (e.g. last 30d MLB).
  2. For each market with a settled outcome (event_start in the past
     AND ESPN reports a final), reconstruct the dossier as of cutoff
     (default: 30 min pre-kickoff).
  3. Apply rule-based selection: edge_pp ≥ X AND sharp_score ≥ Y on
     the same side. Take the highest-edge of {ML, SPR, TOT}.
  4. Grade against ESPN final via the same _grade() rules as the
     live resolver.
  5. Output per-pick CSV + summary stats.

CLI:
  python -m scripts.handicapper_backtest --sport MLB --days 30 \
    --cutoff-min 30 --edge-min 1.0 --sharp-min 4 \
    --out /tmp/backtest_mlb.csv

The replay is signal-only — it doesn't ask Claude to write picks.
That's the gap the live system fills (Claude weighs the additional
context: injuries, lineups, weather, scratches).

Useful as a smoke test: if the rule-based picker LOSES money, our
weighting of edge / sharp_score may be off, and the live picks
should lean more on the qualitative signals Claude adds.
"""
from __future__ import annotations

import argparse
import csv
import logging
import sys
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from _lib import sharp
from _lib.normalize import american_to_prob, devig_two_way
from storage import supabase_client as db

log = logging.getLogger(__name__)

ALLOWED_BOOKS = {
    "PIN", "DK", "FD", "MGM", "CAE", "HR", "BET365",
    "BR", "BOL", "LV", "BVD", "ESPN", "FAN", "MB",
}
ENTRY_BOOKS = ALLOWED_BOOKS - {"PIN"}

_ESPN_PATH = {
    "MLB":   ("baseball",   "mlb"),
    "NBA":   ("basketball", "nba"),
    "NHL":   ("hockey",     "nhl"),
    "NFL":   ("football",   "nfl"),
    "CBB":   ("basketball", "mens-college-basketball"),
    "NCAAF": ("football",   "college-football"),
}


# ──────────────────────────── Snapshot replay ────────────────────────────

def _snapshots_at(sb, market_id: str, cutoff: datetime) -> dict:
    """Latest snapshot per (book, market_type, side) AS OF cutoff. We
    cap the lookback at 24h so retail re-pricing churn doesn't pull a
    stale row, but PIN-only snaps that are 12h old are valid (PIN sits
    on lines for hours)."""
    earliest = (cutoff - timedelta(hours=24)).isoformat()
    rows = (sb.table("book_snapshots")
            .select("book,market_type,side,price_american,line,captured_at")
            .eq("market_id", market_id)
            .gte("captured_at", earliest)
            .lte("captured_at", cutoff.isoformat())
            .order("captured_at", desc=True)
            .limit(5000)
            .execute().data) or []
    out: dict = {}
    for r in rows:
        if r["book"] not in ALLOWED_BOOKS:
            continue
        key = (r["book"], r["market_type"], r["side"])
        if key not in out:
            out[key] = r
    return out


def _pin_opener(sb, market_id: str) -> dict:
    rows = (sb.table("book_snapshots")
            .select("market_type,side,price_american,line,captured_at")
            .eq("market_id", market_id)
            .eq("book", "PIN")
            .order("captured_at")
            .limit(1000)
            .execute().data) or []
    out: dict = {}
    for r in rows:
        key = (r["market_type"], r["side"])
        if key not in out:
            out[key] = r
    return out


# ──────────────────────────── Pick logic (rule-based) ────────────────────────────

def _eval_market(market_type: str, sides: tuple[str, str],
                 latest: dict, pin_opener: dict) -> dict | None:
    """Returns the best-side candidate for one market, or None if the
    sharp signal / edge gates aren't cleared."""
    a, b = sides

    # Sharp side via _lib.sharp.
    fake_mid = "X"
    op_keyed = {(fake_mid, mt, s): v for (mt, s), v in pin_opener.items() if mt == market_type}
    pin_cur_keyed = {(fake_mid, mt, s): v
                     for (b_, mt, s), v in latest.items()
                     if b_ == "PIN" and mt == market_type}

    if market_type == "moneyline":
        sr = sharp.sharp_for_ml(fake_mid, op_keyed, pin_cur_keyed)
    elif market_type == "spread":
        sr = sharp.sharp_for_spread(fake_mid, op_keyed, pin_cur_keyed)
    else:
        sr = sharp.sharp_for_total(fake_mid, op_keyed, pin_cur_keyed)
    if not sr:
        return None
    side, score, _, _ = sr

    pin_a = latest.get(("PIN", market_type, a))
    pin_b = latest.get(("PIN", market_type, b))
    if not (pin_a and pin_b):
        return None
    # Line match for SPR/TOT.
    if market_type == "spread":
        la, lb = pin_a.get("line"), pin_b.get("line")
        if la is None or lb is None or abs(la + lb) > 0.001:
            return None
    elif market_type == "total":
        if pin_a.get("line") != pin_b.get("line"):
            return None
    try:
        p_a = american_to_prob(int(pin_a["price_american"]))
        p_b = american_to_prob(int(pin_b["price_american"]))
        fair_a = devig_two_way(p_a, p_b)
    except (ValueError, TypeError):
        return None
    fair_prob = fair_a if side == a else (1.0 - fair_a)

    # Best non-PIN entry on the sharp side.
    target_line = None
    pin_snap = pin_a if side == a else pin_b
    if market_type != "moneyline" and pin_snap:
        target_line = pin_snap.get("line")
    best = None
    for book in ENTRY_BOOKS:
        snap = latest.get((book, market_type, side))
        if not snap:
            continue
        if market_type != "moneyline" and target_line is not None:
            if snap.get("line") != target_line:
                continue
        price = snap.get("price_american")
        if price is None:
            continue
        if best is None or price > best["price_american"]:
            best = dict(book=book, price_american=int(price),
                        line=snap.get("line"))
    if not best:
        return None
    implied = american_to_prob(best["price_american"])
    edge_pp = (fair_prob - implied) * 100

    return {
        "market_type": market_type,
        "side":        side,
        "sharp_score": score,
        "fair_prob":   round(fair_prob, 4),
        "entry_book":  best["book"],
        "entry_price": best["price_american"],
        "entry_line":  best["line"],
        "edge_pp":     round(edge_pp, 2),
    }


def _select_pick(latest: dict, pin_opener: dict,
                 edge_min: float, sharp_min: int) -> dict | None:
    """Three-market scan; pick the highest-edge candidate that clears
    BOTH thresholds. Returns None if nothing qualifies."""
    candidates = []
    for mt, sides in [("moneyline", ("away", "home")),
                       ("spread",    ("away", "home")),
                       ("total",     ("over", "under"))]:
        c = _eval_market(mt, sides, latest, pin_opener)
        if c and c["sharp_score"] >= sharp_min and c["edge_pp"] >= edge_min:
            candidates.append(c)
    if not candidates:
        return None
    candidates.sort(key=lambda c: c["edge_pp"], reverse=True)
    return candidates[0]


# ──────────────────────────── ESPN final lookup ────────────────────────────

def _espn_final(sport: str, event_name: str, event_start: datetime
                ) -> tuple[int, int] | None:
    """Final score (away, home) for a settled game, or None if not found.
    Same matching pattern as paper_bets_resolver._match_espn."""
    pair = _ESPN_PATH.get(sport)
    if not pair:
        return None
    grp, lg = pair
    date_key = event_start.astimezone(ZoneInfo("America/New_York")).strftime("%Y%m%d")
    url = f"https://site.api.espn.com/apis/site/v2/sports/{grp}/{lg}/scoreboard"
    try:
        r = httpx.get(url, params={"dates": date_key}, timeout=10)
        if r.status_code != 200:
            return None
        events = (r.json() or {}).get("events", []) or []
    except Exception:
        return None

    if " @ " not in event_name:
        return None
    away, home = event_name.split(" @ ", 1)
    away_n, home_n = away.lower(), home.lower()

    for g in events:
        comp = (g.get("competitions") or [{}])[0]
        cs = comp.get("competitors") or []
        if len(cs) != 2:
            continue
        h = next((c for c in cs if c.get("homeAway") == "home"), cs[0])
        a = next((c for c in cs if c.get("homeAway") == "away"), cs[1])
        h_name = ((h.get("team") or {}).get("displayName") or "").lower()
        a_name = ((a.get("team") or {}).get("displayName") or "").lower()
        if not h_name or not a_name:
            continue
        if not ((home_n in h_name or h_name in home_n) and
                (away_n in a_name or a_name in away_n)):
            continue
        state = ((comp.get("status") or {}).get("type") or {}).get("state")
        if state != "post":
            return None
        try:
            return int(a.get("score") or 0), int(h.get("score") or 0)
        except (ValueError, TypeError):
            return None
    return None


# ──────────────────────────── Grading ────────────────────────────

def _grade(side: str, market_type: str, line: float | None,
           away_score: int, home_score: int) -> str | None:
    if market_type == "moneyline":
        if home_score == away_score:
            return "push"
        winner = "home" if home_score > away_score else "away"
        return "won" if side == winner else "lost"
    if market_type == "spread":
        if line is None:
            return None
        margin = ((home_score - away_score) if side == "home"
                  else (away_score - home_score)) + float(line)
        if margin > 0: return "won"
        if margin < 0: return "lost"
        return "push"
    if market_type == "total":
        total = home_score + away_score
        ln = float(line)
        if side == "over":
            if total > ln: return "won"
            if total < ln: return "lost"
            return "push"
        if total < ln: return "won"
        if total > ln: return "lost"
        return "push"
    return None


def _pnl_units(status: str, price: int, units: int) -> float:
    if status in ("push", "void"): return 0.0
    if status == "lost": return float(-units)
    if price > 0: return units * (price / 100.0)
    return units * (100.0 / abs(price))


# ──────────────────────────── Main ────────────────────────────

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="handicapper_backtest")
    p.add_argument("--sport", required=True,
                   choices=list(_ESPN_PATH.keys()),
                   help="MLB / NBA / NHL / NFL / CBB / NCAAF")
    p.add_argument("--days", type=int, default=30,
                   help="Lookback window (default 30)")
    p.add_argument("--cutoff-min", type=int, default=30,
                   help="Minutes pre-kickoff to evaluate at (default 30)")
    p.add_argument("--edge-min", type=float, default=1.0,
                   help="Min edge_pp to pick (default 1.0)")
    p.add_argument("--sharp-min", type=int, default=4,
                   help="Min sharp_score to pick (default 4)")
    p.add_argument("--units", type=int, default=1,
                   help="Sizing assumption (default 1u flat)")
    p.add_argument("--out", default=None,
                   help="CSV output path (default: stdout)")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(message)s")

    sb = db.client()
    now = datetime.now(timezone.utc)
    after = (now - timedelta(days=args.days)).isoformat()
    before = now.isoformat()

    log.info("loading %s markets last %dd", args.sport, args.days)
    markets = (sb.table("markets")
               .select("id,sport,event_name,event_start")
               .eq("sport", args.sport)
               .gte("event_start", after)
               .lt("event_start", before)
               .order("event_start")
               .limit(2000)
               .execute().data) or []
    log.info("found %d candidate markets", len(markets))

    rows_out = []
    picks = wins = losses = pushes = no_pick = no_final = 0
    total_units = 0.0

    for m in markets:
        try:
            ev_start = datetime.fromisoformat(
                m["event_start"].replace("Z", "+00:00"))
        except Exception:
            continue
        cutoff = ev_start - timedelta(minutes=args.cutoff_min)
        latest = _snapshots_at(sb, m["id"], cutoff)
        if not latest:
            continue
        opener = _pin_opener(sb, m["id"])
        if not opener:
            continue

        pick = _select_pick(latest, opener, args.edge_min, args.sharp_min)
        if not pick:
            no_pick += 1
            continue
        picks += 1

        final = _espn_final(args.sport, m["event_name"], ev_start)
        if not final:
            no_final += 1
            continue
        away_score, home_score = final
        status = _grade(pick["side"], pick["market_type"],
                        pick["entry_line"], away_score, home_score)
        if status is None:
            continue
        pnl = _pnl_units(status, pick["entry_price"], args.units)
        total_units += pnl
        if status == "won":  wins += 1
        elif status == "lost": losses += 1
        else: pushes += 1

        rows_out.append({
            "event_start": m["event_start"],
            "event_name":  m["event_name"],
            "market":      pick["market_type"],
            "side":        pick["side"],
            "book":        pick["entry_book"],
            "price":       pick["entry_price"],
            "line":        pick["entry_line"],
            "edge_pp":     pick["edge_pp"],
            "sharp_score": pick["sharp_score"],
            "fair_prob":   pick["fair_prob"],
            "score":       f"{away_score}-{home_score}",
            "status":      status,
            "pnl_units":   round(pnl, 3),
        })

    decided = wins + losses
    hit_rate = round(wins / decided, 4) if decided else None
    roi = round(total_units / picks, 4) if picks else None
    log.info("=== BACKTEST SUMMARY ===")
    log.info("sport=%s days=%d cutoff=%dmin edge>=%.1fpp sharp>=%d units=%du",
             args.sport, args.days, args.cutoff_min,
             args.edge_min, args.sharp_min, args.units)
    log.info("markets=%d picks=%d no_pick=%d no_final=%d",
             len(markets), picks, no_pick, no_final)
    log.info("won=%d lost=%d push=%d  hit_rate=%s",
             wins, losses, pushes,
             f"{hit_rate*100:.1f}%" if hit_rate is not None else "n/a")
    log.info("total_units=%+.2fu  ROI/pick=%s",
             total_units,
             f"{roi*100:+.2f}%" if roi is not None else "n/a")

    out_path = args.out
    if rows_out:
        if out_path:
            with open(out_path, "w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=list(rows_out[0].keys()))
                w.writeheader()
                w.writerows(rows_out)
            log.info("wrote %d rows to %s", len(rows_out), out_path)
        else:
            w = csv.DictWriter(sys.stdout, fieldnames=list(rows_out[0].keys()))
            w.writeheader()
            w.writerows(rows_out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
