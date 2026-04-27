"""Phase 4 Stage 2 — paper-bet resolver.

Pulls pending paper_bets whose event_start is more than RESOLVE_LAG_HOURS
ago, matches each to ESPN's final score, and grades the row to won /
lost / push with `pnl_units` (flat 1u sizing). Mirrors Flask's
`_merge_espn_scores` matching logic in app.py:1288 so resolver +
on-board scores agree.

Runs as an appended step in `scanner-poll.yml` — same 30-min cron as
ingest + alerts + pickers. Idempotent: graded rows leave the pending
filter, so re-running just re-attempts un-graded ones.

CLI:
  python -m scripts.paper_bets_resolver

Sports without an ESPN mapping (UFC) stay pending forever — manual
resolution is fine for that low volume; we can add a UFC handler later.
"""
from __future__ import annotations

import logging
import sys
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from storage import supabase_client as db

log = logging.getLogger(__name__)


# Pending bets must be at least this old before we attempt to resolve.
# 4h is enough for any major sport to finish + ESPN to post the final.
RESOLVE_LAG_HOURS = 4

# Scanner sport code (uppercase) → (sport_group, league) on ESPN's
# scoreboard endpoint. Mirrors Flask's _ESPN_PATH but for scanner codes.
# UFC intentionally absent — no consolidated MMA scoreboard endpoint.
_ESPN_PATH: dict[str, tuple[str, str]] = {
    "MLB":   ("baseball",   "mlb"),
    "NBA":   ("basketball", "nba"),
    "NHL":   ("hockey",     "nhl"),
    "NFL":   ("football",   "nfl"),
    "CBB":   ("basketball", "mens-college-basketball"),
    "NCAAF": ("football",   "college-football"),
}


# ─────────────────────── Time / id helpers ───────────────────────

def _parse_iso(s: str) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def _espn_date_key(event_start_iso: str) -> str | None:
    """ESPN groups scoreboards by US/Eastern calendar date — a Sunday-
    night NBA game tipping at 22:00 ET goes on Sunday's board, not
    Monday's. Return YYYYMMDD in ET, or None on parse fail."""
    dt = _parse_iso(event_start_iso)
    if dt is None:
        return None
    return dt.astimezone(ZoneInfo("America/New_York")).strftime("%Y%m%d")


def _split_event_name(name: str) -> tuple[str, str] | tuple[None, None]:
    if " @ " in name:
        a, h = name.split(" @ ", 1)
        return a.strip(), h.strip()
    return None, None


# ─────────────────────── ESPN fetch + match ───────────────────────

def _fetch_espn(sport: str, date_yyyymmdd: str) -> list[dict[str, Any]]:
    """ESPN scoreboard for one sport+date. No auth, free, no rate limit
    at our volume. Returns events list or [] on error / unsupported."""
    pair = _ESPN_PATH.get(sport)
    if not pair:
        return []
    sport_grp, league = pair
    url = (f"https://site.api.espn.com/apis/site/v2/sports/"
           f"{sport_grp}/{league}/scoreboard")
    try:
        r = httpx.get(url, params={"dates": date_yyyymmdd}, timeout=10)
        if r.status_code != 200:
            log.warning("ESPN %s %s -> %s", sport, date_yyyymmdd, r.status_code)
            return []
        return (r.json() or {}).get("events", []) or []
    except Exception as e:
        log.warning("ESPN %s %s exception: %s", sport, date_yyyymmdd, e)
        return []


def _match_espn(bet: dict, espn_events: list[dict]) -> dict | None:
    """Two-way substring team match + commence_time within ±90 min,
    same shape as Flask's `_merge_espn_scores`. Returns
    {state, home_score, away_score} or None.

    state = ESPN's status.type.state — 'pre' / 'in' / 'post'. Caller
    only proceeds on 'post'."""
    away, home = _split_event_name(bet.get("event_name") or "")
    if not (away and home):
        return None
    away_n, home_n = away.lower(), home.lower()
    bet_start = _parse_iso(bet.get("event_start") or "")

    for g in espn_events:
        comp = (g.get("competitions") or [{}])[0]
        competitors = comp.get("competitors") or []
        if len(competitors) != 2:
            continue
        h = next((c for c in competitors if c.get("homeAway") == "home"), competitors[0])
        a = next((c for c in competitors if c.get("homeAway") == "away"), competitors[1])
        h_name = ((h.get("team") or {}).get("displayName") or "").lower()
        a_name = ((a.get("team") or {}).get("displayName") or "").lower()
        if not h_name or not a_name:
            continue
        if not ((home_n in h_name or h_name in home_n) and
                (away_n in a_name or a_name in away_n)):
            continue

        comp_dt_str = comp.get("date") or g.get("date") or ""
        comp_dt = _parse_iso(comp_dt_str) if comp_dt_str else None
        if bet_start and comp_dt:
            if abs((bet_start - comp_dt).total_seconds()) > 90 * 60:
                continue

        state = ((comp.get("status") or {}).get("type") or {}).get("state", "")

        def _score(c):
            v = c.get("score")
            if v is None or v == "":
                return None
            try:
                return int(v)
            except (ValueError, TypeError):
                return None

        return {
            "state":      state,
            "home_score": _score(h),
            "away_score": _score(a),
        }
    return None


# ─────────────────────── Grading + PnL ───────────────────────

def _grade(bet: dict, home_score: int, away_score: int) -> str | None:
    """Return 'won' / 'lost' / 'push' (or None if not gradable)."""
    mt   = bet.get("market_type")
    side = bet.get("side")
    line = bet.get("entry_line")

    if mt == "moneyline":
        if home_score == away_score:
            return "push"  # rare in most sports; possible in soccer/etc.
        winner = "home" if home_score > away_score else "away"
        return "won" if side == winner else "lost"

    if mt == "spread":
        if line is None:
            return None
        # Side's score margin + their line. Home -1.5: line=-1.5 → home
        # covers if (home-away)-1.5 > 0. Away +1.5: line=+1.5 → away
        # covers if (away-home)+1.5 > 0.
        if side == "home":
            margin = (home_score - away_score) + float(line)
        elif side == "away":
            margin = (away_score - home_score) + float(line)
        else:
            return None
        if margin > 0: return "won"
        if margin < 0: return "lost"
        return "push"

    if mt == "total":
        if line is None:
            return None
        total = home_score + away_score
        if side == "over":
            if total > float(line): return "won"
            if total < float(line): return "lost"
            return "push"
        if side == "under":
            if total < float(line): return "won"
            if total > float(line): return "lost"
            return "push"
        return None

    return None


def _pnl_units(status: str, entry_price: int) -> float:
    """Flat 1u sizing. Win @ +150 = +1.50u, win @ -110 = +0.909u,
    loss = -1.0u, push/void = 0u."""
    if status in ("push", "void"):
        return 0.0
    if status == "lost":
        return -1.0
    # won
    p = int(entry_price)
    if p > 0:
        return p / 100.0
    return 100.0 / abs(p)


# ─────────────────────── Supabase I/O ───────────────────────

def _fetch_pending(sb) -> list[dict]:
    cutoff = (datetime.now(timezone.utc)
              - timedelta(hours=RESOLVE_LAG_HOURS)).isoformat()
    try:
        return (sb.table("paper_bets")
                .select("id,bot,sport,event_name,event_start,market_type,"
                        "side,entry_book,entry_price,entry_line")
                .eq("status", "pending")
                .lt("event_start", cutoff)
                .order("event_start")
                .limit(500)
                .execute().data) or []
    except Exception as e:
        log.error("pending fetch failed: %s", e)
        return []


def _update(sb, bet_id: int, status: str, pnl: float,
            result_score: dict) -> bool:
    try:
        sb.table("paper_bets").update({
            "status":       status,
            "pnl_units":    pnl,
            "result_score": result_score,
            "settled_at":   datetime.now(timezone.utc).isoformat(),
        }).eq("id", bet_id).execute()
        return True
    except Exception as e:
        log.warning("update failed for bet %s: %s", bet_id, e)
        return False


# ─────────────────────── Main ───────────────────────

def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    sb = db.client()
    bets = _fetch_pending(sb)
    if not bets:
        log.info("no pending bets to resolve")
        return 0
    log.info("pending bets: %d", len(bets))

    # Cache ESPN scoreboards per (sport, date) within this run — most
    # nights one date pull covers 5-15 bets.
    espn_cache: dict[tuple[str, str], list] = {}
    won = lost = push = unmatched = not_final = unsupported = 0

    for bet in bets:
        sport = bet.get("sport") or ""
        if sport not in _ESPN_PATH:
            unsupported += 1
            continue
        date_key = _espn_date_key(bet.get("event_start") or "")
        if not date_key:
            unmatched += 1
            continue
        cache_key = (sport, date_key)
        if cache_key not in espn_cache:
            espn_cache[cache_key] = _fetch_espn(sport, date_key)
        events = espn_cache[cache_key]

        m = _match_espn(bet, events)
        if not m:
            unmatched += 1
            continue
        if m["state"] != "post":
            not_final += 1
            continue
        if m["home_score"] is None or m["away_score"] is None:
            unmatched += 1
            continue

        status = _grade(bet, m["home_score"], m["away_score"])
        if status is None:
            unmatched += 1
            continue
        pnl = _pnl_units(status, bet["entry_price"])
        result = {
            "home":  m["home_score"],
            "away":  m["away_score"],
            "total": m["home_score"] + m["away_score"],
        }
        if not _update(sb, bet["id"], status, pnl, result):
            continue

        if status == "won":  won  += 1
        elif status == "lost": lost += 1
        else:                  push += 1
        log.info("RESOLVED bot=%s %s %s/%s -> %s pnl=%+.3fu (%d-%d)",
                 bet["bot"], bet["event_name"], bet["market_type"],
                 bet["side"], status.upper(), pnl,
                 m["away_score"], m["home_score"])

    log.info("resolver done: won=%d lost=%d push=%d unmatched=%d "
             "not_final=%d unsupported=%d",
             won, lost, push, unmatched, not_final, unsupported)
    return 0


if __name__ == "__main__":
    sys.exit(main())
