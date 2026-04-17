"""FanDuel scraper (M6).

Hits FanDuel's public content-managed-page endpoint (same call their
sportsbook frontend makes). Response groups attachments into three maps:

    attachments.events  : { eventId -> {name, openDate, markets[ids...]} }
    attachments.markets : { marketId -> {marketType, eventId, runners[ids]} }
    attachments.runners : { runnerId -> {runnerName, winRunnerOdds, handicap} }

For each event:
  1. Build a VenueEvent and ensure_link() -> markets_row_id.
  2. For each market belonging to that event, classify to ML/spread/total.
  3. For each runner in that market, parse americanOdds + handicap, map side
     (home/away/over/under) and append a BookSnapshot tagged book='FD'.

NOTES:
- FanDuel TLS-fingerprints. Plain httpx works most of the time but may 403
  intermittently. If you see consistent 403s, swap _http() to use
  `curl_cffi.requests` with impersonate='chrome120'.
- State subdomain matters — FD regionalises sbapi.{state}.sportsbook.fanduel.com.
  Default here is 'az' (Arizona). Override via FD_STATE env var if needed.

CLI:
  python -m scrapers.fanduel scrape NFL
  python -m scrapers.fanduel fetch-raw NFL > fd_nfl.json
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx

from config import config
from signals import matcher
from signals.normalize import american_to_prob
from storage import supabase_client as db
from storage.models import BookSnapshot

log = logging.getLogger(__name__)

FD_STATE = os.getenv("FD_STATE", "az")
BASE = f"https://sbapi.{FD_STATE}.sportsbook.fanduel.com/api/content-managed-page"

# FanDuel sport keys.
FD_SPORTS: dict[str, str] = {
    "NFL":   "FOOTBALL",
    "NBA":   "BASKETBALL",
    "MLB":   "BASEBALL",
    "NHL":   "ICE-HOCKEY",
    "CBB":   "BASKETBALL",   # filtered by league; approximate
    "NCAAF": "FOOTBALL",
    "UFC":   "MMA",
}

# Market-type classifiers. FD's market types vary by sport; we match on
# substrings to stay robust.
def _classify_market_type(mtype: str | None, mname: str | None) -> str | None:
    t = (mtype or "").upper()
    n = (mname or "").lower()
    if "MATCH_ODDS" in t or t == "MONEYLINE" or "moneyline" in n:
        return "moneyline"
    if "HANDICAP" in t or "SPREAD" in t or "RUN_LINE" in t or "PUCK_LINE" in t or "spread" in n or "run line" in n:
        return "spread"
    if "TOTAL" in t or "OVER_UNDER" in t or "total" in n or "over/under" in n:
        return "total"
    return None


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def _http() -> httpx.Client:
    return httpx.Client(
        headers={
            "User-Agent": config.fd_user_agent,
            "Accept": "application/json",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": f"https://sportsbook.fanduel.com/",
            "Origin":  f"https://sportsbook.fanduel.com",
            "X-Px-Context": "",
        },
        timeout=15.0,
        follow_redirects=True,
    )


def fetch_sport(sport: str) -> dict[str, Any] | None:
    fd_sport = FD_SPORTS.get(sport)
    if not fd_sport:
        log.warning("no FD sport key for %s", sport)
        return None
    params = {
        "page":     "SPORT",
        "sport":    fd_sport,
        "pageType": "SPORT",
        "_ak":      "FhMFpcPWXMeyZxOx",
    }
    with _http() as http:
        try:
            r = http.get(BASE, params=params)
            if r.status_code != 200:
                log.warning("FD %s -> %s %s", sport, r.status_code, r.text[:200])
                return None
            return r.json()
        except Exception as e:
            log.warning("FD %s exception: %s", sport, e)
            return None


# ---------------------------------------------------------------------------
# Parse
# ---------------------------------------------------------------------------

@dataclass
class FDEvent:
    event_id: str
    name: str
    home: str
    away: str
    start: datetime
    market_ids: list[str]
    raw: dict[str, Any]


def _parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def _safe_int(v: Any) -> int | None:
    try:
        if isinstance(v, str):
            v = v.replace("+", "").replace("−", "-")
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _safe_float(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _split_event_name(name: str) -> tuple[str, str]:
    for sep in (" @ ", " at ", " vs ", " v "):
        if sep in name:
            a, b = name.split(sep, 1)
            if sep in (" @ ", " at "):
                return b.strip(), a.strip()   # (home, away) given "Away @ Home"
            return a.strip(), b.strip()
    return "", ""


def _events(payload: dict[str, Any]) -> list[FDEvent]:
    att = payload.get("attachments") or {}
    events = att.get("events") or {}
    out: list[FDEvent] = []
    for eid, ev in events.items():
        name = ev.get("name") or ""
        start = _parse_ts(ev.get("openDate") or ev.get("startTime"))
        home, away = _split_event_name(name)
        if not start or not home or not away:
            continue
        market_ids = [str(mid) for mid in (ev.get("markets") or [])]
        out.append(FDEvent(str(eid), name, home, away, start, market_ids, ev))
    return out


def _snapshots(
    payload: dict[str, Any],
    event_id_to_market: dict[str, str],
    home_name: dict[str, str],
) -> list[BookSnapshot]:
    att = payload.get("attachments") or {}
    markets = att.get("markets") or {}
    runners = att.get("runners") or {}
    out: list[BookSnapshot] = []

    for mid, mkt in markets.items():
        ev_id = str(mkt.get("eventId") or "")
        market_row_id = event_id_to_market.get(ev_id)
        if not market_row_id:
            continue
        mt = _classify_market_type(mkt.get("marketType"), mkt.get("marketName"))
        if not mt:
            continue
        home = home_name.get(ev_id, "")
        for rid in (mkt.get("runners") or []):
            r = runners.get(str(rid)) or runners.get(rid) or {}
            if not r:
                continue
            # americanOdds can be nested: winRunnerOdds.americanOdds.americanOddsInt
            odds = (
                ((r.get("winRunnerOdds") or {}).get("americanDisplayOdds") or {}).get("americanOddsInt")
                or ((r.get("winRunnerOdds") or {}).get("americanOdds") or {}).get("americanOddsInt")
                or r.get("americanOdds")
            )
            price = _safe_int(odds)
            if price is None:
                continue
            name = (r.get("runnerName") or "").strip()
            handicap = _safe_float(r.get("handicap") or r.get("runnerHandicap"))
            side = _classify_side(mt, name, home)
            if side is None:
                continue
            out.append(BookSnapshot(
                market_id=market_row_id,
                book="FD",
                market_type=mt,
                side=side,
                line=handicap,
                price_american=price,
                implied_prob=american_to_prob(price),
            ))
    return out


def _classify_side(market_type: str, label: str, home_name: str) -> str | None:
    lab = (label or "").strip().lower()
    if market_type == "total":
        if lab.startswith("o") or "over" in lab:
            return "over"
        if lab.startswith("u") or "under" in lab:
            return "under"
        return None
    hn = home_name.strip().lower()
    if hn and (hn in lab or lab in hn):
        return "home"
    return "away"


# ---------------------------------------------------------------------------
# Top-level scrape
# ---------------------------------------------------------------------------

def scrape(sport: str) -> int:
    payload = fetch_sport(sport)
    if not payload:
        return 0
    events = _events(payload)
    if not events:
        log.warning("FD %s: 0 events parsed (endpoint shape may have changed)", sport)
        return 0

    event_id_to_market: dict[str, str] = {}
    home_name: dict[str, str] = {}
    for ev in events:
        venue = matcher.VenueEvent(
            source="fd",
            source_id=ev.event_id,
            sport=sport,
            home=ev.home,
            away=ev.away,
            event_start=ev.start,
            raw={"fd_event": ev.raw, "name": ev.name},
        )
        mid = matcher.ensure_link(venue)
        if mid:
            event_id_to_market[ev.event_id] = mid
            home_name[ev.event_id] = ev.home

    snaps = _snapshots(payload, event_id_to_market, home_name)
    if snaps:
        try:
            db.insert_book_snapshots(snaps)
        except Exception as e:
            log.warning("insert_book_snapshots(FD,%s) failed: %s", sport, e)
            return 0
    log.info(
        "FD %s: %d events, %d matched, %d snapshots",
        sport, len(events), len(event_id_to_market), len(snaps),
    )
    return len(snaps)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(prog="fanduel")
    sub = p.add_subparsers(dest="cmd", required=True)

    sc = sub.add_parser("scrape", help="Scrape one sport and write snapshots")
    sc.add_argument("sport", help="NFL, NBA, MLB, NHL, CBB, NCAAF, UFC")

    raw = sub.add_parser("fetch-raw", help="Dump raw FD JSON to stdout")
    raw.add_argument("sport")

    args = p.parse_args(argv)
    if args.cmd == "scrape":
        n = scrape(args.sport.upper())
        log.info("scraped %d snapshots", n)
    elif args.cmd == "fetch-raw":
        payload = fetch_sport(args.sport.upper())
        if not payload:
            log.error("fetch failed")
            return 1
        print(json.dumps(payload, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
