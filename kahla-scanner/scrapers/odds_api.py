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

# How close (minutes) two event_start values must be to consider the same game.
MATCH_WINDOW = timedelta(minutes=30)


# ---------------------------------------------------------------------------
# Adaptive cadence — per-sport gate driven by time-to-nearest-game
# ---------------------------------------------------------------------------
#
# Before each Odds API call we look at the nearest upcoming game in that
# sport and pick a cadence bucket. Skip the sport entirely if nothing is
# within 18h or if we're inside the overnight blackout window.
#
# The cron-job.org trigger fires this workflow at high frequency (every
# 5 min); these gates decide per-sport whether THIS tick actually hits
# The Odds API. The two together = adaptive cadence without rewriting
# the scheduler. Cost model with us+eu (6 credits/call):
#
#   nearest game in:
#     ≤ 2h           → poll every 5 min   (final-2h sharp window)
#     2-6h           → poll every 15 min
#     6-18h          → poll every 30 min
#     18h+ / none    → skip (off-season / nothing meaningful pre-game)
#   overnight 10p-7a → skip regardless (no US games tip at 3am)
#   no event in next 7d → skip (off-season)
#
# Sports with staggered start times (MLB) will sit in the 5-min bucket
# for most of the afternoon — that's the point. Sport-wide endpoint
# returns all games in one call, so one MLB call covers 15 games at
# different start times.
#
# All times are evaluated in the user's local TZ (America/Phoenix, no
# DST). Phoenix is used everywhere else in the project for "today"
# anchoring; keeping it consistent here means the blackout window is
# 10pm-7am MT year-round.

_LOCAL_TZ = ZoneInfo("America/Phoenix")
_BLACKOUT_START_HOUR = 22  # 10pm local
_BLACKOUT_END_HOUR   = 7   # 7am local

# (max_hours_to_game, cadence_minutes). Picked left-to-right; first
# match wins. Trailing entry catches everything up to OFF_SEASON_LIMIT.
_CADENCE_BUCKETS: list[tuple[float, int]] = [
    (2.0,  5),
    (6.0,  15),
    (18.0, 30),
]
# Slack: cron-job.org ticks aren't perfectly evenly spaced and our own
# DB write has latency, so "fire if last_run + cadence_min <= now" is
# too tight — we'd skip the 5-min tick because the previous one wrote
# at 4m58s ago. Fire if we're within `slack` of the target window.
_CADENCE_SLACK_SEC = 60


def _in_overnight_blackout(now: datetime | None = None) -> bool:
    """True if local time is in the 10pm-7am blackout window."""
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


def _should_fire(sport_code: str) -> tuple[bool, dict[str, Any]]:
    """Decide whether to call The Odds API for `sport_code` on this tick.

    Returns (fire?, meta) where `meta` carries the diagnostic fields we
    log to odds_ingest_runs regardless of outcome:
        status        — 'ok' or 'skipped:<reason>' (final status set by caller on ok)
        cadence_min   — chosen cadence (None when skipped)
        next_game_h   — hours until nearest upcoming game (None when off-season)
        detail        — free-form note

    Pure function of (now, nearest event, last successful run); no
    side effects.
    """
    if _in_overnight_blackout():
        return False, {"status": "skipped:overnight", "detail": "10pm-7am MT"}

    nxt = db.nearest_upcoming_event(sport_code)
    if nxt is None:
        return False, {"status": "skipped:offseason",
                       "detail": "no event in next 7d"}

    now = datetime.now(timezone.utc)
    hours_to = (nxt - now).total_seconds() / 3600.0
    cadence = _cadence_for_next_game(hours_to)
    if cadence is None:
        return False, {"status": "skipped:cadence",
                       "next_game_h": hours_to,
                       "detail": f"nearest game in {hours_to:.1f}h (>18h cap)"}

    last = db.last_ingest_run(sport_code, status="ok")
    if last is not None:
        elapsed_sec = (now - last).total_seconds()
        target_sec = cadence * 60 - _CADENCE_SLACK_SEC
        if elapsed_sec < target_sec:
            return False, {
                "status": "skipped:cadence",
                "cadence_min": cadence,
                "next_game_h": hours_to,
                "detail": (f"last ok run {elapsed_sec:.0f}s ago, "
                           f"need {target_sec:.0f}s for {cadence}m cadence"),
            }

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
    """Return market_id for this game. Reuses an existing markets row if the
    teams + commence_time match within MATCH_WINDOW; otherwise inserts a new
    row. `existing` is hoisted out of the caller — pass the same active-markets
    list to every call within an ingest_sport run to avoid N+1 Supabase reads.
    """
    venue_key = matcher._teams_key(g.home, g.away, aliases)
    for row in existing:
        row_start = _parse_iso(row.get("event_start", ""))
        if row_start is None:
            # Bad/missing timestamp — skip (was previously falling back to
            # g.commence_time, which made the window check pass for ANY row
            # with an unparseable date and matched the wrong game).
            continue
        if abs(row_start - g.commence_time) > MATCH_WINDOW:
            continue
        row_away, row_home = matcher._split_event_name(row.get("event_name", ""))
        if row_home and row_away:
            if matcher._teams_key(row_home, row_away, aliases) == venue_key:
                return row["id"]
            score = matcher._fuzzy_teams_match(g.home, g.away, row_home, row_away, aliases)
            if score >= matcher.FUZZY_THRESHOLD:
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
