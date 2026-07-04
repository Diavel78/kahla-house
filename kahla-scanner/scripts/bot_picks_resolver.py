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

UFC ML auto-grades via ESPN's mma/ufc scoreboard (winner tag) — see
_ESPN_PATH below. Only method-of-victory (spread/total) props can't be
graded there and stay pending for manual settle.
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

        status = (comp.get("status") or {})
        state = (status.get("type") or {}).get("state", "")

        def _score(c):
            v = c.get("score")
            if v is None or v == "":
                return None
            try:
                return int(v)
            except (ValueError, TypeError):
                return None

        # First-inning runs from the per-period linescore (for NRFI/YRFI
        # grading). linescores[0].value = inning-1 runs for that competitor;
        # present once the game is underway. None until then.
        def _inn1(c):
            ls = c.get("linescores") or []
            if not ls:
                return None
            try:
                return int(float(ls[0].get("value")))
            except (ValueError, TypeError):
                return None

        # period = current inning (MLB). >= 2 means the 1st is fully done.
        try:
            period = int(status.get("period"))
        except (ValueError, TypeError):
            period = None

        return {
            "state":      state,
            "home_score": _score(h),
            "away_score": _score(a),
            "inn1_home":  _inn1(h),
            "inn1_away":  _inn1(a),
            "period":     period,
        }
    return None



def _grade_nrfi(bet: dict, m: dict) -> str | None:
    """Grade an NRFI/YRFI pick from the first-inning linescore. Returns
    won/lost, or None when the 1st inning isn't decided yet (retry next
    tick). YRFI (a run scored) resolves the instant either half-inning
    posts a run — even mid-game. NRFI requires the full 1st inning to be
    complete (state post, or we're already in inning >= 2)."""
    a1 = m.get("inn1_away")
    h1 = m.get("inn1_home")
    if a1 is None and h1 is None:
        return None  # no linescore yet — game hasn't really started
    ran = (a1 or 0) > 0 or (h1 or 0) > 0
    if not ran:
        first_done = (m.get("state") == "post") or ((m.get("period") or 0) >= 2)
        if not first_done:
            return None  # bottom of the 1st may still be in progress
    side = bet.get("side")
    if side == "yes":
        return "won" if ran else "lost"
    if side == "no":
        return "won" if not ran else "lost"
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


def _amer_to_prob(p) -> float | None:
    """American odds → implied probability (with vig)."""
    try:
        p = int(p)
    except (TypeError, ValueError):
        return None
    if p > 0:
        return 100.0 / (p + 100.0)
    if p < 0:
        return -p / (-p + 100.0)
    return 0.5


def _pin_close_pair(sb, market_id: str, market_type: str,
                    before_iso: str) -> dict | None:
    """PIN's last pre-event_start snapshot on BOTH sides of a market.

    Returns {side: implied_prob} once both sides have a snapshot before
    the close, else None. Mirrors app.py:_clv_pin_close_pair so Pick Bot
    CLV uses the same closing-line source as the dashboard CLV.
    """
    sides = ("over", "under") if market_type == "total" else ("home", "away")
    out: dict[str, float] = {}
    for side in sides:
        try:
            rows = (sb.table("book_snapshots")
                    .select("price_american")
                    .eq("market_id", market_id)
                    .eq("book", "PIN")
                    .eq("market_type", market_type)
                    .eq("side", side)
                    .lte("captured_at", before_iso)
                    .order("captured_at", desc=True)
                    .limit(1)
                    .execute().data) or []
        except Exception:
            continue
        if not rows:
            continue
        prob = _amer_to_prob(rows[0].get("price_american"))
        if prob is not None:
            out[side] = prob
    return out if len(out) == 2 else None


def _exch_close_pair(sb, market_id: str, market_type: str, line,
                     before_iso: str) -> dict | None:
    """Exchange closing line = the last pre-event_start pm_snapshots cents
    on BOTH sides, devigged. Kalshi mid is the independent ML anchor; PMM
    for SPR/TOT (Kalshi is ML-only). Mirrors handicapper_web._attach_exch_current.

    NOTE the vocab map: bot_picks/book_snapshots use 'moneyline' while
    pm_snapshots uses 'ml'. SPR/TOT match the bet's own line (else ATM).
    """
    pm_mt = "ml" if market_type == "moneyline" else market_type
    sides = ("over", "under") if market_type == "total" else ("home", "away")
    try:
        rows = (sb.table("pm_snapshots")
                .select("source,side,line,cents,captured_at")
                .eq("market_id", market_id)
                .eq("market_type", pm_mt)
                .lte("captured_at", before_iso)
                .order("captured_at", desc=True)
                .limit(600)
                .execute().data) or []
    except Exception:
        return None
    seen: dict = {}
    for r in rows:
        ln = r.get("line")
        try:
            ln = round(float(ln), 2) if ln is not None else None
        except (TypeError, ValueError):
            ln = None
        k = (r.get("source"), r.get("side"), ln)
        if k not in seen:
            try:
                seen[k] = float(r.get("cents"))
            except (TypeError, ValueError):
                pass
    if pm_mt == "ml":
        for src in ("kalshi", "pmm"):
            h = seen.get((src, "home", None))
            a = seen.get((src, "away", None))
            if h is not None and a is not None and (h + a) > 0:
                return {"home": h / (h + a), "away": a / (h + a)}
        return None
    up, down = sides
    cand_lines = {ln for (src, s, ln) in seen if src == "pmm" and s in sides}
    want = None
    if line is not None:
        try:
            want = round(float(line), 2)
        except (TypeError, ValueError):
            want = None
    chosen = want if (want is not None and want in cand_lines) else None
    if chosen is None:                      # ATM fallback (closest to 50)
        best_d = 1e9
        for ln in cand_lines:
            cu = seen.get(("pmm", up, ln))
            cd = seen.get(("pmm", down, ln))
            ref = cu if cu is not None else cd
            if ref is None:
                continue
            d = abs(ref - 50)
            if d < best_d:
                best_d, chosen = d, ln
    if chosen is None:
        return None
    cu = seen.get(("pmm", up, chosen))
    cd = seen.get(("pmm", down, chosen))
    if cu is not None and cd is not None and (cu + cd) > 0:
        return {up: cu / (cu + cd), down: cd / (cu + cd)}
    return None


def _compute_clv(sb, bet: dict) -> float | None:
    """Closing Line Value in percentage points for one pick.

    clv_pp = (closing_devig_prob_for_side − entry_implied_prob) × 100

    Closing line = the exchange (Kalshi/PMM) last pre-event_start devigged
    mid (cutover, June 2026) — falls back to PIN's book_snapshots close
    only while that feed is still warm. Positive = the bot was early on the
    side the line later moved toward (sharp). None when neither source has
    a closing pair or the entry price is unreadable.
    """
    market_id = bet.get("market_id")
    mt = bet.get("market_type")
    side = bet.get("side")
    if not (market_id and mt and side):
        return None
    entry_prob = _amer_to_prob(bet.get("entry_price"))
    if entry_prob is None:
        return None
    before = bet.get("event_start") or ""
    pair = _exch_close_pair(sb, market_id, mt, bet.get("entry_line"), before)
    if not pair or side not in pair:
        pair = _pin_close_pair(sb, market_id, mt, before)   # warm-feed fallback
    if not pair or side not in pair:
        return None
    total = sum(pair.values())
    if total <= 0:
        return None
    close_devig = pair[side] / total
    return round((close_devig - entry_prob) * 100.0, 2)


# Bot market_type → VSiN market_type. NRFI has no VSiN splits.
_VSIN_MT_MAP = {"moneyline": "ml", "spread": "spread", "total": "total"}
# Sports VSiN carries a splits view for — skip the closing lookup for the rest.
_VSIN_RESOLVE_SPORTS = {"MLB", "NBA", "NHL", "NFL", "NCAAF", "NCAAB", "CBB", "CFB"}


def _closing_vsin(sb, bet: dict) -> dict | None:
    """Last pre-event_start VSiN read (Circa + DraftKings handle%/bets%, both
    sides) for this pick's market, from vsin_snapshots. Paired with the
    bet-time read (signal_blob.vsin) it shows whether sharp money hit Circa
    late on the pick's side. None for NRFI / sports VSiN doesn't carry / no
    snapshot data. Computed once at grade time (the close is fixed)."""
    mid = bet.get("market_id")
    vmt = _VSIN_MT_MAP.get(bet.get("market_type") or "")
    before = bet.get("event_start") or ""
    if not (mid and vmt and before):
        return None
    try:
        rows = (sb.table("vsin_snapshots")
                .select("book,side,line,handle_pct,bets_pct,captured_at")
                .eq("market_id", mid).eq("market_type", vmt)
                .lt("captured_at", before)
                .order("captured_at", desc=True).limit(200).execute().data) or []
    except Exception:
        return None
    if not rows:
        return None
    out: dict = {}
    seen: set = set()
    last_at = None
    for r in rows:                      # desc → first per (book,side) is the close
        k = (r["book"], r["side"])
        if k in seen:
            continue
        seen.add(k)
        out.setdefault(r["book"], {})[r["side"]] = {
            "handle": r.get("handle_pct"), "bets": r.get("bets_pct"),
            "line": r.get("line")}
        last_at = last_at or r.get("captured_at")
    if not out:
        return None
    out["captured_at"] = last_at
    return out


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
                .select("id,market_id,sport,event_name,event_start,market_type,"
                        "side,entry_book,entry_price,entry_line,units,clv_pp,closing_vsin")
                .eq("status", "pending")
                .lt("event_start", cutoff)
                .order("event_start")
                .limit(500)
                .execute().data) or []
    except Exception as e:
        log.error("pending fetch failed: %s", e)
        return []


def _update(sb, pick_id: int, status: str, pnl: float,
            result_score: dict, clv_pp: float | None = None,
            closing_vsin: dict | None = None) -> bool:
    try:
        payload = {
            "status":       status,
            "pnl_units":    pnl,
            "result_score": result_score,
            "settled_at":   datetime.now(timezone.utc).isoformat(),
        }
        # Only write CLV when we have a value AND the row doesn't already
        # carry one — closing line is fixed at event_start, so it never
        # needs recomputing.
        if clv_pp is not None:
            payload["clv_pp"] = clv_pp
        if closing_vsin is not None:
            payload["closing_vsin"] = closing_vsin
        sb.table("bot_picks").update(payload).eq("id", pick_id).execute()
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


# ───────────────────── Pick Bot paperlog grading ─────────────────────
# The pickbot_paperlog table records EVERY gate-cleared suggestion the bot
# made over the 5h->1min window (whether or not the user bet it). We grade
# it with the EXACT same ESPN-match + to-WIN math as bot_picks, so the
# 2-week review buckets line up. MLB-only. Each row carries its own
# entry_price/line (a snapshot at suggestion time), so each grades + CLVs
# independently — the whole point ("did the 5h suggestion beat the 90m one?").

def _fetch_pending_paperlog(sb) -> list[dict]:
    now = datetime.now(timezone.utc).isoformat()
    try:
        return (sb.table("pickbot_paperlog")
                .select("id,market_id,sport,event_name,event_start,market_type,"
                        "side,entry_price,line,units,clv_pp,closing_vsin")
                .eq("status", "pending")
                .lt("event_start", now)
                .order("event_start")
                .limit(3000).execute().data) or []
    except Exception as e:
        log.error("paperlog pending fetch failed: %s", e)
        return []


def _update_paperlog(sb, row_id: int, status: str, pnl: float,
                     result_score: dict, clv_pp: float | None = None,
                     closing_vsin: dict | None = None) -> bool:
    import json as _json
    try:
        payload = {
            "status":       status,
            "pnl_units":    pnl,
            "result_score": _json.dumps(result_score) if result_score else None,
            "settled_at":   datetime.now(timezone.utc).isoformat(),
        }
        if clv_pp is not None:
            payload["clv_pp"] = clv_pp
        if closing_vsin is not None:
            payload["closing_vsin"] = closing_vsin   # jsonb column (dict, not text)
        sb.table("pickbot_paperlog").update(payload).eq("id", row_id).execute()
        return True
    except Exception as e:
        log.warning("paperlog update failed for %s: %s", row_id, e)
        return False


def _resolve_paperlog(sb) -> dict:
    """Grade pending pickbot_paperlog rows. Reuses the bot_picks helpers.
    Isolated + best-effort — never crashes the bot_picks resolver."""
    out = {"seen": 0, "won": 0, "lost": 0, "push": 0,
           "unmatched": 0, "not_final": 0}
    rows = _fetch_pending_paperlog(sb)
    out["seen"] = len(rows)
    if not rows:
        return out
    espn_cache: dict = {}
    for bet in rows:
        sport = bet.get("sport") or ""
        if sport not in _ESPN_PATH or bet.get("entry_price") is None:
            continue
        bet["entry_line"] = bet.get("line")   # _grade/_compute_clv expect entry_line
        date_key = _espn_date_key(bet.get("event_start") or "")
        if not date_key:
            out["unmatched"] += 1
            continue
        ck = (sport, date_key)
        if ck not in espn_cache:
            espn_cache[ck] = _fetch_espn(sport, date_key)
        m = _match_espn(bet, espn_cache[ck])
        if not m:
            out["unmatched"] += 1
            continue

        if bet.get("market_type") == "nrfi":
            status = _grade_nrfi(bet, m)
            if status is None:
                out["not_final"] += 1
                continue
            result = {"inn1_away": m.get("inn1_away"), "inn1_home": m.get("inn1_home")}
            clv = None
        else:
            if m.get("state") != "post":
                out["not_final"] += 1
                continue
            if m.get("home_score") is None or m.get("away_score") is None:
                out["unmatched"] += 1
                continue
            status = _grade(bet, m["home_score"], m["away_score"])
            if status is None:
                out["unmatched"] += 1
                continue
            result = {"home": m["home_score"], "away": m["away_score"],
                      "total": m["home_score"] + m["away_score"]}
            try:
                clv = _compute_clv(sb, bet)
            except Exception:
                clv = None

        units = bet.get("units") or 1
        pnl = _pnl_units(status, bet["entry_price"], units)
        cvsin = None
        if bet.get("closing_vsin") is None and sport in _VSIN_RESOLVE_SPORTS:
            try:
                cvsin = _closing_vsin(sb, bet)
            except Exception:
                cvsin = None
        _update_paperlog(sb, bet["id"], status, pnl, result, clv, cvsin)
        if status == "won":
            out["won"] += 1
        elif status == "lost":
            out["lost"] += 1
        elif status == "push":
            out["push"] += 1
    return out


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

            # NRFI / YRFI — graded off the 1st-inning linescore, NOT the
            # final, so it bypasses the post gate (can settle the moment
            # the 1st inning is decided). No CLV (no NRFI line in our feed).
            if bet.get("market_type") == "nrfi":
                status = _grade_nrfi(bet, m)
                if status is None:
                    not_final += 1
                    continue
                units = bet.get("units") or 1
                pnl = _pnl_units(status, bet["entry_price"], units)
                a1, h1 = m.get("inn1_away"), m.get("inn1_home")
                result = {"inn1_away": a1, "inn1_home": h1,
                          "first_inning_runs": (a1 or 0) + (h1 or 0)}
                if not _update(sb, bet["id"], status, pnl, result, None):
                    continue
                if status == "won":  won += 1
                else:                lost += 1
                log.info("RESOLVED bot_pick %s NRFI/%s @ %du -> %s pnl=%+.3fu (1st: %s-%s)",
                         bet["event_name"], bet["side"], units,
                         status.upper(), pnl, a1, h1)
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
            # Closing Line Value — compute once (skip if already set).
            # Best-effort: a missing PIN closing pair just leaves it NULL.
            clv = None
            if bet.get("clv_pp") is None:
                try:
                    clv = _compute_clv(sb, bet)
                except Exception as e:
                    log.warning("clv compute failed for pick %s: %s", bet["id"], e)
            # Closing VSiN read (Circa + DK handle/bets at the close) — the
            # bet-vs-close sharp-money tuning signal. Best-effort, set once.
            cvsin = None
            if bet.get("closing_vsin") is None and sport in _VSIN_RESOLVE_SPORTS:
                try:
                    cvsin = _closing_vsin(sb, bet)
                except Exception as e:
                    log.warning("closing_vsin failed for pick %s: %s", bet["id"], e)
            if not _update(sb, bet["id"], status, pnl, result, clv, cvsin):
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

        # Grade the Pick Bot paperlog too — isolated so it can never crash
        # the bot_picks resolver above (same ESPN data, reused helpers).
        try:
            pl = _resolve_paperlog(sb)
            if pl["seen"]:
                log.info("paperlog resolver: seen=%d won=%d lost=%d push=%d "
                         "unmatched=%d not_final=%d", pl["seen"], pl["won"],
                         pl["lost"], pl["push"], pl["unmatched"], pl["not_final"])
        except Exception as e:
            log.warning("paperlog resolver failed (bot_picks unaffected): %s", e)
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
