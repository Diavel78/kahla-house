"""The Odds API → Supabase ingest.

Replaces the retired Owls Insight scraper. Hits /v4/sports/{sport}/odds
for each enabled sport with regions=us,eu (EU is required for Pinnacle),
markets=h2h,spreads,totals, normalizes to BookSnapshot rows, and writes
deduped (only-on-change) rows to the same `book_snapshots` table the
charts read from.

API ref: https://the-odds-api.com/liveapi/guides/v4/

Books written (short codes used in book_snapshots.book):
  US region: DK, FD, MGM, CAE, HR, BET365, BR, BOL, LV
  EU region: PIN, plus any other EU book Odds API returns (passed through
             uppercased by short-code mapping)
  Note: Circa is NOT in The Odds API at all — that's a known data gap.

CLI:
  python -m scrapers.odds_api                  # all SPORTS_ENABLED sports
  python -m scrapers.odds_api --sport MLB      # one sport
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from config import config
from _lib import matcher
from _lib.normalize import american_to_prob
from storage import supabase_client as db
from storage.models import BookSnapshot, Market

log = logging.getLogger(__name__)

ODDS_API_BASE = "https://api.the-odds-api.com/v4"

# Scanner sport code (uppercase, stored in markets.sport)  ->  Odds API sport_key
SPORT_KEYS = {
    "MLB":   "baseball_mlb",
    "NBA":   "basketball_nba",
    "NHL":   "icehockey_nhl",
    "NFL":   "americanfootball_nfl",
    "CBB":   "basketball_ncaab",
    "NCAAF": "americanfootball_ncaaf",
    "UFC":   "mma_mixed_martial_arts",
}

# Odds API bookmaker key (lowercase)  ->  short code stored in book_snapshots.book.
BOOK_CODES = {
    # Sharp + the big-4 US retail
    "pinnacle":      "PIN",
    "draftkings":    "DK",
    "fanduel":       "FD",
    "betmgm":        "MGM",
    "caesars":       "CAE",
    # Other US-licensed / available-to-Rob books
    "hardrockbet":   "HR",
    "hardrock":      "HR",
    "bet365":        "BET365",
    "betrivers":     "BR",
    "betonlineag":   "BOL",
    "betonline":     "BOL",
    "lowvig":        "LV",
    "bovada":        "BVD",
    "espnbet":       "ESPN",
    "fanatics":      "FAN",
    "mybookieag":    "MB",
}

# Allowlist — only these short codes get written to book_snapshots. The Odds
# API EU region returns dozens of European books (winamax_fr, tipico_de,
# unibet_se, etc.) we don't care about; this set is the explicit US-style
# slate Rob can actually use. Anything whose mapped short code isn't here
# is silently dropped at ingest.
ALLOWED_BOOKS = {
    "PIN", "DK", "FD", "MGM", "CAE",
    "HR", "BET365", "BR", "BOL",
    "LV", "BVD", "ESPN", "FAN", "MB",
}

# How close two event_start values must be to consider the same game.
# Per-sport because MLB doubleheaders are real (same teams, 3-5h apart on
# the same day) and we MUST keep them as distinct markets. Everything
# else plays once per day per team — a same-team match within 12h is
# the same game even if the Odds API reports the commence_time hours
# off (placeholder time vs corrected tip-off — common cause of dupes).
MATCH_WINDOW = timedelta(minutes=30)  # legacy alias (MLB still uses it)
_MATCH_WINDOW_BY_SPORT: dict[str, timedelta] = {
    "MLB": timedelta(minutes=30),    # doubleheader protection
    "NBA": timedelta(hours=12),
    "NHL": timedelta(hours=12),
    "NFL": timedelta(hours=12),
    "CBB": timedelta(hours=12),
    "NCAAF": timedelta(hours=12),
    "UFC": timedelta(hours=6),       # UFC cards can have late/early changes
}


def _match_window_for(sport_code: str) -> timedelta:
    return _MATCH_WINDOW_BY_SPORT.get(sport_code, MATCH_WINDOW)


# ---------------------------------------------------------------------------
# Adaptive cadence — per-sport gate driven by time-to-nearest-game
# ---------------------------------------------------------------------------
#
# Before each Odds API call we look at the nearest upcoming game in that
# sport and pick a cadence bucket. Skip the sport entirely if nothing is
# within 18h or if we're inside the overnight blackout window.
#
# The cron-job.org trigger fires this workflow at high frequency (every
# 1 min); these gates decide per-sport whether THIS tick actually hits
# The Odds API. The two together = adaptive cadence without rewriting
# the scheduler. Cost model with us+eu (6 credits/call):
#
#   nearest game in:
#     ≤ 30 min       → poll every 2 min   (terminal steam window)
#     30 min – 2h    → poll every 5 min   (final-2h sharp window)
#     2 – 6h         → poll every 15 min
#     6 – 18h        → poll every 30 min
#     18h+ / none    → skip (off-season / nothing meaningful pre-game)
#   overnight 10p-7a → skip regardless (no US games tip at 3am)
#   no event in next 7d → skip (off-season)
#
# Sports with staggered start times (MLB) cycle UP into the 2-min bucket
# only during the actual 30-min pre-tip windows of individual games,
# then DOWN to 15-min cadence once a game tips and the next game is
# 2+ hours away. The sport-wide endpoint returns all games in one call,
# so one MLB call still covers 15 games at different start times.
#
# All times are evaluated in the user's local TZ (America/Phoenix, no
# DST). Phoenix is used everywhere else in the project for "today"
# anchoring; keeping it consistent here means the blackout window is
# 11pm-7am MT year-round.

_LOCAL_TZ = ZoneInfo("America/Phoenix")
_BLACKOUT_START_HOUR = 23  # 11pm local
_BLACKOUT_END_HOUR   = 7   # 7am local

# (max_hours_to_game, cadence_minutes). Picked left-to-right; first
# match wins. Trailing entry catches everything up to 18h.
#
# The 5-min bucket runs to 2.5h (not 2h) on purpose: the Pick Bot's
# picker/evaluator surfaces picks out to 150 min (the 120-150 "early"
# test window — sharp money often moves the line BEFORE the 2h mark, so
# we want to catch it). Polling those games at the old 15-min cadence
# would show stale lines and miss the very movement we're chasing, so
# the 120-150 window gets the same 5-min freshness as the 60-120
# betting window. Keep this boundary in sync with handicapper_web.py's
# EVAL_WINDOW_MAX (150). Modest credit cost — well within headroom.
_CADENCE_BUCKETS: list[tuple[float, int]] = [
    (0.5,  2),
    (2.5,  5),
    (6.0,  15),
    (18.0, 30),
]

# Discovery poll: when a sport has NO upcoming game in our DB it LOOKS
# off-season — but it might just be a scheduling GAP (e.g. Stanley Cup
# Final games sit 2+ days apart and the last one aged out of the window).
# If we skip forever we never DISCOVER the newly-scheduled games (the
# cold-start trap — won't fetch because nothing's upcoming, nothing's
# upcoming because we won't fetch). So re-probe The Odds API on this slow
# cadence; one hit re-seeds the DB and normal cadence resumes. Cheap
# (6 cr × ~4/day per dormant sport).
_DISCOVERY_SEC = 6 * 3600  # 6h

# Cross-confirm trigger (June 2026). For sports with PMM+Kalshi cent data,
# we stop blind-polling FAR out (>3h) and instead pull only when Flask's
# /api/pm-snapshot detects both free feeds moving >=1c same direction (it
# writes an odds_pull_requests row). Near window (<=3h) keeps tight blind
# cadence (covers the 60-180min prime window). Non-trigger sports keep the
# legacy 15/30-min far cadence. Add a sport here once its cent data flows.
_TRIGGER_SPORTS = {"MLB", "NBA", "NHL"}
_PULL_REQ_FRESH_SEC = 600     # a pull-request is valid for 10 min
_TRIGGER_MIN_GAP_SEC = 300    # per-sport 5-min cap between triggered pulls
_NEAR_WINDOW_H = 3.0          # blind tight-cadence ceiling (= prime edge)


def _slack_for(cadence_min: int) -> int:
    """Slack window in seconds so cron-job.org tick jitter doesn't make
    us skip when we should fire. Scaled to ~15% of cadence with a 10s
    floor and 60s ceiling so it never exceeds a meaningful fraction of
    the cadence (e.g. 60s slack on a 2-min bucket would let us fire at
    1m45s = effectively 1.75-min cadence, wasting credits)."""
    return min(60, max(10, int(cadence_min * 60 * 0.15)))


def _in_overnight_blackout(now: datetime | None = None) -> bool:
    """True if local time is in the 11pm-7am blackout window."""
    now = now or datetime.now(timezone.utc)
    local = now.astimezone(_LOCAL_TZ)
    hr = local.hour
    if _BLACKOUT_START_HOUR <= _BLACKOUT_END_HOUR:
        return _BLACKOUT_START_HOUR <= hr < _BLACKOUT_END_HOUR
    # Window wraps midnight (the common case: 22-7).
    return hr >= _BLACKOUT_START_HOUR or hr < _BLACKOUT_END_HOUR


def _cadence_for_next_game(hours_to_game: float | None) -> int | None:
    """Returns the cadence in minutes for the given time-to-nearest-game,
    or None if we should skip this sport (no games in 18h)."""
    if hours_to_game is None:
        return None
    if hours_to_game < 0:
        # Game already started — useful only if more games follow soon.
        # The caller should pass the NEXT upcoming game (not in-progress),
        # so a negative value here means "no more games today, off-season-ish".
        return None
    for max_h, cad in _CADENCE_BUCKETS:
        if hours_to_game <= max_h:
            return cad
    return None  # >18h out


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception:
        return None


def _pending_pull_request(sport_code: str, now: datetime) -> bool:
    """True if a FRESH unconsumed cross-confirm pull-request exists for the
    sport (written by Flask's /api/pm-snapshot when PMM+Kalshi co-moved)."""
    try:
        rows = (db.client().table("odds_pull_requests")
                .select("requested_at,consumed_at").eq("sport", sport_code)
                .limit(1).execute().data) or []
    except Exception:
        return False
    if not rows:
        return False
    req = _parse_iso(rows[0].get("requested_at"))
    con = _parse_iso(rows[0].get("consumed_at"))
    if req is None or (now - req).total_seconds() > _PULL_REQ_FRESH_SEC:
        return False               # none / stale
    return con is None or con < req  # unconsumed


def _consume_pull_request(sport_code: str, now: datetime) -> None:
    try:
        db.client().table("odds_pull_requests").update(
            {"consumed_at": now.isoformat()}).eq("sport", sport_code).execute()
    except Exception:
        pass


def _should_fire(sport_code: str) -> tuple[bool, dict[str, Any]]:
    """Decide whether to call The Odds API for `sport_code` on this tick.
    Returns (fire?, meta) with the diagnostic fields logged to
    odds_ingest_runs. NOT pure — consumes a pull-request on a trigger fire.

    Cadence model (June 2026): tight blind polling <=3h (2-min terminal,
    5-min to the 3h prime edge); FAR out (>3h) is **trigger-only** for
    `_TRIGGER_SPORTS` (pull when the free PMM+Kalshi feeds cross-confirm a
    move, per-sport 5-min cap) and legacy 15/30-min blind for the rest.
    Blackout 11pm-7am MT, with one 3am snapshot per in-season sport."""
    now = datetime.now(timezone.utc)
    local = now.astimezone(_LOCAL_TZ)

    if _in_overnight_blackout(now):
        # One overnight snapshot at ~3am MT so we see what lines did while
        # asleep. One-shot: requires the last ok run >30min old (blackout
        # means nothing pulled since ~9:55pm, so the first 3am tick fires
        # and the rest skip on the fresh last-run).
        if local.hour == 3 and db.nearest_upcoming_event(sport_code) is not None:
            last_ok = db.last_ingest_run(sport_code, status="ok")
            if last_ok is None or (now - last_ok).total_seconds() > 1800:
                return True, {"status": "ok", "cadence_min": None,
                              "detail": "3am overnight snapshot"}
        return False, {"status": "skipped:overnight", "detail": "11pm-7am MT"}

    nxt = db.nearest_upcoming_event(sport_code)
    if nxt is None:
        last_ok = db.last_ingest_run(sport_code, status="ok")
        if last_ok is not None and (now - last_ok).total_seconds() < _DISCOVERY_SEC:
            wait_h = (_DISCOVERY_SEC - (now - last_ok).total_seconds()) / 3600.0
            return False, {"status": "skipped:offseason",
                           "detail": f"no upcoming game; next discovery probe in {wait_h:.1f}h"}
        return True, {"status": "ok", "cadence_min": None,
                      "detail": "discovery probe (no upcoming game in DB)"}

    hours_to = (nxt - now).total_seconds() / 3600.0
    if hours_to < 0:
        return False, {"status": "skipped:cadence", "next_game_h": hours_to,
                       "detail": "nearest event already started"}
    last = db.last_ingest_run(sport_code, status="ok")
    elapsed = (now - last).total_seconds() if last is not None else 1e9

    # Near window (<=3h): tight blind cadence covering the prime window.
    if hours_to <= _NEAR_WINDOW_H:
        cadence = 2 if hours_to <= 0.5 else 5
        target = cadence * 60 - _slack_for(cadence)
        if elapsed < target:
            return False, {"status": "skipped:cadence", "cadence_min": cadence,
                           "next_game_h": hours_to,
                           "detail": f"last ok {elapsed:.0f}s ago, need {target:.0f}s"}
        return True, {"cadence_min": cadence, "next_game_h": hours_to}

    # Far window (>3h).
    if sport_code in _TRIGGER_SPORTS:
        if elapsed >= _TRIGGER_MIN_GAP_SEC and _pending_pull_request(sport_code, now):
            _consume_pull_request(sport_code, now)
            return True, {"status": "ok", "cadence_min": None, "next_game_h": hours_to,
                          "detail": "xconfirm trigger pull"}
        return False, {"status": "skipped:cadence", "next_game_h": hours_to,
                       "detail": f"far ({hours_to:.1f}h) — trigger-only, awaiting xconfirm"}

    # Non-trigger sport: legacy blind far cadence (15/30 min).
    if hours_to <= 6.0:
        cadence = 15
    elif hours_to <= 18.0:
        cadence = 30
    else:
        return False, {"status": "skipped:cadence", "next_game_h": hours_to,
                       "detail": f"nearest game in {hours_to:.1f}h (>18h cap)"}
    target = cadence * 60 - _slack_for(cadence)
    if elapsed < target:
        return False, {"status": "skipped:cadence", "cadence_min": cadence,
                       "next_game_h": hours_to,
                       "detail": f"last ok {elapsed:.0f}s ago, need {target:.0f}s"}
    return True, {"cadence_min": cadence, "next_game_h": hours_to}


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def _api_key() -> str:
    key = os.getenv("ODDS_API_KEY", "").strip()
    if not key:
        raise RuntimeError("ODDS_API_KEY not set")
    return key


def fetch_odds(sport_code: str) -> list[dict[str, Any]] | None:
    """GET /sports/{sport_key}/odds. Returns the events list or None on error."""
    sport_key = SPORT_KEYS.get(sport_code)
    if not sport_key:
        log.warning("no Odds API sport_key for %s", sport_code)
        return None
    url = f"{ODDS_API_BASE}/sports/{sport_key}/odds"
    params = {
        "api_key":     _api_key(),   # Odds API uses snake_case here, not "apiKey"
        # `us,eu` because Pinnacle is in the EU region, not US — without it
        # we'd lose PIN entirely (the sharpest book and the whole point of
        # the line-movement chart). Cost = markets × regions = 3 × 2 = 6
        # credits per call. Cron cadence is set to 30 min in the workflow
        # to fit the $59/100K-credit tier (60K credits/mo).
        "regions":     "us,eu",
        "markets":     "h2h,spreads,totals",
        "oddsFormat":  "american",
        "dateFormat":  "iso",
    }
    try:
        r = httpx.get(url, params=params, timeout=20)
        if r.status_code != 200:
            log.warning("Odds API %s -> %s %s", sport_code, r.status_code, r.text[:200])
            return None
        # Surface remaining-credits in logs so we notice when the budget shrinks
        used = r.headers.get("x-requests-used", "?")
        remaining = r.headers.get("x-requests-remaining", "?")
        log.info("Odds API %s: used=%s remaining=%s", sport_code, used, remaining)
        return r.json()
    except Exception as e:
        log.warning("Odds API %s exception: %s", sport_code, e)
        return None


# ---------------------------------------------------------------------------
# Parse
# ---------------------------------------------------------------------------

@dataclass
class OddsApiGame:
    sport: str                        # MLB / NBA / etc.
    event_id: str                     # Odds API event id (uuid)
    home: str
    away: str
    commence_time: datetime
    bookmakers: list[dict[str, Any]]  # raw bookmakers list from response


def _parse_iso(s: str) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def parse_games(sport_code: str, raw: list[dict[str, Any]]) -> list[OddsApiGame]:
    out: list[OddsApiGame] = []
    if not isinstance(raw, list):
        return out
    for ev in raw:
        if not isinstance(ev, dict):
            continue
        eid = ev.get("id") or ""
        home = ev.get("home_team") or ""
        away = ev.get("away_team") or ""
        ct = _parse_iso(ev.get("commence_time", ""))
        if not (eid and home and away and ct):
            continue
        out.append(OddsApiGame(
            sport=sport_code,
            event_id=eid,
            home=home,
            away=away,
            commence_time=ct,
            bookmakers=ev.get("bookmakers", []) or [],
        ))
    return out


def _book_code(odds_api_key: str) -> str:
    """Map Odds API bookmaker key to our short code. Unknown books pass through
    uppercased so we never silently drop data."""
    return BOOK_CODES.get(odds_api_key, odds_api_key.upper())


def build_snapshots(g: OddsApiGame, market_id: str) -> list[BookSnapshot]:
    """Convert one game's bookmakers list into BookSnapshot rows."""
    out: list[BookSnapshot] = []
    for bk in g.bookmakers:
        bk_key = (bk.get("key") or "").lower()
        if not bk_key:
            continue
        # Allowlist: skip Euro books and anything not on our shortlist.
        if _book_code(bk_key) not in ALLOWED_BOOKS:
            continue
        book = _book_code(bk_key)
        for mkt in bk.get("markets", []) or []:
            mkt_key = mkt.get("key", "")
            outcomes = mkt.get("outcomes", []) or []
            if mkt_key == "h2h":
                for o in outcomes:
                    name = o.get("name", "")
                    price = o.get("price")
                    if price is None:
                        continue
                    side = "home" if name == g.home else "away" if name == g.away else None
                    if not side:
                        continue
                    out.append(BookSnapshot(
                        market_id=market_id, book=book,
                        market_type="moneyline", side=side,
                        price_american=int(price), line=None,
                        implied_prob=american_to_prob(int(price)),
                    ))
            elif mkt_key == "spreads":
                for o in outcomes:
                    name = o.get("name", "")
                    price = o.get("price")
                    point = o.get("point")
                    if price is None or point is None:
                        continue
                    side = "home" if name == g.home else "away" if name == g.away else None
                    if not side:
                        continue
                    out.append(BookSnapshot(
                        market_id=market_id, book=book,
                        market_type="spread", side=side,
                        price_american=int(price), line=float(point),
                        implied_prob=american_to_prob(int(price)),
                    ))
            elif mkt_key == "totals":
                for o in outcomes:
                    name = (o.get("name") or "").lower()
                    price = o.get("price")
                    point = o.get("point")
                    if price is None or point is None:
                        continue
                    side = "over" if name == "over" else "under" if name == "under" else None
                    if not side:
                        continue
                    out.append(BookSnapshot(
                        market_id=market_id, book=book,
                        market_type="total", side=side,
                        price_american=int(price), line=float(point),
                        implied_prob=american_to_prob(int(price)),
                    ))
    return out


# ---------------------------------------------------------------------------
# Find-or-create market (matches owls.py pattern so existing markets are reused)
# ---------------------------------------------------------------------------

def _find_or_create_market(
    g: OddsApiGame,
    aliases: dict[str, str],
    existing: list[dict[str, Any]],
) -> str | None:
    """Return market_id for this game. Reuses an existing markets row if
    the teams match within the sport's match window; otherwise inserts a
    new row. `existing` is hoisted out of the caller — pass the same
    active-markets list to every call within an ingest_sport run to
    avoid N+1 Supabase reads.

    If the API now reports a different commence_time than the existing
    row stores (e.g., placeholder time corrected to the real tip), the
    existing row's event_start is UPDATED in place to the new time. Was
    creating a duplicate markets row instead — that's how
    "Cavaliers @ Pistons" ended up listed twice on /handicapper.
    """
    window = _match_window_for(g.sport)
    venue_key = matcher._teams_key(g.home, g.away, aliases)
    for row in existing:
        row_start = _parse_iso(row.get("event_start", ""))
        if row_start is None:
            # Bad/missing timestamp — skip (was previously falling back to
            # g.commence_time, which made the window check pass for ANY row
            # with an unparseable date and matched the wrong game).
            continue
        if abs(row_start - g.commence_time) > window:
            continue
        row_away, row_home = matcher._split_event_name(row.get("event_name", ""))
        matched = False
        if row_home and row_away:
            if matcher._teams_key(row_home, row_away, aliases) == venue_key:
                matched = True
            else:
                score = matcher._fuzzy_teams_match(g.home, g.away, row_home, row_away, aliases)
                if score >= matcher.FUZZY_THRESHOLD:
                    matched = True
        if not matched:
            continue
        # Existing row matches the same game — update its event_start
        # if the API now reports a different time. Mutating in place
        # keeps the row+id stable so all attached book_snapshots stay
        # linked to it (no orphan history when a game is rescheduled).
        if abs(row_start - g.commence_time) > timedelta(minutes=2):
            try:
                db.client().table("markets").update(
                    {"event_start": g.commence_time.isoformat()}
                ).eq("id", row["id"]).execute()
                row["event_start"] = g.commence_time.isoformat()
                log.info(
                    "markets %s event_start %s → %s (drift %.0fm)",
                    row["id"][:8], row_start.isoformat(),
                    g.commence_time.isoformat(),
                    (g.commence_time - row_start).total_seconds() / 60.0,
                )
            except Exception as e:
                log.warning("event_start update failed for %s: %s",
                            row.get("id"), e)
        return row["id"]
    # No match — create a new markets row.
    m = Market(
        sport=g.sport,
        event_name=f"{g.away} @ {g.home}",
        event_start=g.commence_time,
    )
    try:
        row = db.upsert_market(m)
        new_id = row.get("id")
        # Keep our local list current so subsequent matches in the same run
        # see the row we just created.
        if new_id:
            existing.append({
                "id":          new_id,
                "event_name":  m.event_name,
                "event_start": m.event_start.isoformat(),
                "sport":       m.sport,
                "status":      "active",
            })
        return new_id
    except Exception as e:
        log.warning("upsert_market failed for %s @ %s: %s", g.away, g.home, e)
        return None


# ---------------------------------------------------------------------------
# Dedup (only persist snapshots that changed since last cycle)
# ---------------------------------------------------------------------------

def _latest_snapshot_map(
    market_ids: list[str],
    within_minutes: int = 1440,  # 24h is enough — sharp books rarely sit longer
) -> dict[tuple[str, str, str, str], tuple[int, float | None]]:
    if not market_ids:
        return {}
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=within_minutes)).isoformat()
    latest: dict[tuple[str, str, str, str], tuple[int, float | None, str]] = {}
    CHUNK = 100
    for i in range(0, len(market_ids), CHUNK):
        chunk = market_ids[i:i + CHUNK]
        res = (
            db.client()
            .table("book_snapshots")
            .select("market_id,book,market_type,side,price_american,line,captured_at")
            .in_("market_id", chunk)
            .gte("captured_at", cutoff)
            .order("captured_at", desc=True)
            .limit(20000)
            .execute()
        )
        for r in res.data or []:
            key = (r["market_id"], r["book"], r["market_type"], r["side"])
            if key in latest and latest[key][2] >= r["captured_at"]:
                continue
            latest[key] = (r["price_american"], r["line"], r["captured_at"])
    return {k: (v[0], v[1]) for k, v in latest.items()}


def _dedup_unchanged(
    snaps: list[BookSnapshot], latest: dict[tuple[str, str, str, str], tuple[int, float | None]]
) -> list[BookSnapshot]:
    out: list[BookSnapshot] = []
    for s in snaps:
        key = (s.market_id, s.book, s.market_type, s.side)
        prev = latest.get(key)
        if prev is None:
            out.append(s)
            continue
        prev_price, prev_line = prev
        if s.price_american != prev_price or s.line != prev_line:
            out.append(s)
    return out


# ---------------------------------------------------------------------------
# Ingest one sport / all sports
# ---------------------------------------------------------------------------

def ingest_sport(sport_code: str, force: bool = False) -> dict[str, int]:
    """Fetch + write snapshots for one sport, gated by the adaptive
    cadence rules in `_should_fire`. Pass `force=True` to bypass the gate
    (useful for manual runs / `--force` on the CLI).

    Every invocation writes a heartbeat row to `odds_ingest_runs` —
    skipped ticks too — so we can see per-sport that the cron is alive
    and which decisions it's making.
    """
    counts = {"games": 0, "matched": 0, "created": 0, "candidate": 0,
              "snapshots": 0, "deduped": 0}

    meta: dict[str, Any] = {}
    if not force:
        fire, meta = _should_fire(sport_code)
        if not fire:
            log.info("Odds API %s: %s (%s)",
                     sport_code, meta.get("status"), meta.get("detail"))
            db.record_ingest_run(
                sport=sport_code,
                status=meta.get("status") or "skipped:unknown",
                cadence_min=meta.get("cadence_min"),
                next_game_h=meta.get("next_game_h"),
                detail=meta.get("detail"),
            )
            return counts

    raw = fetch_odds(sport_code)
    if raw is None:
        db.record_ingest_run(
            sport=sport_code,
            status="error",
            cadence_min=meta.get("cadence_min"),
            next_game_h=meta.get("next_game_h"),
            detail="fetch_odds returned None",
        )
        return counts
    games = parse_games(sport_code, raw)
    counts["games"] = len(games)
    if not games:
        log.info("Odds API %s: 0 games parsed", sport_code)
        db.record_ingest_run(
            sport=sport_code,
            status="ok",
            cadence_min=meta.get("cadence_min"),
            next_game_h=meta.get("next_game_h"),
            events=0,
            snapshots=0,
            detail="no games returned",
        )
        return counts

    aliases = db.list_team_aliases(sport_code)
    # Hoist the active-markets list out of the per-game loop. Was N+1: 30
    # MLB games made 30 identical Supabase queries to fetch active markets.
    # Now: one fetch, mutated in place when a new market is inserted.
    existing_markets = db.list_active_markets(sport_code)
    existing_count = len(existing_markets)

    all_snaps: list[BookSnapshot] = []
    market_ids: list[str] = []
    for g in games:
        mid = _find_or_create_market(g, aliases, existing_markets)
        if not mid:
            continue
        market_ids.append(mid)
        all_snaps.extend(build_snapshots(g, mid))

    counts["created"] = max(0, len(existing_markets) - existing_count)
    counts["matched"] = counts["games"] - counts["created"]
    counts["candidate"] = len(all_snaps)

    latest = _latest_snapshot_map(market_ids)
    to_write = _dedup_unchanged(all_snaps, latest)
    counts["deduped"] = len(all_snaps) - len(to_write)

    if to_write:
        try:
            db.insert_book_snapshots(to_write)
            counts["snapshots"] = len(to_write)
        except Exception as e:
            log.exception("insert_book_snapshots(%s) failed: %s", sport_code, e)

    log.info(
        "Odds API %s: %d games, %d matched, %d created, %d candidate, %d dedup'd, %d written",
        sport_code, counts["games"], counts["matched"], counts["created"],
        counts["candidate"], counts["deduped"], counts["snapshots"],
    )
    db.record_ingest_run(
        sport=sport_code,
        status="ok",
        cadence_min=meta.get("cadence_min"),
        next_game_h=meta.get("next_game_h"),
        events=counts["games"],
        snapshots=counts["snapshots"],
    )
    return counts


def ingest_all(force: bool = False) -> None:
    for sport in config.sports_enabled:
        if sport not in SPORT_KEYS:
            log.debug("skip sport %s (no Odds API mapping)", sport)
            continue
        try:
            ingest_sport(sport, force=force)
        except Exception as e:
            log.exception("Odds API ingest %s crashed: %s", sport, e)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(prog="odds_api")
    p.add_argument("--sport", help="Single sport (e.g. MLB). Default: all SPORTS_ENABLED.")
    p.add_argument(
        "--force",
        action="store_true",
        help="Bypass the adaptive cadence gate. Always hit the API. "
             "Useful for backfills/debugging — every call still costs 6 credits.",
    )
    args = p.parse_args(argv)
    if args.sport:
        ingest_sport(args.sport.upper(), force=args.force)
    else:
        ingest_all(force=args.force)
    return 0


if __name__ == "__main__":
    sys.exit(main())
