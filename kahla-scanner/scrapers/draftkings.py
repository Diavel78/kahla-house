"""DraftKings scraper (M2).

Hits the same public JSON endpoints sportsbook.draftkings.com uses. The
response shape is subject to change — this scraper tolerates BOTH the
older `eventGroups` shape AND the newer `sportscontent/leagues/{id}` shape,
falling back gracefully.

For each event in the response:
  1. Build a VenueEvent (source='dk', team names + start time).
  2. ensure_link() to match it to a markets row (creates linkage on first
     match, logs to unmatched_markets on failure).
  3. Parse moneyline / spread / total selections for that event.
  4. Insert BookSnapshot rows tagged book='DK'.

Sports + DK league/eventGroup IDs:
  NFL: 88808, NBA: 42648, MLB: 84240, NHL: 42133, CBB: 92483,
  NCAAF: 87637, UFC: 9034

CLI:
  python -m scrapers.draftkings scrape NFL
  python -m scrapers.draftkings fetch-raw NFL > dk_nfl.json
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable

import httpx

from config import config
from signals import matcher
from signals.normalize import american_to_prob
from storage import supabase_client as db
from storage.models import BookSnapshot

log = logging.getLogger(__name__)

# Current-gen endpoint (sportscontent API, JSON with simpler shape)
URL_SPORTSCONTENT = (
    "https://sportsbook-nash.draftkings.com/api/sportscontent/dkusnj/v1/leagues/{gid}"
)
# Legacy endpoint (eventGroups, older shape) — used as a fallback
URL_LEGACY = (
    "https://sportsbook.draftkings.com/sites/US-SB/api/v5/eventgroups/{gid}?format=json"
)

SPORT_EVENT_GROUPS: dict[str, int] = {
    "NFL":   88808,
    "NBA":   42648,
    "MLB":   84240,
    "NHL":   42133,
    "CBB":   92483,
    "NCAAF": 87637,
    "UFC":   9034,
}

# Selection label variants we'll recognise for moneyline / spread / total.
ML_LABELS      = {"moneyline", "money line", "ml"}
SPREAD_LABELS  = {"spread", "point spread", "run line", "runline", "puck line", "puckline"}
TOTAL_LABELS   = {"total", "total points", "totals", "over/under", "total runs", "total goals"}


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def _http() -> httpx.Client:
    return httpx.Client(
        headers={
            "User-Agent": config.dk_user_agent,
            "Accept": "application/json",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://sportsbook.draftkings.com/",
        },
        timeout=15.0,
        follow_redirects=True,
    )


def fetch_group(sport: str) -> tuple[str, dict[str, Any]] | None:
    """Returns (shape, payload) where shape is 'sportscontent' or 'legacy'."""
    gid = SPORT_EVENT_GROUPS.get(sport)
    if not gid:
        log.warning("no DK event group for sport %s", sport)
        return None
    with _http() as http:
        for shape, url in [
            ("sportscontent", URL_SPORTSCONTENT.format(gid=gid)),
            ("legacy",        URL_LEGACY.format(gid=gid)),
        ]:
            try:
                r = http.get(url)
                if r.status_code == 200:
                    return shape, r.json()
                log.warning("DK %s %s -> %s", sport, shape, r.status_code)
            except Exception as e:
                log.warning("DK %s %s exception: %s", sport, shape, e)
    return None


# ---------------------------------------------------------------------------
# Shape parsers
# ---------------------------------------------------------------------------

@dataclass
class DKEvent:
    event_id: str
    name: str
    home: str
    away: str
    start: datetime
    raw: dict[str, Any]


def _parse_ts(s: str) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def _norm_label(s: Any) -> str:
    return (s or "").strip().lower()


def _classify_market(label: str) -> str | None:
    lab = _norm_label(label)
    if lab in ML_LABELS or lab.startswith("moneyline"):
        return "moneyline"
    if lab in SPREAD_LABELS or "spread" in lab or lab.endswith("line"):
        return "spread"
    if lab in TOTAL_LABELS or lab.startswith("total") or "over/under" in lab:
        return "total"
    return None


def _safe_int(v: Any) -> int | None:
    if v is None:
        return None
    try:
        if isinstance(v, str):
            v = v.replace("+", "").replace("−", "-")
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _safe_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ---- sportscontent shape --------------------------------------------------

def _events_sportscontent(payload: dict[str, Any]) -> list[DKEvent]:
    out: list[DKEvent] = []
    for ev in (payload.get("events") or []):
        ev_id = str(ev.get("id") or ev.get("eventId") or "")
        name = ev.get("name") or ""
        start = _parse_ts(ev.get("startEventDate") or ev.get("startDate") or "")
        teams = ev.get("participants") or []
        home = away = ""
        for t in teams:
            ha = (t.get("homeAway") or "").lower()
            tname = t.get("name") or (t.get("team") or {}).get("name") or ""
            if ha == "home":
                home = tname
            elif ha == "away":
                away = tname
        if not home or not away:
            # fall back: split "Away @ Home" from the name
            for sep in (" @ ", " vs ", " at "):
                if sep in name:
                    a, b = name.split(sep, 1)
                    if sep == " @ " or sep == " at ":
                        away, home = a.strip(), b.strip()
                    else:
                        home, away = a.strip(), b.strip()
                    break
        if not ev_id or not start or not home or not away:
            continue
        out.append(DKEvent(ev_id, name, home, away, start, ev))
    return out


def _snapshots_sportscontent(
    payload: dict[str, Any], event_id_to_market: dict[str, str], home_name: dict[str, str]
) -> list[BookSnapshot]:
    """Parse offers from the sportscontent shape into BookSnapshot rows."""
    out: list[BookSnapshot] = []
    for m in (payload.get("markets") or []):
        mkt_type = _classify_market(
            ((m.get("marketType") or {}).get("name")) or m.get("name") or ""
        )
        if not mkt_type:
            continue
        ev_id = str(m.get("eventId") or "")
        market_id = event_id_to_market.get(ev_id)
        if not market_id:
            continue
        home = home_name.get(ev_id, "")
        for sel in (m.get("selections") or m.get("outcomes") or []):
            odds = sel.get("displayOdds") or {}
            price = _safe_int(odds.get("american") or sel.get("oddsAmerican"))
            if price is None:
                continue
            label = sel.get("label") or sel.get("name") or ""
            points = _safe_float(sel.get("points") or sel.get("line"))
            side = _classify_side(mkt_type, label, home)
            if side is None:
                continue
            out.append(_snap(market_id, mkt_type, side, price, points))
    return out


# ---- legacy shape ---------------------------------------------------------

def _events_legacy(payload: dict[str, Any]) -> list[DKEvent]:
    out: list[DKEvent] = []
    eg = payload.get("eventGroup") or {}
    for ev in (eg.get("events") or []):
        ev_id = str(ev.get("eventId") or "")
        name = ev.get("name") or ""
        home = ev.get("teamName1") or ""
        away = ev.get("teamName2") or ""
        start = _parse_ts(ev.get("startDate") or "")
        if not ev_id or not start or not home or not away:
            continue
        out.append(DKEvent(ev_id, name, home, away, start, ev))
    return out


def _snapshots_legacy(
    payload: dict[str, Any], event_id_to_market: dict[str, str], home_name: dict[str, str]
) -> list[BookSnapshot]:
    out: list[BookSnapshot] = []
    eg = payload.get("eventGroup") or {}
    for cat in (eg.get("offerCategories") or []):
        for sub in (cat.get("offerSubcategoryDescriptors") or []):
            osub = sub.get("offerSubcategory") or {}
            for offer_group in (osub.get("offers") or []):
                for offer in offer_group:
                    label = offer.get("label") or ""
                    mkt_type = _classify_market(label)
                    if not mkt_type:
                        continue
                    ev_id = str(offer.get("eventId") or "")
                    market_id = event_id_to_market.get(ev_id)
                    if not market_id:
                        continue
                    home = home_name.get(ev_id, "")
                    for oc in (offer.get("outcomes") or []):
                        price = _safe_int(oc.get("oddsAmerican"))
                        if price is None:
                            continue
                        out_label = oc.get("label") or ""
                        points = _safe_float(oc.get("line"))
                        side = _classify_side(mkt_type, out_label, home)
                        if side is None:
                            continue
                        out.append(_snap(market_id, mkt_type, side, price, points))
    return out


# ---- side classifier (shared) --------------------------------------------

def _classify_side(market_type: str, label: str, home_name: str) -> str | None:
    lab = _norm_label(label)
    if market_type == "total":
        if lab.startswith("o") or "over" in lab:
            return "over"
        if lab.startswith("u") or "under" in lab:
            return "under"
        return None
    # moneyline / spread: label is the team name. Match to home_name leniently.
    hn = _norm_label(home_name)
    if hn and (hn in lab or lab in hn):
        return "home"
    return "away"


def _snap(market_id: str, market_type: str, side: str, price: int, line: float | None) -> BookSnapshot:
    return BookSnapshot(
        market_id=market_id,
        book="DK",
        market_type=market_type,
        side=side,
        line=line,
        price_american=price,
        implied_prob=american_to_prob(price),
    )


# ---------------------------------------------------------------------------
# Top-level scrape
# ---------------------------------------------------------------------------

def scrape(sport: str) -> int:
    """Scrape one sport. Returns number of snapshots inserted."""
    got = fetch_group(sport)
    if not got:
        return 0
    shape, payload = got

    events = _events_sportscontent(payload) if shape == "sportscontent" else _events_legacy(payload)
    if not events:
        log.warning("DK %s: parsed 0 events (shape=%s)", sport, shape)
        return 0

    # Link events to markets rows (creates market if needed via ensure_link).
    event_id_to_market: dict[str, str] = {}
    home_name: dict[str, str] = {}
    for ev in events:
        venue_ev = matcher.VenueEvent(
            source="dk",
            source_id=ev.event_id,
            sport=sport,
            home=ev.home,
            away=ev.away,
            event_start=ev.start,
            raw={"dk_event": ev.raw, "name": ev.name},
        )
        market_id = matcher.ensure_link(venue_ev)
        if market_id:
            event_id_to_market[ev.event_id] = market_id
            home_name[ev.event_id] = ev.home

    # Parse offers + insert snapshots.
    if shape == "sportscontent":
        snaps = _snapshots_sportscontent(payload, event_id_to_market, home_name)
    else:
        snaps = _snapshots_legacy(payload, event_id_to_market, home_name)

    if snaps:
        try:
            db.insert_book_snapshots(snaps)
        except Exception as e:
            log.warning("insert_book_snapshots(DK,%s) failed: %s", sport, e)
            return 0
    log.info(
        "DK %s: %d events, %d matched, %d snapshots (shape=%s)",
        sport, len(events), len(event_id_to_market), len(snaps), shape,
    )
    return len(snaps)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(prog="draftkings")
    sub = p.add_subparsers(dest="cmd", required=True)

    sc = sub.add_parser("scrape", help="Scrape one sport and write snapshots")
    sc.add_argument("sport", help="NFL, NBA, MLB, NHL, CBB, NCAAF, UFC")

    raw = sub.add_parser("fetch-raw", help="Dump raw DK JSON to stdout")
    raw.add_argument("sport")

    args = p.parse_args(argv)
    if args.cmd == "scrape":
        n = scrape(args.sport.upper())
        log.info("scraped %d snapshots", n)
    elif args.cmd == "fetch-raw":
        got = fetch_group(args.sport.upper())
        if not got:
            log.error("fetch failed")
            return 1
        shape, payload = got
        payload["_shape"] = shape
        print(json.dumps(payload, indent=2, default=str))
    return 0


# Legacy helpers kept for the scaffold contract (unused, OK to delete later)
def _parse_offers(market_id: str, offers: Iterable[dict[str, Any]]) -> Iterable[BookSnapshot]:
    return []


if __name__ == "__main__":
    sys.exit(main())
