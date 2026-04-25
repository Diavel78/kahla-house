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
from datetime import datetime, timedelta, timezone
from typing import Any

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
    "pinnacle":     "PIN",
    "draftkings":   "DK",
    "fanduel":      "FD",
    "betmgm":       "MGM",
    "caesars":      "CAE",
    "hardrockbet":  "HR",
    "hardrock":     "HR",
    "betonlineag":  "BOL",
    "betonline":    "BOL",
}

# Allowlist — only these books get written to book_snapshots. The Odds API
# EU region returns dozens of European books we don't care about. Anything
# whose mapped short code isn't in this set is silently dropped at ingest.
ALLOWED_BOOKS = {"PIN", "DK", "FD", "MGM", "CAE", "HR", "BOL"}

# How close (minutes) two event_start values must be to consider the same game.
MATCH_WINDOW = timedelta(minutes=30)


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

def ingest_sport(sport_code: str) -> dict[str, int]:
    counts = {"games": 0, "matched": 0, "created": 0, "candidate": 0,
              "snapshots": 0, "deduped": 0}
    raw = fetch_odds(sport_code)
    if raw is None:
        return counts
    games = parse_games(sport_code, raw)
    counts["games"] = len(games)
    if not games:
        log.info("Odds API %s: 0 games parsed", sport_code)
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
    return counts


def ingest_all() -> None:
    for sport in config.sports_enabled:
        if sport not in SPORT_KEYS:
            log.debug("skip sport %s (no Odds API mapping)", sport)
            continue
        try:
            ingest_sport(sport)
        except Exception as e:
            log.exception("Odds API ingest %s crashed: %s", sport, e)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(prog="odds_api")
    p.add_argument("--sport", help="Single sport (e.g. MLB). Default: all SPORTS_ENABLED.")
    args = p.parse_args(argv)
    if args.sport:
        ingest_sport(args.sport.upper())
    else:
        ingest_all()
    return 0


if __name__ == "__main__":
    sys.exit(main())
