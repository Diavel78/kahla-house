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
    sport: str, days_ahead: int = 3, limit: int = 1500
) -> list[dict[str, Any]]:
    """Fetch per-GAME markets from gamma.

    Polymarket's sport tag_slugs (mlb/nba/nhl) are season-only — they hold
    MVP, Champion, CBA, etc. Per-game markets aren't tagged with them.

    Approach: query /markets with NO tag_slug, just `end_date_max` to
    isolate short-horizon markets. Anything resolving in the next 48h is
    almost certainly a game (politics/crypto markets have longer horizons).
    Tag-agnostic results then pass through ESPN cross-reference — if a
    market's question doesn't match an ESPN game in our window, it's not
    a sport market we care about and gets silently dropped.
    """
    now = datetime.now(timezone.utc)
    end_max = now + timedelta(days=days_ahead + 1)
    end_max_iso = end_max.isoformat()
    end_min_iso = (now - timedelta(hours=12)).isoformat()

    markets: list[dict[str, Any]] = []
    offset = 0
    page_size = 100
    with _http() as h:
        while offset < limit:
            # Sort DESCENDING by endDate — gamma has thousands of stale
            # endDate markets (zombie placeholders from old political
            # questions) that come first when ascending. Descending
            # surfaces real current markets. Also narrow by end_date_min
            # to drop anything whose nominal end is already behind us.
            params = {
                "active":        "true",
                "closed":        "false",
                "archived":      "false",
                "order":         "endDate",
                "ascending":     "false",
                "limit":         page_size,
                "offset":        offset,
                "end_date_max":  end_max_iso,
                "endDateMax":    end_max_iso,
                "end_date_min":  end_min_iso,
                "endDateMin":    end_min_iso,
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

    log.info("gamma %s: fetched %d raw markets (no tag filter)", sport, len(markets))

    # ALWAYS log the first raw market's keys + values so we can see what
    # gamma actually returns, even when everything gets filtered out.
    if markets:
        first = markets[0]
        # Show all non-trivial fields, trimmed for readability.
        preview: dict[str, Any] = {}
        for k, v in first.items():
            if v is None or v == "" or v == [] or v == {}:
                continue
            if isinstance(v, str) and len(v) > 120:
                preview[k] = v[:120] + "..."
            elif isinstance(v, list) and len(v) > 4:
                preview[k] = f"[len={len(v)}] " + str(v[:3])
            else:
                preview[k] = v
        log.info("gamma %s: first raw market keys -> %s", sport, preview)

    # Client-side filter. Try multiple candidate date fields — gamma's
    # `endDate` might be a long resolution window while `gameStartTime`
    # or `startDate` is the actual game time.
    games: list[dict[str, Any]] = []
    for m in markets:
        candidates = [
            m.get("gameStartTime"),
            m.get("startDate"),
            m.get("start_date"),
            m.get("endDate"),
            m.get("end_date"),
        ]
        found: datetime | None = None
        for c in candidates:
            dt = _parse_ts(c)
            if dt and now - timedelta(hours=6) < dt < end_max:
                found = dt
                break
        if not found:
            continue
        # Stash the chosen start into the market so downstream code sees it.
        m.setdefault("_chosen_start", found.isoformat())
        games.append(m)
    log.info("gamma %s: %d markets in date window", sport, len(games))

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


_TEAM_ABBREV = {
    # minimal MLB abbreviations commonly used on Polymarket slugs
    "new york yankees": "nyy", "boston red sox": "bos", "los angeles dodgers": "lad",
    "san francisco giants": "sf",  "san diego padres": "sd",  "chicago cubs": "chc",
    "chicago white sox": "chw",    "cleveland guardians": "cle", "detroit tigers": "det",
    "houston astros": "hou",       "kansas city royals": "kc",   "los angeles angels": "laa",
    "milwaukee brewers": "mil",    "minnesota twins": "min",     "oakland athletics": "oak",
    "athletics": "oak",            "seattle mariners": "sea",    "tampa bay rays": "tb",
    "texas rangers": "tex",        "toronto blue jays": "tor",   "atlanta braves": "atl",
    "miami marlins": "mia",        "new york mets": "nym",       "philadelphia phillies": "phi",
    "washington nationals": "wsh", "arizona diamondbacks": "ari","colorado rockies": "col",
    "cincinnati reds": "cin",      "pittsburgh pirates": "pit",  "st. louis cardinals": "stl",
    "baltimore orioles": "bal",
    # NBA
    "atlanta hawks":"atl","boston celtics":"bos","brooklyn nets":"bkn","charlotte hornets":"cha",
    "chicago bulls":"chi","cleveland cavaliers":"cle","dallas mavericks":"dal","denver nuggets":"den",
    "detroit pistons":"det","golden state warriors":"gsw","houston rockets":"hou","indiana pacers":"ind",
    "la clippers":"lac","los angeles clippers":"lac","los angeles lakers":"lal","memphis grizzlies":"mem",
    "miami heat":"mia","milwaukee bucks":"mil","minnesota timberwolves":"min","new orleans pelicans":"nop",
    "new york knicks":"nyk","oklahoma city thunder":"okc","orlando magic":"orl","philadelphia 76ers":"phi",
    "phoenix suns":"phx","portland trail blazers":"por","sacramento kings":"sac","san antonio spurs":"sas",
    "toronto raptors":"tor","utah jazz":"uta","washington wizards":"was",
    # NHL
    "anaheim ducks":"ana","boston bruins":"bos","buffalo sabres":"buf","calgary flames":"cgy",
    "carolina hurricanes":"car","chicago blackhawks":"chi","colorado avalanche":"col","columbus blue jackets":"cbj",
    "dallas stars":"dal","detroit red wings":"det","edmonton oilers":"edm","florida panthers":"fla",
    "los angeles kings":"lak","minnesota wild":"min","montreal canadiens":"mtl","nashville predators":"nsh",
    "new jersey devils":"nj","new york islanders":"nyi","new york rangers":"nyr","ottawa senators":"ott",
    "philadelphia flyers":"phi","pittsburgh penguins":"pit","san jose sharks":"sj","seattle kraken":"sea",
    "st. louis blues":"stl","tampa bay lightning":"tb","toronto maple leafs":"tor","vancouver canucks":"van",
    "vegas golden knights":"vgk","washington capitals":"wsh","winnipeg jets":"wpg","utah hockey club":"uta",
}


def _abbrev(team: str) -> str | None:
    return _TEAM_ABBREV.get(team.lower().strip())


def _candidate_slugs(sport: str, ev: ESPNGame) -> list[str]:
    """Plausible Polymarket slug patterns for a given ESPN game."""
    date = ev.start.strftime("%Y-%m-%d")
    date_compact = ev.start.strftime("%B-%-d").lower()  # e.g. april-17
    home_ab = _abbrev(ev.home)
    away_ab = _abbrev(ev.away)
    home_norm = _norm(ev.home).replace(" ", "-")
    away_norm = _norm(ev.away).replace(" ", "-")
    slug_sport = sport.lower()

    candidates: list[str] = []
    if home_ab and away_ab:
        candidates += [
            f"{slug_sport}-{away_ab}-{home_ab}-{date}",
            f"{slug_sport}-{home_ab}-{away_ab}-{date}",
            f"{away_ab}-vs-{home_ab}-{date}",
            f"{home_ab}-vs-{away_ab}-{date}",
        ]
    candidates += [
        f"will-the-{home_norm}-beat-the-{away_norm}",
        f"will-the-{away_norm}-beat-the-{home_norm}",
        f"{away_norm}-vs-{home_norm}-{date}",
        f"{home_norm}-vs-{away_norm}-{date}",
        f"{slug_sport}-{away_norm}-at-{home_norm}-{date_compact}",
    ]
    # Dedupe preserving order
    seen: set[str] = set()
    out: list[str] = []
    for c in candidates:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def _poly_slug_exists(slug: str) -> dict[str, Any] | None:
    """Hit gamma /markets/slug/<slug> directly. Cheap: single HTTP call per guess.
    Returns the market dict on 200, None on 404 or error.
    """
    url = f"{GAMMA_BASE}/markets/slug/{slug}"
    try:
        with _http() as h:
            r = h.get(url, timeout=10.0)
            if r.status_code == 200:
                data = r.json() or {}
                if data and not data.get("closed"):
                    return data
    except Exception:
        pass
    return None


def discover_by_slug_probe(sport: str, espn_games: list[ESPNGame]) -> int:
    """Fallback: for each ESPN game, probe a handful of likely Polymarket
    slug patterns. Seed anything that returns 200. Returns count seeded.
    """
    seeded = 0
    existing = {m["poly_market_id"] for m in db.list_active_markets(sport)
                if m.get("poly_market_id")}
    for ev in espn_games:
        found = None
        for slug in _candidate_slugs(sport, ev):
            if slug in existing:
                break
            found = _poly_slug_exists(slug)
            if found:
                break
        if not found:
            continue
        slug = found.get("slug") or slug
        if slug in existing:
            continue
        home_side = _infer_home_side(found, ev)
        try:
            m = Market(
                sport=sport,
                event_name=f"{ev.away} @ {ev.home}",
                event_start=ev.start,
                poly_market_id=slug,
            )
            row = db.upsert_market(m)
            db.client().table("markets").update(
                {"notes": {"poly_home_side": home_side,
                           "auto_seeded": True,
                           "discovered_via": "slug-probe",
                           "ml_question": found.get("question")}}
            ).eq("id", row["id"]).execute()
            seeded += 1
            existing.add(slug)
            log.info("slug-probe seeded %s (%s @ %s) home_side=%s",
                     slug, ev.away, ev.home, home_side)
        except Exception as e:
            log.warning("slug-probe upsert(%s) failed: %s", slug, e)
    return seeded


def discover_sport(sport: str, days_ahead: int = 3) -> dict[str, int]:
    """Discover + seed markets for one sport. Returns counts."""
    counts = {
        "gamma_events": 0, "matched": 0, "seeded": 0,
        "skipped_existing": 0, "skipped_no_ml": 0, "skipped_no_match": 0,
        "slug_probe_seeded": 0,
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

    # Fallback: if gamma discovery seeded nothing, probe Polymarket slugs
    # directly from ESPN team names. Hits /markets/slug/<slug> with a few
    # candidate patterns per game.
    if counts["seeded"] == 0 and espn_games:
        log.info("gamma seeded 0 for %s — falling back to slug probe", sport)
        probed = discover_by_slug_probe(sport, espn_games)
        counts["slug_probe_seeded"] = probed

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
