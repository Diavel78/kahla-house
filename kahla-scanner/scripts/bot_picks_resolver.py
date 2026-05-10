"""Handicapper Bot — pick resolver.

Pulls pending bot_picks rows whose event_start is more than
RESOLVE_LAG_HOURS ago, matches each to ESPN's final score, and grades
the row to won / lost / push with `pnl_units` (1/3/5u sizing per the
pick's `units` value).

Mirrors paper_bets_resolver.py exactly — same ESPN matching logic, same
±90 min commence-time tolerance, same grading rules. Only difference is
PnL math reads `units` from the row (paper_bets is flat 1u).

Runs as an appended step in scanner-poll.yml. Idempotent: graded rows
leave the pending filter, re-running just re-attempts un-graded ones.

CLI:
  python -m scripts.bot_picks_resolver

UFC stays pending forever — ESPN has no consolidated MMA scoreboard.
Manual resolution via SQL is fine for that low volume.
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


# 0 = check every pending pick from the moment its game starts. The
# real "is this game over" gate is ESPN state='post', not an arbitrary
# clock-based cushion. If a game's still in progress, the resolver
# counts it as `not_final` and tries again next cron tick — cheap and
# self-correcting. Was 4h, which delayed grading unnecessarily.
RESOLVE_LAG_HOURS = 0

_ESPN_PATH: dict[str, tuple[str, str]] = {
    "MLB":   ("baseball",   "mlb"),
    "NBA":   ("basketball", "nba"),
    "NHL":   ("hockey",     "nhl"),
    "NFL":   ("football",   "nfl"),
    "CBB":   ("basketball", "mens-college-basketball"),
    "NCAAF": ("football",   "college-football"),
    # UFC: ESPN's MMA scoreboard only supports ML grading (winner tag).
    # Spread / total method-of-victory bets stay pending — user can
    # settle them manually via the page button.
    "UFC":   ("mma",        "ufc"),
}


def _parse_iso(s: str) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def _espn_date_key(event_start_iso: str) -> str | None:
    dt = _parse_iso(event_start_iso)
    if dt is None:
        return None
    return dt.astimezone(ZoneInfo("America/New_York")).strftime("%Y%m%d")


def _split_event_name(name: str) -> tuple[str, str] | tuple[None, None]:
    if " @ " in name:
        a, h = name.split(" @ ", 1)
        return a.strip(), h.strip()
    return None, None


def _fetch_espn(sport: str, date_yyyymmdd: str) -> list[dict[str, Any]]:
    pair = _ESPN_PATH.get(sport)
    if not pair:
        return []
    grp, lg = pair
    url = (f"https://site.api.espn.com/apis/site/v2/sports/"
           f"{grp}/{lg}/scoreboard")
    try:
        r = httpx.get(url, params={"dates": date_yyyymmdd}, timeout=10)
        if r.status_code != 200:
            log.warning("ESPN %s %s -> %s", sport, date_yyyymmdd, r.status_code)
            return []
        return (r.json() or {}).get("events", []) or []
    except Exception as e:
        log.warning("ESPN %s %s exception: %s", sport, date_yyyymmdd, e)
        return []


import re

def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def _ufc_match_espn(bet: dict, espn_events: list[dict]) -> dict | None:
    """UFC-specific matcher. ESPN MMA scoreboard returns a flat list of
    fights (one event per competition with two `athlete` competitors).
    Match by fighter-name normalization in either orientation since
    UFC home/away assignment is arbitrary. Returns the standard match
    dict shape with `winner_home` / `winner_away` (bools) instead of
    numeric scores — UFC ML grading reads the winner boolean."""
    away, home = _split_event_name(bet.get("event_name") or "")
    if not (away and home):
        return None
    away_n, home_n = _norm(away), _norm(home)
    bet_start = _parse_iso(bet.get("event_start") or "")

    for g in espn_events:
        comp = (g.get("competitions") or [{}])[0]
        cs = comp.get("competitors") or []
        if len(cs) != 2:
            continue
        # ESPN MMA puts fighter info under `athlete`, not `team`. Try both.
        def _name(c):
            return _norm((c.get("athlete") or {}).get("displayName")
                         or (c.get("team") or {}).get("displayName") or "")
        n1, n2 = _name(cs[0]), _name(cs[1])
        if not (n1 and n2):
            continue
        # Try both orientations — UFC home/away is meaningless.
        std_match = ((home_n in n1 or n1 in home_n) and
                     (away_n in n2 or n2 in away_n))
        swap_match = ((home_n in n2 or n2 in home_n) and
                      (away_n in n1 or n1 in away_n))
        if not (std_match or swap_match):
            # Last-name token fallback for diacritics / hyphens.
            home_tokens = [t for t in home_n.split() if len(t) >= 3]
            away_tokens = [t for t in away_n.split() if len(t) >= 3]
            std_match = (any(t in n1 for t in home_tokens)
                         and any(t in n2 for t in away_tokens))
            swap_match = (any(t in n2 for t in home_tokens)
                          and any(t in n1 for t in away_tokens))
            if not (std_match or swap_match):
                continue
        # Map: which espn competitor corresponds to OUR home / away?
        if std_match:
            h_c, a_c = cs[0], cs[1]
        else:
            h_c, a_c = cs[1], cs[0]

        # 24h window — UFC card timing in our markets table is the
        # card-start, individual fight commence may differ by hours.
        comp_dt_s = comp.get("date") or g.get("date") or ""
        comp_dt = _parse_iso(comp_dt_s) if comp_dt_s else None
        if bet_start and comp_dt:
            if abs((bet_start - comp_dt).total_seconds()) > 24 * 3600:
                continue

        state = ((comp.get("status") or {}).get("type") or {}).get("state", "")

        return {
            "state":        state,
            "home_score":   None,   # UFC has no numeric score
            "away_score":   None,
            "winner_home":  bool(h_c.get("winner")),
            "winner_away":  bool(a_c.get("winner")),
        }
    return None


def _match_espn(bet: dict, espn_events: list[dict]) -> dict | None:
    away, home = _split_event_name(bet.get("event_name") or "")
    if not (away and home):
        return None
    away_n, home_n = away.lower(), home.lower()
    bet_start = _parse_iso(bet.get("event_start") or "")

    for g in espn_events:
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

        comp_dt_s = comp.get("date") or g.get("date") or ""
        comp_dt = _parse_iso(comp_dt_s) if comp_dt_s else None
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


def _grade(bet: dict, home_score: int, away_score: int) -> str | None:
    mt   = bet.get("market_type")
    side = bet.get("side")
    line = bet.get("entry_line")

    if mt == "moneyline":
        if home_score == away_score:
            return "push"
        winner = "home" if home_score > away_score else "away"
        return "won" if side == winner else "lost"

    if mt == "spread":
        if line is None:
            return None
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


def _pnl_units(status: str, entry_price: int, units: int) -> float:
    """To-WIN sizing. The user bets to win N units, not to risk N units.
    So a win is always exactly +units, regardless of price. A loss is
    whatever it cost to chase that win at the entry line.

    Examples:
      Win  3u @ +123 → +3.00u  (you wanted 3u, you got 3u)
      Win  1u @ -105 → +1.00u  (same)
      Lost 3u @ +123 → -2.44u  (needed to risk 3·100/123 = 2.44u to win 3u)
      Lost 1u @ -105 → -1.05u  (needed to risk 1·105/100 = 1.05u to win 1u)
      Push / void    →  0u
    """
    if status in ("push", "void"):
        return 0.0
    if status == "won":
        return float(units)
    p = int(entry_price)
    if p > 0:
        return -units * (100.0 / p)
    return -units * (abs(p) / 100.0)


def _fetch_pending(sb) -> list[dict]:
    cutoff = (datetime.now(timezone.utc)
              - timedelta(hours=RESOLVE_LAG_HOURS)).isoformat()
    try:
        return (sb.table("bot_picks")
                .select("id,sport,event_name,event_start,market_type,"
                        "side,entry_book,entry_price,entry_line,units")
                .eq("status", "pending")
                .lt("event_start", cutoff)
                .order("event_start")
                .limit(500)
                .execute().data) or []
    except Exception as e:
        log.error("pending fetch failed: %s", e)
        return []


def _update(sb, pick_id: int, status: str, pnl: float,
            result_score: dict) -> bool:
    try:
        sb.table("bot_picks").update({
            "status":       status,
            "pnl_units":    pnl,
            "result_score": result_score,
            "settled_at":   datetime.now(timezone.utc).isoformat(),
        }).eq("id", pick_id).execute()
        return True
    except Exception as e:
        log.warning("update failed for pick %s: %s", pick_id, e)
        return False


def _write_heartbeat(sb, summary: dict) -> None:
    """Write one row to resolver_runs so the page can surface 'last
    grading run Nm ago' + the breakdown. Best-effort — if the heartbeat
    write itself fails we just log and move on (don't crash the resolver
    over its own diagnostic)."""
    try:
        sb.table("resolver_runs").insert({"kind": "bot_picks", **summary}).execute()
    except Exception as e:
        log.warning("heartbeat write failed: %s", e)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    started = datetime.now(timezone.utc)
    summary: dict[str, Any] = {
        "picks_seen": 0, "won": 0, "lost": 0, "push": 0,
        "unmatched": 0, "not_final": 0, "unsupported": 0,
        "took_ms": None, "error": None,
    }
    sb = None
    try:
        sb = db.client()
        bets = _fetch_pending(sb)
        summary["picks_seen"] = len(bets)
        if not bets:
            log.info("no pending bot_picks to resolve")
            summary["took_ms"] = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
            _write_heartbeat(sb, summary)
            return 0
        log.info("pending bot_picks: %d", len(bets))

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

            if sport == "UFC":
                m = _ufc_match_espn(bet, events)
            else:
                m = _match_espn(bet, events)
            if not m:
                unmatched += 1
                continue
            if m["state"] != "post":
                not_final += 1
                continue

            # UFC: only ML grading is supported (winner boolean from
            # ESPN). SPR / TOT method-of-victory bets stay pending —
            # user settles them manually via the page.
            if sport == "UFC":
                if bet.get("market_type") != "moneyline":
                    unsupported += 1
                    continue
                if not (m["winner_home"] or m["winner_away"]):
                    not_final += 1   # No winner reported yet (NC, draw, ongoing)
                    continue
                if m["winner_home"] and m["winner_away"]:
                    status = "push"   # draw
                else:
                    won_side = "home" if m["winner_home"] else "away"
                    status = "won" if bet["side"] == won_side else "lost"
                result = {
                    "home":  None, "away": None, "total": None,
                    "winner_home": m["winner_home"],
                    "winner_away": m["winner_away"],
                }
            else:
                if m["home_score"] is None or m["away_score"] is None:
                    unmatched += 1
                    continue
                status = _grade(bet, m["home_score"], m["away_score"])
                if status is None:
                    unmatched += 1
                    continue
                result = {
                    "home":  m["home_score"],
                    "away":  m["away_score"],
                    "total": m["home_score"] + m["away_score"],
                }
            units = bet.get("units") or 1
            pnl = _pnl_units(status, bet["entry_price"], units)
            if not _update(sb, bet["id"], status, pnl, result):
                continue

            if status == "won":  won  += 1
            elif status == "lost": lost += 1
            else:                  push += 1
            score_frag = (
                f"({m['away_score']}-{m['home_score']})"
                if m.get("home_score") is not None else
                f"(winner: {'home' if m.get('winner_home') else 'away'})"
            )
            log.info("RESOLVED bot_pick %s %s/%s @ %du -> %s pnl=%+.3fu %s",
                     bet["event_name"], bet["market_type"], bet["side"],
                     units, status.upper(), pnl, score_frag)

        summary.update(won=won, lost=lost, push=push, unmatched=unmatched,
                       not_final=not_final, unsupported=unsupported)
        log.info("bot_picks resolver done: won=%d lost=%d push=%d unmatched=%d "
                 "not_final=%d unsupported=%d",
                 won, lost, push, unmatched, not_final, unsupported)
        summary["took_ms"] = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
        _write_heartbeat(sb, summary)
        return 0
    except Exception as e:
        # Capture full traceback in the heartbeat so we can debug from
        # the page (and avoid relying on `continue-on-error: true`
        # silently swallowing crashes).
        import traceback
        tb = traceback.format_exc()
        summary["error"] = (f"{type(e).__name__}: {e}\n{tb}")[:4000]
        summary["took_ms"] = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
        log.error("bot_picks resolver CRASHED: %s", summary["error"])
        if sb is not None:
            _write_heartbeat(sb, summary)
        return 1


if __name__ == "__main__":
    sys.exit(main())
