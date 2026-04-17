"""Discover upcoming games from Polymarket's public gamma API and seed them
into the scanner's markets table with correct home/away orientation
(cross-referenced against ESPN).

Gamma is unauthenticated — no API key needed. We use it here rather than
the polymarket-us SDK because gamma exposes a `/markets` search that the
authenticated SDK surface doesn't.

Flow:
  1. Pull every active MLB/NBA/NHL game from ESPN over the next N days.
     ESPN tells us authoritatively who's home and who's away.
  2. Hit gamma-api.polymarket.com/events?tag_slug=<sport> for the same
     window. Each event contains several child markets (moneyline, total,
     alt lines).
  3. Pick each event's moneyline market (the one whose outcomes are the
     two team names).
  4. Fuzzy-match the Poly event to an ESPN game by team names + date.
     This gives us definitive home/away + lets us pick home_side for the
     Poly binary: if the YES outcome is the home team, home_side='yes'.
  5. Upsert into markets with sport/event_name/event_start/poly_market_id
     and notes={'poly_home_side': ..., 'auto_seeded': True,
                'discovered_via': 'gamma+espn'}.

Skipped markets are logged to unmatched_markets so you can audit what
didn't link.
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from rapidfuzz import fuzz

from storage import supabase_client as db
from storage.models import Market

log = logging.getLogger(__name__)

GAMMA_BASE = "https://gamma-api.polymarket.com"
ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/scoreboard"

ESPN_SPORT: dict[str, tuple[str, str]] = {
    "MLB": ("baseball", "mlb"),
    "NBA": ("basketball", "nba"),
    "NHL": ("hockey", "nhl"),
    "NFL": ("football", "nfl"),
}

# Tag slugs Polymarket uses on gamma. Confirmed against polymarket.com browse URLs.
GAMMA_TAG: dict[str, str] = {
    "MLB": "mlb",
    "NBA": "nba",
    "NHL": "nhl",
    "NFL": "nfl",
}


# ---------------------------------------------------------------------------
# ESPN upcoming games
# ---------------------------------------------------------------------------

@dataclass
class ESPNGame:
    sport: str
    home: str
    away: str
    start: datetime
    event_id: str


def _http() -> httpx.Client:
    return httpx.Client(
        headers={
            "User-Agent": "kahla-scanner/1.0 (+https://thekahlahouse.com)",
            "Accept": "application/json",
        },
        timeout=20.0,
    )


def _fetch_espn_day(sport: str, date: datetime) -> list[ESPNGame]:
    pair = ESPN_SPORT.get(sport)
    if not pair:
        return []
    url = ESPN_BASE.format(sport=pair[0], league=pair[1])
    params = {"dates": date.strftime("%Y%m%d")}
    out: list[ESPNGame] = []
    with _http() as h:
        try:
            r = h.get(url, params=params)
            if r.status_code != 200:
                return []
            events = (r.json() or {}).get("events") or []
        except Exception as e:
            log.warning("ESPN %s %s: %s", sport, date.date(), e)
            return []
    for ev in events:
        comps = ev.get("competitions") or []
        if not comps:
            continue
        c = comps[0]
        competitors = c.get("competitors") or []
        home_c = next((x for x in competitors if (x.get("homeAway") or "").lower() == "home"), None)
        away_c = next((x for x in competitors if (x.get("homeAway") or "").lower() == "away"), None)
        if not home_c or not away_c:
            continue
        home = ((home_c.get("team") or {}).get("displayName") or "").strip()
        away = ((away_c.get("team") or {}).get("displayName") or "").strip()
        start_raw = ev.get("date") or c.get("date")
        if not home or not away or not start_raw:
            continue
        try:
            start = datetime.fromisoformat(start_raw.replace("Z", "+00:00"))
        except Exception:
            continue
        out.append(ESPNGame(
            sport=sport,
            home=home,
            away=away,
            start=start,
            event_id=str(ev.get("id") or ""),
        ))
    return out


def fetch_espn_window(sport: str, days_ahead: int = 3) -> list[ESPNGame]:
    """Today through today+days_ahead in UTC."""
    today = datetime.now(timezone.utc)
    games: list[ESPNGame] = []
    for offset in range(days_ahead + 1):
        games.extend(_fetch_espn_day(sport, today + timedelta(days=offset)))
    return games


# ---------------------------------------------------------------------------
# Polymarket gamma
# ---------------------------------------------------------------------------

def fetch_gamma_events(
    sport: str, days_ahead: int = 3, limit: int = 2000
) -> list[dict[str, Any]]:
    """Fetch per-GAME events from gamma.

    Gamma's /events?tag_slug=<sport> is polluted with season-long futures
    (MVP, Champion, CBA). Those resolve months out; individual game markets
    resolve within hours. So we hit /markets with the tag + endDate <=
    now + days_ahead, then group the results by eventSlug into synthetic
    event dicts.
    """
    tag = GAMMA_TAG.get(sport)
    if not tag:
        log.warning("no gamma tag for %s", sport)
        return []

    now = datetime.now(timezone.utc)
    end_max = now + timedelta(days=days_ahead + 1)
    end_max_iso = end_max.isoformat()

    markets: list[dict[str, Any]] = []
    offset = 0
    page_size = 100
    with _http() as h:
        while offset < limit:
            params = {
                "tag_slug":      tag,
                "active":        "true",
                "closed":        "false",
                "archived":      "false",
                "order":         "endDate",
                "ascending":     "true",
                "limit":         page_size,
                "offset":        offset,
                # Server-side end-date filter; harmless if gamma ignores it.
                "end_date_max":  end_max_iso,
                "endDateMax":    end_max_iso,
            }
            try:
                r = h.get(f"{GAMMA_BASE}/markets", params=params)
                if r.status_code != 200:
                    log.warning("gamma %s: %s %s", sport, r.status_code, r.text[:200])
                    break
                page = r.json() or []
            except Exception as e:
                log.warning("gamma %s exception: %s", sport, e)
                break
            if not page:
                break
            markets.extend(page)
            if len(page) < page_size:
                break
            offset += page_size

    log.info("gamma %s: fetched %d raw markets", sport, len(markets))

    # Client-side filter by endDate (server filter may be ignored depending
    # on gamma version). Keep markets ending between now-6h and now+days_ahead.
    games: list[dict[str, Any]] = []
    for m in markets:
        ed = _parse_ts(m.get("endDate") or m.get("end_date"))
        if not ed:
            continue
        if ed < now - timedelta(hours=6):
            continue
        if ed > end_max:
            continue
        games.append(m)
    log.info("gamma %s: %d markets in date window", sport, len(games))

    if games:
        sample_keys = {k: v for k, v in games[0].items()
                       if k in ("id", "slug", "question", "startDate", "endDate",
                                "eventSlug", "groupItemTitle", "outcomes")}
        log.info("gamma %s: sample market -> %s", sport, sample_keys)

    # Group by eventSlug into synthetic events the rest of discover_sport
    # already knows how to consume (each event has a 'markets' list and a
    # 'title'/'startDate' drawn from the ML market).
    by_event: dict[str, dict[str, Any]] = {}
    for m in games:
        event_key = m.get("eventSlug") or m.get("event_slug") or m.get("slug")
        if not event_key:
            continue
        bucket = by_event.setdefault(event_key, {
            "id":        m.get("eventId") or m.get("event_id") or event_key,
            "slug":      event_key,
            "title":     "",
            "startDate": None,
            "markets":   [],
        })
        bucket["markets"].append(m)

    # Pick a 'primary' market per event to set event-level title/startDate.
    synthetic: list[dict[str, Any]] = []
    for event_key, ev in by_event.items():
        ml = _extract_ml_market(ev)
        if ml:
            ev["title"] = (
                ml.get("groupItemTitle")
                or ml.get("question")
                or event_key.replace("-", " ")
            )
            ev["startDate"] = (
                ml.get("startDate") or ml.get("start_date")
                or ml.get("gameStartTime")
            )
        else:
            # Fall back to the first market in the group
            first = ev["markets"][0]
            ev["title"] = first.get("groupItemTitle") or first.get("question") or event_key
            ev["startDate"] = first.get("startDate") or first.get("gameStartTime")
        synthetic.append(ev)

    log.info("gamma %s: %d synthetic events (grouped by eventSlug)",
             sport, len(synthetic))
    return synthetic


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

_PARENS_RE = re.compile(r"\s*\([^)]*\)\s*")


def _norm(s: str) -> str:
    s = _PARENS_RE.sub(" ", s or "")
    s = re.sub(r"[^\w\s]", " ", s.lower())
    return re.sub(r"\s+", " ", s).strip()


def _extract_ml_market(event: dict[str, Any]) -> dict[str, Any] | None:
    """Pick the moneyline market from a gamma event.

    Strategy: find the market whose outcomes are the two team names (simple
    binary), NOT a spread/total. Heuristics:
      - question does not contain 'over', 'under', 'total', spread numbers
      - groupItemTitle OR outcomes look like team names (alpha-only-ish)
    """
    markets = event.get("markets") or []
    candidates: list[tuple[int, dict[str, Any]]] = []
    for m in markets:
        if m.get("closed") or not m.get("active", True):
            continue
        q = (m.get("question") or "").lower()
        if any(k in q for k in ("over", "under", "total", "o/u", "more than", "less than")):
            continue
        # Avoid spread markets (contain signed numbers like "-1.5" or "+1.5")
        if re.search(r"[+\-−]\s?\d+(\.\d+)?", q):
            continue
        # Score: prefer markets with short questions and simple "will X win / X vs Y"
        score = 0
        if "will " in q and " win" in q:
            score += 5
        if " vs " in q or " @ " in q:
            score += 3
        if "moneyline" in q or "ml" in q.split():
            score += 5
        candidates.append((score, m))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]


def _match_espn_game(
    title: str, start: datetime | None, espn_games: list[ESPNGame]
) -> ESPNGame | None:
    """Fuzzy-match a gamma event title to an ESPN game."""
    if not espn_games:
        return None
    title_norm = _norm(title)
    best: tuple[int, ESPNGame | None] = (0, None)
    for g in espn_games:
        if start:
            dt = abs((g.start - start).total_seconds())
            if dt > 6 * 3600:   # >6h apart — not the same game
                continue
        hay = f"{_norm(g.away)} {_norm(g.home)}"
        hay_rev = f"{_norm(g.home)} {_norm(g.away)}"
        score = max(
            fuzz.partial_ratio(title_norm, hay),
            fuzz.partial_ratio(title_norm, hay_rev),
            fuzz.token_set_ratio(title_norm, hay),
        )
        if score > best[0]:
            best = (score, g)
    if best[0] >= 65:
        return best[1]
    return None


def _infer_home_side(market: dict[str, Any], espn: ESPNGame) -> str:
    """Given a Poly binary market + ESPN home/away, decide which Poly token
    represents the home team winning.

    Returns 'yes' if YES outcome = home team winning, else 'no'.
    """
    q = (market.get("question") or "").lower()
    home_norm = _norm(espn.home)
    away_norm = _norm(espn.away)

    # If the question names a specific team ("Will Yankees beat Red Sox?"),
    # the YES outcome corresponds to that team winning.
    home_hit = fuzz.partial_ratio(q, home_norm)
    away_hit = fuzz.partial_ratio(q, away_norm)
    if home_hit >= 80 and home_hit > away_hit + 5:
        return "yes"
    if away_hit >= 80 and away_hit > home_hit + 5:
        return "no"

    # Fall back to outcomes/groupItemTitle
    git = (market.get("groupItemTitle") or "").lower()
    if git:
        if fuzz.partial_ratio(git, home_norm) >= 80:
            return "yes"
        if fuzz.partial_ratio(git, away_norm) >= 80:
            return "no"

    outcomes_raw = market.get("outcomes")
    if isinstance(outcomes_raw, str):
        try:
            outcomes = json.loads(outcomes_raw)
        except Exception:
            outcomes = []
    else:
        outcomes = outcomes_raw or []
    if outcomes and isinstance(outcomes, list) and len(outcomes) >= 1:
        first = str(outcomes[0]).lower()
        if fuzz.partial_ratio(first, home_norm) >= 80:
            return "yes"
        if fuzz.partial_ratio(first, away_norm) >= 80:
            return "no"

    # Conservative default
    return "yes"


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def _parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception:
        return None


def discover_sport(sport: str, days_ahead: int = 3) -> dict[str, int]:
    """Discover + seed markets for one sport. Returns counts."""
    counts = {
        "gamma_events": 0, "matched": 0, "seeded": 0,
        "skipped_existing": 0, "skipped_no_ml": 0, "skipped_no_match": 0,
        "failed": 0,
    }
    espn_games = fetch_espn_window(sport, days_ahead=days_ahead)
    log.info("ESPN %s upcoming games: %d", sport, len(espn_games))

    gamma_events = fetch_gamma_events(sport, days_ahead=days_ahead)
    counts["gamma_events"] = len(gamma_events)
    log.info("gamma %s events: %d", sport, len(gamma_events))
    if not gamma_events:
        return counts

    # Build lookup of already-seeded markets (to skip).
    existing = {m["poly_market_id"] for m in db.list_active_markets(sport)}
    existing |= {m["poly_market_id"] for m in db.list_active_markets() if m.get("poly_market_id")}

    sample_skipped: list[str] = []
    for ev in gamma_events:
        title = ev.get("title") or ev.get("slug") or ""
        start = _parse_ts(ev.get("startDate") or ev.get("start_date"))
        if not start:
            markets = ev.get("markets") or []
            if markets:
                start = _parse_ts(markets[0].get("startDate") or markets[0].get("start_date"))
        if not start:
            continue

        ml = _extract_ml_market(ev)
        if not ml:
            counts["skipped_no_ml"] += 1
            if len(sample_skipped) < 3:
                sample_skipped.append(f"no-ml: {title[:80]}")
            continue
        slug = ml.get("slug")
        if not slug:
            counts["skipped_no_ml"] += 1
            continue
        if slug in existing:
            counts["skipped_existing"] += 1
            continue

        espn_game = _match_espn_game(title, start, espn_games)
        if not espn_game:
            counts["skipped_no_match"] += 1
            if len(sample_skipped) < 3:
                sample_skipped.append(f"no-espn: {title[:80]}")
            # Only log to unmatched_markets if the title LOOKS like a game
            # (contains 'vs', '@', or 'at' joining two alpha tokens). Futures
            # markets like 'NBA MVP' or 'World Series Champion' shouldn't
            # become noise in the review queue.
            if re.search(r"\b(vs\.?|@|at)\b", title, re.IGNORECASE):
                db.log_unmatched(
                    "gamma", slug, sport=sport,
                    event_name=title, event_start=start,
                    payload={"gamma_event_id": ev.get("id"),
                             "ml_question": ml.get("question")},
                )
            continue

        counts["matched"] += 1
        home_side = _infer_home_side(ml, espn_game)
        event_name = f"{espn_game.away} @ {espn_game.home}"

        try:
            m = Market(
                sport=sport,
                event_name=event_name,
                event_start=espn_game.start,    # prefer ESPN's authoritative time
                poly_market_id=slug,
            )
            row = db.upsert_market(m)
            db.client().table("markets").update(
                {"notes": {
                    "poly_home_side":  home_side,
                    "auto_seeded":     True,
                    "discovered_via":  "gamma+espn",
                    "gamma_event_id":  str(ev.get("id") or ""),
                    "ml_question":     ml.get("question"),
                    "original_title":  title,
                }}
            ).eq("id", row["id"]).execute()
            counts["seeded"] += 1
            log.info("seeded %s (%s) home_side=%s", slug, event_name, home_side)
            existing.add(slug)
        except Exception as e:
            counts["failed"] += 1
            log.warning("upsert_market(%s) failed: %s", slug, e)

    log.info("discover %s: %s", sport, counts)
    if sample_skipped:
        log.info("discover %s sample skipped: %s", sport, sample_skipped)
    return counts


def discover_sports(sports: list[str], days_ahead: int = 3) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for sport in sports:
        try:
            out[sport] = discover_sport(sport.upper(), days_ahead=days_ahead)
        except Exception as e:
            log.exception("discover %s failed: %s", sport, e)
            out[sport] = {"failed": 1}
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    p = argparse.ArgumentParser(prog="discover")
    p.add_argument(
        "--sports", default="MLB,NBA,NHL",
        help="Comma-separated sport keys (default: MLB,NBA,NHL)",
    )
    p.add_argument("--days", type=int, default=3, help="Days ahead (default 3)")
    args = p.parse_args(argv)
    sports = [s.strip().upper() for s in args.sports.split(",") if s.strip()]
    out = discover_sports(sports, days_ahead=args.days)
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
