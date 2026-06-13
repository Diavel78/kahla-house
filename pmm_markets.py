"""Polymarket market lookup for Pick Bot.

Given a game (sport, away, home, event_start), find the matching
Polymarket event and parse its sub-markets into ML / Spread / Total
buckets with current bid/ask. Powers the Polymarket-target display on
the Pick Bot dossier so the user can see PMM's actual line + price
alongside PIN's devigged fair.

Strategy:
  1. Map our sport code → Polymarket tag slug.
  2. Hit `Events.list(tagSlug=..., startTimeMin=..., startTimeMax=...,
     active=true, closed=false)`. Walk the returned events looking for
     one whose title or markets reference both team names.
  3. Within the matched event, classify each Market as ml / spread /
     total via title + outcome pattern + extract the line.
  4. For each classified market, hit `Markets.bbo(slug)` for current
     bid/ask. Convert decimal prices to American odds.

All calls go through the existing `app.get_client()` PolymarketUS
singleton — same credentials used by the dashboard P&L code. Reads
only; never writes.

Caching:
  • Event-search results: 5 min per (sport, normalized event_name).
  • BBO: 30 sec per market_slug.
Both are module-level dicts that survive across requests on the same
warm Vercel container.

Failure modes are silent — every parsing exception returns "no PMM
match" rather than breaking the dossier. The Pick Bot dossier falls
back to PIN fair at PIN's line in that case (current behavior).
"""
from __future__ import annotations

import logging
import re
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Any

log = logging.getLogger(__name__)


# Our sport code → Polymarket tag slug. Verified against Polymarket's
# public sport tag pages (e.g. polymarket.com/sports/nba). UFC and
# soccer aren't useful for the spread/total flow — PMM has UFC ML only
# and we don't ingest soccer odds anyway. NCAAF and CBB are in the
# college-football and college-basketball tags respectively.
_SPORT_TAG_SLUG: dict[str, str] = {
    "MLB":   "mlb",
    "NBA":   "nba",
    "NHL":   "nhl",
    "NFL":   "nfl",
    "NCAAF": "college-football",
    "CBB":   "college-basketball",
    "UFC":   "ufc",
}

# World Cup tag slugs to try, in order — for the read-only World Cup
# viewer on the Pick Bot page (see list_world_cup below). PMM's exact
# slug for the 2026 tournament is UNVERIFIED from our sandbox, so we try
# the likely candidates and use the first that returns events. Same
# defensive posture as the NRFI schema — the diag dump is ground truth.
_WORLD_CUP_TAG_SLUGS: list[str] = [
    "fifa-world-cup", "world-cup", "2026-fifa-world-cup",
    "world-cup-2026", "soccer-world-cup", "soccer",
]

# Caches. Module-level so they survive across requests on a warm Vercel
# container; cold start resets (acceptable — pays one extra event-search
# round-trip per cold-start container, then caches build back up).
_EVENT_CACHE: dict[str, tuple[float, dict | None]] = {}
_EVENT_CACHE_TTL_SEC = 5 * 60

# Whole-payload cache for the World Cup viewer (list_world_cup does many
# event/market round-trips, so we don't want to rebuild it on every tab
# view / poll). Module-level; cold start resets.
_WC_CACHE: dict[str, Any] = {"t": 0.0, "data": None}
_WC_CACHE_TTL_SEC = 3 * 60


# ──────────────────────────── Math helpers ────────────────────────────

def _prob_to_american(prob: float | None) -> int | None:
    """0.55 → -122. None for unparseable / out-of-range."""
    if prob is None or not (0 < prob < 1):
        return None
    if prob >= 0.5:
        return int(round(-prob / (1 - prob) * 100))
    return int(round((1 - prob) / prob * 100))


def _safe_amount(val: Any) -> float | None:
    """SDK uses {value: '0.55', currency: 'USD'} for prices. Return the
    float, or None if missing/unparseable."""
    if val is None:
        return None
    if isinstance(val, dict):
        val = val.get("value")
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


# ──────────────────────────── Normalization ────────────────────────────

def _norm(s: str) -> str:
    """Lowercase + strip accents + collapse non-alphanumeric → spaces.
    Same approach as the live-event matcher in handicapper_web.py."""
    if not s:
        return ""
    nfkd = unicodedata.normalize("NFKD", s)
    no_acc = "".join(c for c in nfkd if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9]+", " ", no_acc.lower()).strip()


def _name_match(haystack: str, needle: str) -> bool:
    """Two-way substring match on normalized names. 'Mariners' matches
    'Seattle Mariners' and vice versa. Mirrors the splits-matching
    convention used elsewhere."""
    if not (haystack and needle):
        return False
    h, n = _norm(haystack), _norm(needle)
    if not (h and n):
        return False
    return n in h or h in n


def _team_mention_pos(q_norm: str, team: str) -> int:
    """Earliest position in the normalized question where this team is
    identifiable, trying full name → city/region → mascot last-token.
    Returns -1 if none of the variants appear.

    The city variant is essential: PMM titles many NBA/NFL markets by
    city ("Spread: Oklahoma City (-3)", "Oklahoma City to win"), where
    neither the full name "oklahoma city thunder" nor the mascot
    "thunder" appears — so without it ML/spread markets fail side
    detection and get silently dropped."""
    best = -1
    for cand in (_norm(team), _city_tokens(team), _last_token(team)):
        if not cand:
            continue
        p = q_norm.find(cand)
        if p >= 0 and (best < 0 or p < best):
            best = p
    return best


def _side_by_first_mention(question: str, away: str, home: str) -> str | None:
    """Pick home/away based on which team is named FIRST in the question.
    PMM questions like "Spread: Baltimore Orioles (+4.5)" or "Oklahoma
    City to win" name the YES-side team first. If both teams appear
    (e.g., "Team A to win vs Team B"), the earlier one is YES. Each team
    is matched by full name, city/region, OR mascot (see
    _team_mention_pos) so city-titled markets resolve correctly.

    Returns "home", "away", or None if neither team is found."""
    q = _norm(question)
    if not q:
        return None
    a_pos = _team_mention_pos(q, away)
    h_pos = _team_mention_pos(q, home)
    if a_pos < 0 and h_pos < 0:
        return None
    if h_pos < 0:
        return "away"
    if a_pos < 0:
        return "home"
    return "away" if a_pos < h_pos else "home"


def _last_token(team_name: str) -> str:
    """Last word in a team name. 'Baltimore Orioles' → 'orioles'.
    Used as a looser match fallback when PMM uses just the team
    nickname in event titles."""
    parts = [p for p in _norm(team_name).split() if len(p) >= 3]
    return parts[-1] if parts else ""


def _city_tokens(team_name: str) -> str:
    """Everything BEFORE the trailing mascot — the city/region part.
    'Oklahoma City Thunder' → 'oklahoma city'; 'Spurs' → ''. PMM
    occasionally titles NBA/NFL events by city ("Oklahoma City vs
    San Antonio") where the mascot last-token isn't present, so a
    both-cities-present pass catches those."""
    parts = [p for p in _norm(team_name).split() if len(p) >= 3]
    return " ".join(parts[:-1]) if len(parts) >= 2 else ""



def _match_event_to_game(events: list, away: str, home: str) -> Any | None:
    """Walk a list of PMM events looking for one referring to the
    away vs home game. Tries progressively looser matches:
      1. Title contains BOTH full team names
      2. Title contains BOTH team last-tokens (e.g., 'orioles' + 'rays')
      3. Any market within the event has team.name matching one of
         our teams AND title mentions the other
    Returns the matched event (untouched SDK object) or None."""
    away_last = _last_token(away)
    home_last = _last_token(home)
    for ev in events:
        title = ev.get("title") if isinstance(ev, dict) else getattr(ev, "title", "")
        # Pass 1: full team names
        if _name_match(title, away) and _name_match(title, home):
            return ev
        # Pass 2: last tokens (Orioles + Rays)
        if away_last and home_last:
            t_norm = _norm(title)
            if away_last in t_norm and home_last in t_norm:
                return ev
    # Pass 2.5: city/region tokens both present ("Oklahoma City vs
    # San Antonio") — for events PMM titles by city instead of mascot.
    away_city = _city_tokens(away)
    home_city = _city_tokens(home)
    if away_city and home_city:
        for ev in events:
            title = ev.get("title") if isinstance(ev, dict) else getattr(ev, "title", "")
            t_norm = _norm(title)
            if away_city in t_norm and home_city in t_norm:
                return ev
    for ev in events:
        markets = ev.get("markets") if isinstance(ev, dict) else getattr(ev, "markets", [])
        markets = markets or []
        title = ev.get("title") if isinstance(ev, dict) else getattr(ev, "title", "")
        for m in markets:
            team = m.get("team") if isinstance(m, dict) else getattr(m, "team", None)
            tname = None
            if team:
                tname = (team.get("name") if isinstance(team, dict)
                         else getattr(team, "name", None))
            mtitle = m.get("title") if isinstance(m, dict) else getattr(m, "title", "")
            if tname and (_name_match(tname, away) or _name_match(tname, home)):
                if _name_match(mtitle, away) or _name_match(mtitle, home) \
                   or _name_match(title, away) or _name_match(title, home):
                    return ev
    return None


# ──────────────────────────── Event search ────────────────────────────

def _search_event(client, sport: str, away: str, home: str,
                  event_start_iso: str,
                  diag: dict | None = None) -> dict | None:
    """Find the Polymarket event for one game. Returns the matched
    event dict (with .markets) or None.

    `diag` (if passed) gets populated with search diagnostics so the
    debug surface can show why a game didn't match — events_returned,
    sample_event_titles, tag_used, time_window_used.
    """
    tag = _SPORT_TAG_SLUG.get(sport)
    if not tag:
        if diag is not None:
            diag["error"] = f"sport {sport} not in _SPORT_TAG_SLUG"
        return None
    try:
        bet_dt = datetime.fromisoformat(event_start_iso.replace("Z", "+00:00"))
    except Exception as e:
        if diag is not None:
            diag["error"] = f"bad event_start: {e}"
        return None

    # Window: PMM's startTime can drift from ours (per-sport scheduling
    # differences). Use a generous ±12h window — we filter by team-name
    # match anyway, so over-fetching is cheap.
    win_min = (bet_dt - timedelta(hours=12)).isoformat()
    win_max = (bet_dt + timedelta(hours=12)).isoformat()
    cache_key = f"{sport}:{_norm(away)}:{_norm(home)}:{bet_dt.date().isoformat()}"
    cached = _EVENT_CACHE.get(cache_key)
    if cached and (time.time() - cached[0]) < _EVENT_CACHE_TTL_SEC:
        if diag is not None:
            diag["cache_hit"] = True
        return cached[1]

    if diag is not None:
        diag["tag"] = tag
        diag["window_min"] = win_min
        diag["window_max"] = win_max

    # Try a couple param shapes. Polymarket events aren't always
    # `active=true` until close to tip, and sometimes the tag filter
    # alone misses events that need `relatedTags=true`. Iterate until
    # we get a non-empty response, then proceed with team matching.
    attempts = [
        {"tagSlug": tag, "closed": False,
         "startTimeMin": win_min, "startTimeMax": win_max, "limit": 100},
        {"tagSlug": tag, "closed": False, "relatedTags": True,
         "startTimeMin": win_min, "startTimeMax": win_max, "limit": 100},
        {"tagSlug": tag, "closed": False, "limit": 200},   # no time filter as fallback
    ]
    # Try EACH param shape, accumulating a deduped union of events, and
    # attempt a team match after each. Stop as soon as we match — but do
    # NOT stop just because a shape returned a non-empty (possibly wrong)
    # result. The old code broke on the first non-empty response, so when
    # the tight time-windowed query returned a single unrelated NBA event
    # it never reached the broad `limit:200` fallback that actually
    # contained the game. (NBA playoff games were the recurring victim.)
    events: list = []
    seen_slugs: set = set()
    last_error = None
    used_attempt = None
    matched = None
    for i, params in enumerate(attempts):
        try:
            resp = client.events.list(params)
        except Exception as e:
            last_error = f"events.list attempt {i} failed: {str(e)[:200]}"
            continue
        evs = resp.get("events") if isinstance(resp, dict) else getattr(resp, "events", None)
        evs = evs or []
        for ev in evs:
            slug = ev.get("slug") if isinstance(ev, dict) else getattr(ev, "slug", None)
            if slug and slug in seen_slugs:
                continue
            if slug:
                seen_slugs.add(slug)
            events.append(ev)
        m = _match_event_to_game(events, away, home)
        if m is not None:
            matched = m
            used_attempt = i
            break
    if not events and last_error and diag is not None:
        diag["error"] = last_error

    if diag is not None:
        diag["events_returned"] = len(events)
        diag["sample_event_titles"] = [
            ((e.get("title") if isinstance(e, dict) else getattr(e, "title", "")) or "")
            for e in events[:8]
        ]
    # Events.list returns event metadata but NOT the nested markets
    # list (despite Event's TypedDict declaring markets). Hit the
    # markets.list endpoint directly with eventSlug filter — that one
    # IS documented to return markets. Try retrieve_by_slug as a
    # secondary fallback in case markets.list doesn't work for some
    # event types.
    if matched is not None:
        slug = matched.get("slug") if isinstance(matched, dict) else getattr(matched, "slug", None)
        existing_markets = matched.get("markets") if isinstance(matched, dict) else getattr(matched, "markets", None)
        existing_markets = existing_markets or []
        if not existing_markets and slug:
            fetched_markets: list = []
            # Primary: markets.list(eventSlug=[slug]) — returns markets
            # with embedded bestBidQuote/bestAskQuote.
            try:
                mresp = client.markets.list({
                    "eventSlug": [slug],
                    "closed":    False,
                    "limit":     100,
                })
                ms = mresp.get("markets") if isinstance(mresp, dict) else getattr(mresp, "markets", None)
                fetched_markets = ms or []
            except Exception as e:
                if diag is not None:
                    diag["markets_list_error"] = str(e)[:200]
            # Secondary fallback: events.retrieve_by_slug. Rarely needed
            # in practice but cheap to keep as a safety net.
            if not fetched_markets:
                try:
                    full = client.events.retrieve_by_slug(slug)
                    ev_full = full.get("event") if isinstance(full, dict) else getattr(full, "event", None)
                    if ev_full is not None:
                        mk = ev_full.get("markets") if isinstance(ev_full, dict) else getattr(ev_full, "markets", None)
                        if mk:
                            fetched_markets = mk
                except Exception as e:
                    if diag is not None:
                        diag["retrieve_by_slug_error"] = str(e)[:200]
            # Attach fetched markets back onto the matched event.
            if fetched_markets:
                if isinstance(matched, dict):
                    matched["markets"] = fetched_markets
                else:
                    try:
                        setattr(matched, "markets", fetched_markets)
                    except Exception:
                        matched = {
                            "id":        matched.get("id") if hasattr(matched, "get") else getattr(matched, "id", None),
                            "slug":      slug,
                            "title":     matched.get("title") if hasattr(matched, "get") else getattr(matched, "title", ""),
                            "startTime": matched.get("startTime") if hasattr(matched, "get") else getattr(matched, "startTime", ""),
                            "markets":   fetched_markets,
                        }

    # If filter-based search returned nothing useful, try a name search.
    # Polymarket's tag filter occasionally misses events that the
    # search-by-query endpoint finds.
    if matched is None:
        try:
            sresp = client.search.query({
                "query":  f"{away} {home}",
                "status": "upcoming",
                "limit":  20,
            })
            # Search response shape varies; look for events list
            search_events = []
            if isinstance(sresp, dict):
                search_events = sresp.get("events") or sresp.get("results") or []
            if search_events:
                matched = _match_event_to_game(search_events, away, home)
                # search result event likely lacks `markets` — refetch by slug
                if matched is not None:
                    slug = matched.get("slug") if isinstance(matched, dict) else getattr(matched, "slug", None)
                    if slug:
                        try:
                            full = client.events.retrieve_by_slug(slug)
                            ev = full.get("event") if isinstance(full, dict) else getattr(full, "event", None)
                            if ev:
                                matched = ev
                        except Exception as e:
                            if diag is not None:
                                diag["retrieve_by_slug_error"] = str(e)[:200]
        except Exception as e:
            if diag is not None:
                diag["search_fallback_error"] = str(e)[:200]

    result = _event_to_dict(matched) if matched else None
    # Only cache successful matches. Caching None would freeze "no
    # match" results for the TTL, which kills iteration speed when
    # we're tuning matching heuristics. Cost of not caching misses
    # is minimal — one extra round-trip per minute per missed game.
    if result is not None:
        _EVENT_CACHE[cache_key] = (time.time(), result)
    if diag is not None:
        diag["matched"] = bool(matched)
        if matched:
            diag["matched_title"] = (matched.get("title") if isinstance(matched, dict)
                                      else getattr(matched, "title", ""))
            diag["matched_slug"] = (matched.get("slug") if isinstance(matched, dict)
                                     else getattr(matched, "slug", ""))
    return result


def _event_to_dict(ev: Any) -> dict:
    """Normalize SDK Event (TypedDict or object) into a plain dict so
    downstream code doesn't have to care about the SDK's shape."""
    def g(k):
        return ev.get(k) if isinstance(ev, dict) else getattr(ev, k, None)
    markets = g("markets") or []
    return {
        "id":        g("id"),
        "slug":      g("slug"),
        "title":     g("title"),
        "startTime": g("startTime"),
        "markets":   [_market_to_dict(m) for m in markets],
    }


def _market_to_dict(m: Any) -> dict:
    """Normalize one SDK Market into a plain dict containing only the
    fields downstream code uses. PMM's actual response shape (post-
    discovery): question / sportsMarketType{V2} / line / bestBidQuote /
    bestAskQuote / marketSides. No `team` field at all (post-discovery
    May 2026)."""
    def g(k):
        return m.get(k) if isinstance(m, dict) else getattr(m, k, None)
    return {
        "id":                  g("id"),
        "slug":                g("slug"),
        "question":            g("question"),
        "description":         g("description"),
        "sportsMarketType":    g("sportsMarketType"),
        "sportsMarketTypeV2":  g("sportsMarketTypeV2"),
        "line":                g("line"),
        "spreadTotalSuffix":   g("spreadTotalSuffix"),
        "marketSides":         g("marketSides"),
        "bestBidQuote":        g("bestBidQuote"),
        "bestAskQuote":        g("bestAskQuote"),
        "active":              g("active"),
        "closed":              g("closed"),
    }


# ──────────────────────────── Market classification ────────────────────────────

# sportsMarketTypeV2 → our market_type bucket. Field is directly on
# each market in the API response (no regex on titles needed). Variants
# like `baseball_team_first_five_total` are filed under SPORTS_MARKET_
# TYPE_TOTAL too — we filter those out by checking the lowercase
# sportsMarketType (v1) field against a primary-types allowlist.
_MARKET_TYPE_BUCKET: dict[str, str] = {
    "SPORTS_MARKET_TYPE_MONEYLINE": "ml",
    "SPORTS_MARKET_TYPE_SPREAD":    "spread",
    "SPORTS_MARKET_TYPE_TOTAL":     "total",
}
# Primary-type slugs we accept. Anything else under the same V2 bucket
# is a prop variation (e.g. first-five-innings, first-inning, etc.).
_PRIMARY_TYPE_SLUGS: set[str] = {
    "moneyline", "h2h",
    "spreads", "spread", "handicap",
    "totals", "total", "over_under",
}


def _classify_market(m: dict, away: str, home: str
                     ) -> tuple[str, float | None, str | None] | None:
    """Identify a Polymarket market as ml/spread/total and extract its
    line + side. Returns (market_type, line, side) or None.

      market_type: "ml" | "spread" | "total"
      line:        float for spread/total, None for ml
      side:        "home" | "away" for ml/spread, "over" | "under" for total

    Uses the real API schema (post-discovery May 2026):
      sportsMarketTypeV2: 'SPORTS_MARKET_TYPE_MONEYLINE' / '_SPREAD' / '_TOTAL'
      sportsMarketType:   'moneyline' / 'spreads' / 'totals' / variants
      line:               numeric line (e.g. 4.5)
      question:           human-readable like "Spread: Baltimore Orioles (+4.5)"
                          → encodes which team is the YES side
    """
    v2 = m.get("sportsMarketTypeV2") or ""
    v1 = (m.get("sportsMarketType") or "").lower()
    bucket = _MARKET_TYPE_BUCKET.get(v2)
    if not bucket:
        return None
    # Skip prop variants (first-five, first-inning, etc.) — they live
    # under the same V2 bucket but have non-primary V1 slugs.
    if v1 not in _PRIMARY_TYPE_SLUGS:
        return None

    line = m.get("line")
    if line is not None:
        try:
            line = float(line)
        except (TypeError, ValueError):
            line = None

    question = m.get("question") or ""

    if bucket == "ml":
        side = _side_by_first_mention(question, away, home)
        return ("ml", None, side) if side else None

    if bucket == "spread":
        # Question pattern: "Spread: Baltimore Orioles (+4.5)" — the
        # team named first IS the YES side. Line comes from the
        # `line` field. The synthetic inverse side is generated
        # downstream (one PMM market = both sides via bid/ask invert).
        side = _side_by_first_mention(question, away, home)
        return ("spread", line, side) if side else None

    if bucket == "total":
        # Question pattern truncated to "Baltimore Orioles vs. Tampa Bay
        # Rays: O/" — PMM's convention is YES = OVER for these binary
        # total markets. If the question explicitly says "under" we
        # honor that; otherwise default to over.
        q = question.lower()
        if "under" in q and "over" not in q:
            return ("total", line, "under")
        return ("total", line, "over")

    return None


# First-inning (NRFI/YRFI) detection. PMM files these OUTSIDE the
# ml/spread/total buckets (_classify_market returns None for them), so we
# sniff them separately by text. PMM's exact schema for first-inning
# markets is UNVERIFIED from our side — the lookup() diagnostic dumps
# every market's question/type so we can confirm + tune this against a
# real MLB event. Detection is permissive on purpose; the diag is the
# ground truth.
_FI_HINTS = ("1st inning", "first inning", "1st-inning", "first-inning",
             "inning 1", "1st_inning", "first_inning")


def _classify_first_inning(m: dict) -> tuple[str, float | None, str] | None:
    """Detect a game-level NRFI/YRFI market. Returns ('nrfi', None, side)
    where side is the YES outcome: 'yes' = a run scores in the 1st (YRFI),
    'no' = no run (NRFI). None if this isn't a first-inning run market.

    Orientation comes from the question framing: a "no run / scoreless"
    phrasing makes YES = NRFI; a "run scored" phrasing makes YES = YRFI.
    The opposite side is synthesized from the bid/ask inverse downstream."""
    q = (m.get("question") or "")
    text = (q + " " + str(m.get("slug") or "") + " "
            + str(m.get("sportsMarketType") or "")).lower()
    if not any(h in text for h in _FI_HINTS):
        return None
    # Must concern a run scoring (not a 1st-inning strikeout/hit/etc prop).
    if not any(k in text for k in ("run", "runs", "score", "scored",
                                   "scoreless", "nrfi", "yrfi")):
        return None
    no_frame = any(k in text for k in ("no run", "no runs", "scoreless", "nrfi"))
    yes_side = "no" if no_frame else "yes"
    return ("nrfi", None, yes_side)


def _get_market_quote(m: dict) -> dict | None:
    """Read bestBidQuote / bestAskQuote directly off the market.
    PMM embeds the BBO on every market in the markets.list response —
    no separate bbo() call required."""
    bid = _safe_amount(m.get("bestBidQuote"))
    ask = _safe_amount(m.get("bestAskQuote"))
    if bid is None and ask is None:
        return None
    mid = ((bid + ask) / 2) if (bid is not None and ask is not None) else (bid or ask)
    return {
        "bid": bid,
        "ask": ask,
        "mid": mid,
        "bid_american":  _prob_to_american(bid) if bid is not None else None,
        "ask_american":  _prob_to_american(ask) if ask is not None else None,
        "mid_american":  _prob_to_american(mid) if mid is not None else None,
    }


def _inverse_quote(quote: dict | None) -> dict | None:
    """For a binary YES market, derive the NO side's quote.
    NO bid = 1 - YES ask. NO ask = 1 - YES bid. (Bid/ask flip on inverse.)
    """
    if not quote:
        return None
    y_bid = quote.get("bid")
    y_ask = quote.get("ask")
    n_bid = (1.0 - y_ask) if y_ask is not None else None
    n_ask = (1.0 - y_bid) if y_bid is not None else None
    n_mid = ((n_bid + n_ask) / 2) if (n_bid is not None and n_ask is not None) else (n_bid or n_ask)
    return {
        "bid": n_bid,
        "ask": n_ask,
        "mid": n_mid,
        "bid_american":  _prob_to_american(n_bid) if n_bid is not None else None,
        "ask_american":  _prob_to_american(n_ask) if n_ask is not None else None,
        "mid_american":  _prob_to_american(n_mid) if n_mid is not None else None,
    }


def _inverse_side(market_type: str, side: str, line: float | None
                  ) -> tuple[str | None, float | None]:
    """Return (side, line) for the inverse of a binary YES market.
       ml home → away (no line change)
       spread +4.5 home → -4.5 away (line flips sign)
       total over 4.5 → under 4.5 (line unchanged)
    """
    if market_type == "ml":
        opp = "away" if side == "home" else "home"
        return opp, None
    if market_type == "spread":
        opp = "away" if side == "home" else "home"
        inv_line = -line if line is not None else None
        return opp, inv_line
    if market_type == "total":
        opp = "under" if side == "over" else "over"
        return opp, line
    return None, None


# ──────────────────────────── Public entry point ────────────────────────────

def lookup(client, sport: str, away: str, home: str, event_start_iso: str,
           with_bbo: bool = True,
           diag: dict | None = None) -> dict | None:
    """Top-level: find the PMM event for a game and return its parsed
    markets with current bid/ask.

    Returns a dict shaped:
        {
          "event_slug":  "nba-cle-vs-det-2026-05-17",
          "event_title": "Cavaliers vs Pistons",
          "ml": [
            {"side": "home", "slug": "...", "title": "...", "quote": {...}},
            {"side": "away", "slug": "...", "title": "...", "quote": {...}},
          ],
          "spread": [
            {"side": "home", "line": -7.5, "slug": "...", "quote": {...}},
            ... (one entry per line+side PMM offers)
          ],
          "total": [
            {"side": "over",  "line": 215.5, "slug": "...", "quote": {...}},
            {"side": "under", "line": 215.5, "slug": "...", "quote": {...}},
          ],
        }
    or None if no PMM event matched. Caller falls back to PIN-only.

    Passing `with_bbo=False` skips the per-market BBO fetches (returns
    the structure without `quote` fields) — useful for debug surfaces
    that want to inspect classification without burning BBO calls.
    """
    if not client:
        return None
    ev = _search_event(client, sport, away, home, event_start_iso, diag=diag)
    if not ev or not ev.get("markets"):
        return None

    out: dict[str, Any] = {
        "event_slug":  ev.get("slug"),
        "event_title": ev.get("title"),
        "event_start": ev.get("startTime"),
        "ml":     [],
        "spread": [],
        "total":  [],
        "nrfi":   [],
    }
    # Discovery diagnostic — dump EVERY market on the event (question +
    # type slugs + line + closed/active) so we can see exactly what PMM
    # offers for a game, including whether it has a first-inning market
    # and what its schema looks like. This is how we confirm/tune the
    # NRFI detection without being able to inspect the SDK from a sandbox.
    if diag is not None:
        diag["all_markets"] = [
            {"q": (mm.get("question") or "")[:80],
             "v1": mm.get("sportsMarketType"),
             "v2": mm.get("sportsMarketTypeV2"),
             "line": mm.get("line"),
             "closed": bool(mm.get("closed")), "active": bool(mm.get("active"))}
            for mm in (ev.get("markets") or [])[:50]
        ]
    for m in ev["markets"]:
        # Skip markets that have closed (settled, expired, etc.) — we
        # only want live tradeable markets for the limit-order target.
        if m.get("closed") or not m.get("active"):
            continue
        result = _classify_market(m, away, home)
        if not result:
            # Not a primary ml/spread/total — try the first-inning
            # (NRFI/YRFI) sniff before giving up on this market.
            fi = _classify_first_inning(m)
            if fi:
                mt, line, side = fi
                quote = _get_market_quote(m) if with_bbo else None
                out["nrfi"].append({"side": side, "line": None,
                                    "slug": m.get("slug"),
                                    "title": m.get("question"), "quote": quote})
                opp = "no" if side == "yes" else "yes"
                out["nrfi"].append({"side": opp, "line": None,
                                    "slug": m.get("slug"),
                                    "title": m.get("question"),
                                    "quote": _inverse_quote(quote),
                                    "synthetic": True})
            continue
        mt, line, side = result
        quote = _get_market_quote(m) if with_bbo else None
        # Direct entry — the YES side of this binary market.
        out[mt].append({
            "side":  side,
            "line":  line,
            "slug":  m.get("slug"),
            "title": m.get("question"),
            "quote": quote,
        })
        # Synthesized inverse entry — derive the NO side's pricing
        # from this same market. PMM doesn't publish a separate market
        # for the NO side; we synthesize it so best_line_for() can
        # find it when the bot asks for the opposite side.
        inv_side, inv_line = _inverse_side(mt, side, line)
        if inv_side is not None:
            out[mt].append({
                "side":      inv_side,
                "line":      inv_line,
                "slug":      m.get("slug"),
                "title":     m.get("question"),
                "quote":     _inverse_quote(quote),
                "synthetic": True,
            })

    if diag is not None:
        diag["counts"] = {k: len(out.get(k, [])) for k in ("ml", "spread", "total", "nrfi")}
    return out


# ──────────────────────────── Pick the best line ────────────────────────────

def best_line_for(pmm: dict | None, market_type: str, side: str,
                  pin_line: float | None) -> dict | None:
    """Pick the PMM market entry whose line is closest to PIN's line
    on the same side. For ML there's no line — return the matching-side
    entry directly. Returns None if PMM has no matching entry.

    market_type: "ml" | "spread" | "total"
    side:        "home" | "away" (ml/spread) | "over" | "under" (total)
    """
    if not pmm:
        return None
    entries = (pmm.get(market_type) or [])
    if not entries:
        return None
    same_side = [e for e in entries if e.get("side") == side]
    if not same_side:
        return None
    if market_type == "ml" or pin_line is None:
        return same_side[0]
    # Closest line wins.
    best = None
    best_diff = None
    for e in same_side:
        ln = e.get("line")
        if ln is None:
            continue
        diff = abs(ln - pin_line)
        if best_diff is None or diff < best_diff:
            best, best_diff = e, diff
    return best


# ──────────────────────────── World Cup viewer ────────────────────────────
# Read-only listing of live World Cup markets straight from Polymarket —
# outright Winner board + upcoming/live match markets — for the Pick Bot
# page's World Cup tab. Deliberately bypasses ALL the PIN/Kalshi/Odds-API
# machinery (no markets table, no devig, no sharp score, no logging): this
# is pure "nice to see the World Cup stuff" display.

def _mget(o: Any, k: str) -> Any:
    """Field getter that works on either a dict or an SDK object."""
    return o.get(k) if isinstance(o, dict) else getattr(o, k, None)


def _wc_result_label(m: Any, t1: str | None, t2: str | None) -> str:
    """Label one soccer match-result (drawable_outcome) leg as a team name
    or 'Draw'. PMM phrases these as 'Will Qatar win against Switzerland…'
    (a team leg) and 'Will the World Cup match Qatar vs Switzerland … end
    in a draw?' (the draw leg). Prefer the SDK's short outcome fields if
    present; otherwise read the question."""
    for k in ("groupItemTitle", "shortTitle", "yesSubTitle", "outcome"):
        v = _mget(m, k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    q = (_mget(m, "question") or "")
    if "draw" in q.lower():
        return "Draw"
    # "Will Qatar win against Switzerland…" → the team that wins.
    mt = re.match(r"^will\s+(?:the\s+)?(.+?)\s+win\b", q, re.I)
    if mt:
        cand = mt.group(1).strip()
        for t in (t1, t2):           # normalize to the clean title name
            if t and _name_match(cand, t):
                return t
        return cand
    return "Draw"   # drawable leg with no team-win and no 'draw' word


def _is_match_title(title: str) -> bool:
    """A head-to-head match event ("Qatar vs. Switzerland"). The outright
    Winner board, group-winner markets, and goalscorer props are NOT
    matches and are excluded from the match list."""
    t = (title or "").lower()
    return (" vs " in t) or (" vs. " in t) or (" v " in t)


def _wc_teams_from_title(title: str) -> tuple[str | None, str | None]:
    """Split 'Qatar vs. Switzerland' → ('Qatar', 'Switzerland')."""
    parts = re.split(r"\s+vs\.?\s+|\s+v\s+", title or "", maxsplit=1, flags=re.I)
    if len(parts) == 2:
        return parts[0].strip() or None, parts[1].strip() or None
    return None, None


def _order_three_way(results: list[dict], t1: str | None, t2: str | None) -> list[dict]:
    """Order match-result outcomes as team1 / Draw / team2 (the familiar
    1-X-2). Falls back to probability-desc if the teams can't be matched
    to outcome labels."""
    draw = [r for r in results if "draw" in (r.get("label") or "").lower()]

    def _is(r, team):
        return bool(team) and _name_match(r.get("label") or "", team)

    r1 = [r for r in results if _is(r, t1) and not _is(r, t2)]
    r2 = [r for r in results if _is(r, t2) and not _is(r, t1)]
    ordered = r1[:1] + draw[:1] + r2[:1]
    if len(ordered) < 2:   # team labels didn't resolve — show by prob
        ordered = sorted(results, key=lambda r: (r.get("prob") is None, -(r.get("prob") or 0.0)))
    return ordered[:3]


def list_world_cup(client, max_events: int = 40,
                   time_budget_sec: float = 7.5,
                   diag: dict | None = None) -> dict:
    """List live World Cup MATCHES from Polymarket for the read-only
    viewer — match-result (moneyline 1-X-2) only, no totals / props /
    outright board. Returns:

        {"tag_used": "fifa-world-cup",
         "matches": [
            {"slug", "title", "start_time",
             "results": [{"label","prob","american"}]},   # team1 / Draw / team2
            ...]}

    Matches are ordered by start time (upcoming + live, finished dropped).
    Silent-fail → empty list (the caller surfaces "no matches right now").
    """
    out: dict[str, Any] = {"tag_used": None, "matches": []}
    if not client:
        return out

    # Whole-payload cache (skip when a diag dump is requested — debug
    # wants a fresh round-trip).
    if diag is None:
        c = _WC_CACHE
        if c.get("data") is not None and (time.time() - c.get("t", 0)) < _WC_CACHE_TTL_SEC:
            return c["data"]

    t0 = time.time()

    # 1. Pull World Cup events by tag — first candidate slug that returns
    #    events wins; accumulate a deduped union across param shapes.
    events: list = []
    seen: set = set()
    tag_used = None
    for slug in _WORLD_CUP_TAG_SLUGS:
        for params in ({"tagSlug": slug, "closed": False, "limit": 100},
                       {"tagSlug": slug, "closed": False, "relatedTags": True, "limit": 100}):
            try:
                resp = client.events.list(params)
            except Exception as e:
                if diag is not None:
                    diag.setdefault("errors", []).append(f"events.list {slug}: {str(e)[:140]}")
                continue
            evs = resp.get("events") if isinstance(resp, dict) else getattr(resp, "events", None)
            evs = evs or []
            for ev in evs:
                s = _mget(ev, "slug")
                if s and s in seen:
                    continue
                if s:
                    seen.add(s)
                events.append(ev)
            if evs and tag_used is None:
                tag_used = slug
        if events:
            break
    out["tag_used"] = tag_used
    if diag is not None:
        diag["events_returned"] = len(events)
        diag["sample_titles"] = [(_mget(e, "title") or "") for e in events[:12]]
    if not events:
        return out

    # 2. Keep only head-to-head match events; drop finished ones (started
    #    > 4h ago and never flagged closed). Order by kickoff.
    now = datetime.now(timezone.utc)
    enriched: list[dict] = []
    for ev in events:
        title = _mget(ev, "title") or ""
        if not _is_match_title(title):
            continue
        st = _mget(ev, "startTime") or _mget(ev, "start_time") or ""
        try:
            sdt = datetime.fromisoformat(st.replace("Z", "+00:00")) if st else None
        except Exception:
            sdt = None
        if sdt is not None and sdt < (now - timedelta(hours=4)):
            continue
        t1, t2 = _wc_teams_from_title(title)
        enriched.append({"ev": ev, "title": title, "start": st, "sdt": sdt, "t1": t1, "t2": t2})

    enriched.sort(key=lambda e: e["sdt"].timestamp() if e["sdt"] is not None else float("inf"))

    # 3. Per match, fetch markets and keep ONLY the moneyline (match-
    #    result) outcomes — the 1-X-2. Totals, team-totals and goalscorer
    #    props are dropped. Time-budgeted so a big slate can't blow the
    #    Vercel 10s window.
    matches: list[dict] = []
    for e in enriched[:max_events]:
        if (time.time() - t0) > time_budget_sec:
            break
        ev = e["ev"]
        slug = _mget(ev, "slug")
        markets = _mget(ev, "markets") or []
        if not markets and slug:
            try:
                mr = client.markets.list({"eventSlug": [slug], "closed": False, "limit": 100})
                markets = (mr.get("markets") if isinstance(mr, dict)
                           else getattr(mr, "markets", None)) or []
            except Exception as ex:
                markets = []
                if diag is not None:
                    diag.setdefault("errors", []).append(f"markets.list {slug}: {str(ex)[:140]}")
        results: list[dict] = []
        for m in markets:
            if _mget(m, "closed"):
                continue
            v1 = (_mget(m, "sportsMarketType") or "").lower()
            # Full-game match result only. Soccer's 3-way is
            # 'drawable_outcome'; 2-way sports use 'moneyline'/'h2h'.
            # Excludes first/second-half variants, spreads, totals,
            # exact-score and goalscorer props (all separate type slugs).
            if v1 not in ("drawable_outcome", "moneyline", "h2h"):
                continue
            quote = _get_market_quote(_market_to_dict(m))
            if not quote:
                continue
            results.append({
                "label":    _wc_result_label(m, e["t1"], e["t2"]),
                "prob":     quote.get("mid"),
                "american": quote.get("mid_american"),
            })
        matches.append({
            "slug":       slug,
            "title":      e["title"],
            "start_time": e["start"],
            "results":    _order_three_way(results, e["t1"], e["t2"]),
        })

    out["matches"] = matches
    if diag is not None:
        diag["matches_built"] = [{"title": m["title"], "n": len(m["results"])} for m in matches[:40]]
        # Raw market shape on the first match — ground truth for confirming
        # the moneyline type string + label fields against the real schema.
        if enriched:
            fm = _mget(enriched[0]["ev"], "markets") or []
            diag["sample_markets"] = [
                {"q": (_mget(mm, "question") or "")[:70],
                 "v1": _mget(mm, "sportsMarketType"),
                 "v2": _mget(mm, "sportsMarketTypeV2")}
                for mm in fm[:25]
            ]
    if matches:
        _WC_CACHE["t"] = time.time()
        _WC_CACHE["data"] = out
    return out
