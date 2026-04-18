"""Owls Insight → Supabase ingest.

Replaces direct DK/FD/Polymarket scrapers with one unified pull from
Owls Insight (https://owlsinsight.com), which aggregates 13 US sportsbooks
plus Polymarket odds into a single API.

For each sport, we pull /{sport}/odds, match each game to an existing
`markets` row (creating one if new), then insert a BookSnapshot per
(book, market_type, side).

Books (short codes written to book_snapshots.book):
  POLY, PIN, DK, FD, CIR, MGM, CAE, HR, NVG

CLI:
  python -m scrapers.owls                  # all SPORTS_ENABLED sports
  python -m scrapers.owls --sport MLB      # one sport
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
from signals import matcher
from signals.normalize import american_to_prob
from storage import supabase_client as db
from storage.models import BookSnapshot, Market

log = logging.getLogger(__name__)

OWLS_BASE = "https://api.owlsinsight.com/api/v1"

# Scanner sport code (uppercase) -> Owls API sport path (lowercase)
SPORT_PATHS = {
    "MLB":   "mlb",
    "NBA":   "nba",
    "NHL":   "nhl",
    "NFL":   "nfl",
    "CBB":   "ncaab",
    "NCAAF": "ncaaf",
    "UFC":   "mma",
}

# Owls bookmaker key -> short code stored in book_snapshots.book
BOOK_CODES = {
    "polymarket":  "POLY",
    "pinnacle":    "PIN",
    "draftkings":  "DK",
    "fanduel":     "FD",
    "circa":       "CIR",
    "betmgm":      "MGM",
    "caesars":     "CAE",
    "hardrock":    "HR",
    "novig":       "NVG",
    # Skipped: wynn, westgate, south_point, stations (noisy Vegas books
    # with limited coverage). Add later if we want them.
}

# How close (minutes) two event_start values must be to consider the same game.
MATCH_WINDOW = timedelta(minutes=30)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def _api_key() -> str:
    key = os.getenv("OWLS_INSIGHT_API_KEY", "").strip()
    if not key:
        raise RuntimeError("OWLS_INSIGHT_API_KEY not set")
    return key


def fetch_odds(sport: str) -> dict[str, Any] | None:
    """GET /{sport}/odds. Returns raw JSON or None on error."""
    path = SPORT_PATHS.get(sport)
    if not path:
        log.warning("no Owls path for sport %s", sport)
        return None
    url = f"{OWLS_BASE}/{path}/odds"
    headers = {
        "Authorization": f"Bearer {_api_key()}",
        "User-Agent":    "kahla-scanner/1.0",
    }
    try:
        r = httpx.get(url, headers=headers, timeout=20)
        if r.status_code != 200:
            log.warning("Owls %s -> %s %s", sport, r.status_code, r.text[:200])
            return None
        return r.json()
    except Exception as e:
        log.warning("Owls %s exception: %s", sport, e)
        return None


# ---------------------------------------------------------------------------
# Parse
# ---------------------------------------------------------------------------

@dataclass
class OwlsGame:
    sport: str
    event_id: str            # Owls event id
    home: str
    away: str
    commence_time: datetime
    book_markets: dict[str, dict[str, list[dict[str, Any]]]]
    # book_markets[book_code]['h2h'] = [{name, price}, ...]
    # book_markets[book_code]['spreads'] = [{name, price, point}, ...]
    # book_markets[book_code]['totals']  = [{name, price, point}, ...]


def _parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def parse_games(sport: str, raw: dict[str, Any]) -> list[OwlsGame]:
    """Collapse the per-book response into one game per (sport, teams, time)."""
    data = raw.get("data") or {}
    if not isinstance(data, dict):
        return []

    games: dict[str, OwlsGame] = {}
    for top_key, events in data.items():
        if not isinstance(events, list):
            continue
        for ev in events:
            eid = str(ev.get("eventId") or ev.get("id") or "")
            home = ev.get("home_team") or ""
            away = ev.get("away_team") or ""
            start = _parse_ts(ev.get("commence_time"))
            if not (eid and home and away and start):
                continue
            game = games.get(eid)
            if game is None:
                game = OwlsGame(
                    sport=sport,
                    event_id=eid,
                    home=home,
                    away=away,
                    commence_time=start,
                    book_markets={},
                )
                games[eid] = game
            for b in ev.get("bookmakers") or []:
                book_key = (b.get("key") or "").lower()
                code = BOOK_CODES.get(book_key)
                if not code:
                    continue
                bm = game.book_markets.setdefault(code, {"h2h": [], "spreads": [], "totals": []})
                for m in b.get("markets") or []:
                    mk = m.get("key")
                    outs = m.get("outcomes") or []
                    if mk in ("h2h", "moneyline"):
                        bm["h2h"].extend(outs)
                    elif mk == "spreads":
                        bm["spreads"].extend(outs)
                    elif mk == "totals":
                        bm["totals"].extend(outs)
    return list(games.values())


# ---------------------------------------------------------------------------
# Market upsert (find existing or create new)
# ---------------------------------------------------------------------------

def _find_or_create_market(game: OwlsGame, aliases: dict[str, str]) -> str | None:
    """Return a markets.id for this game. Create if no existing row matches."""
    existing = db.list_active_markets(game.sport)
    venue_key = matcher._teams_key(game.home, game.away, aliases)
    for row in existing:
        try:
            row_start = _parse_ts(row["event_start"])
        except Exception:
            continue
        if row_start is None or abs(row_start - game.commence_time) > MATCH_WINDOW:
            continue
        row_split = matcher._split_event_name(row["event_name"])
        if not all(row_split):
            continue
        row_home, row_away = row_split[1], row_split[0]  # "Away @ Home"
        if matcher._teams_key(row_home, row_away, aliases) == venue_key:
            return row["id"]
        score = matcher._fuzzy_teams_match(game.home, game.away, row_home, row_away, aliases)
        if score >= matcher.FUZZY_THRESHOLD:
            return row["id"]

    # No match — create
    m = Market(
        sport=game.sport,
        event_name=f"{game.away} @ {game.home}",
        event_start=game.commence_time,
        status="active",
    )
    try:
        row = db.upsert_market(m)
        return row.get("id")
    except Exception as e:
        log.warning("create market failed for %s: %s", game.event_id, e)
        return None


# ---------------------------------------------------------------------------
# Snapshot building
# ---------------------------------------------------------------------------

def _side_for_h2h(outcome_name: str, home: str, away: str) -> str | None:
    n = (outcome_name or "").strip().lower()
    if n == home.strip().lower():
        return "home"
    if n == away.strip().lower():
        return "away"
    return None


def _side_for_total(outcome_name: str) -> str | None:
    n = (outcome_name or "").strip().lower()
    if n == "over":
        return "over"
    if n == "under":
        return "under"
    return None


def _safe_int(v: Any) -> int | None:
    try:
        return int(round(float(v)))
    except (TypeError, ValueError):
        return None


def _safe_float(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def build_snapshots(game: OwlsGame, market_id: str) -> list[BookSnapshot]:
    out: list[BookSnapshot] = []
    for book, mk in game.book_markets.items():
        # Moneyline
        for o in mk.get("h2h", []):
            side = _side_for_h2h(o.get("name"), game.home, game.away)
            price = _safe_int(o.get("price"))
            if side is None or price is None:
                continue
            out.append(BookSnapshot(
                market_id=market_id, book=book, market_type="moneyline",
                side=side, price_american=price,
                implied_prob=american_to_prob(price),
            ))
        # Spread
        for o in mk.get("spreads", []):
            side = _side_for_h2h(o.get("name"), game.home, game.away)
            price = _safe_int(o.get("price"))
            point = _safe_float(o.get("point"))
            if side is None or price is None:
                continue
            out.append(BookSnapshot(
                market_id=market_id, book=book, market_type="spread",
                side=side, line=point, price_american=price,
                implied_prob=american_to_prob(price),
            ))
        # Total
        for o in mk.get("totals", []):
            side = _side_for_total(o.get("name"))
            price = _safe_int(o.get("price"))
            point = _safe_float(o.get("point"))
            if side is None or price is None:
                continue
            out.append(BookSnapshot(
                market_id=market_id, book=book, market_type="total",
                side=side, line=point, price_american=price,
                implied_prob=american_to_prob(price),
            ))
    return out


# ---------------------------------------------------------------------------
# Top-level ingest
# ---------------------------------------------------------------------------

def _latest_snapshot_map(
    market_ids: list[str], within_minutes: int = 30
) -> dict[tuple[str, str, str, str], tuple[int, float | None]]:
    """Fetch recent snapshots for these markets and build a lookup of the most
    recent (price_american, line) per (market_id, book, market_type, side).

    Used to suppress no-op inserts when the price hasn't moved since the last
    cron cycle — cuts ~60-75% of writes in a typical 5-min interval.
    """
    if not market_ids:
        return {}
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=within_minutes)).isoformat()
    # Supabase PostgREST allows `in.(v1,v2,...)` filtering. Chunk to avoid URL
    # length issues with large market lists.
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
            .limit(10000)
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
    """Return only snapshots whose price or line changed vs. the latest known."""
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


def ingest_sport(sport: str) -> dict[str, int]:
    """Fetch + parse + persist one sport. Returns counts dict."""
    counts = {"games": 0, "matched": 0, "created": 0, "candidate": 0, "snapshots": 0, "deduped": 0}
    raw = fetch_odds(sport)
    if not raw:
        return counts
    games = parse_games(sport, raw)
    counts["games"] = len(games)
    if not games:
        log.info("Owls %s: 0 games parsed", sport)
        return counts

    aliases = db.list_team_aliases(sport)
    existing_count = len(db.list_active_markets(sport))

    all_snaps: list[BookSnapshot] = []
    market_ids: list[str] = []
    for g in games:
        mid = _find_or_create_market(g, aliases)
        if not mid:
            continue
        market_ids.append(mid)
        all_snaps.extend(build_snapshots(g, mid))

    # Post-ingest: count how many created this run
    counts["created"] = max(0, len(db.list_active_markets(sport)) - existing_count)
    counts["matched"] = counts["games"] - counts["created"]
    counts["candidate"] = len(all_snaps)

    # Only persist snapshots whose price/line differs from the last recorded
    # value for the same (market, book, market_type, side).
    latest = _latest_snapshot_map(market_ids)
    to_write = _dedup_unchanged(all_snaps, latest)
    counts["deduped"] = len(all_snaps) - len(to_write)

    if to_write:
        try:
            db.insert_book_snapshots(to_write)
            counts["snapshots"] = len(to_write)
        except Exception as e:
            log.exception("insert_book_snapshots(%s) failed: %s", sport, e)

    log.info(
        "Owls %s: %d games, %d matched, %d created, %d candidate, %d dedup'd, %d written",
        sport, counts["games"], counts["matched"], counts["created"],
        counts["candidate"], counts["deduped"], counts["snapshots"],
    )
    return counts


def ingest_all() -> None:
    for sport in config.sports_enabled:
        if sport not in SPORT_PATHS:
            log.debug("skip sport %s (no Owls mapping)", sport)
            continue
        try:
            ingest_sport(sport)
        except Exception as e:
            log.exception("Owls ingest %s crashed: %s", sport, e)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(prog="owls")
    p.add_argument("--sport", help="Single sport (e.g. MLB). Default: all SPORTS_ENABLED.")
    args = p.parse_args(argv)
    if args.sport:
        ingest_sport(args.sport.upper())
    else:
        ingest_all()
    return 0


if __name__ == "__main__":
    sys.exit(main())
