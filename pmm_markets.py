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

# Caches. Module-level so they survive across requests on a warm Vercel
# container; cold start resets (acceptable — pays one extra event-search
# round-trip per cold-start container, then caches build back up).
_EVENT_CACHE: dict[str, tuple[float, dict | None]] = {}
_EVENT_CACHE_TTL_SEC = 5 * 60

_BBO_CACHE: dict[str, tuple[float, dict | None]] = {}
_BBO_CACHE_TTL_SEC = 30


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


def _last_token(team_name: str) -> str:
    """Last word in a team name. 'Baltimore Orioles' → 'orioles'.
    Used as a looser match fallback when PMM uses just the team
    nickname in event titles."""
    parts = [p for p in _norm(team_name).split() if len(p) >= 3]
    return parts[-1] if parts else ""


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
    events: list = []
    last_error = None
    used_attempt = None
    for i, params in enumerate(attempts):
        try:
            resp = client.events.list(params)
        except Exception as e:
            last_error = f"events.list attempt {i} failed: {str(e)[:200]}"
            continue
        evs = resp.get("events") if isinstance(resp, dict) else getattr(resp, "events", None)
        evs = evs or []
        if evs:
            events = evs
            used_attempt = i
            break
        if used_attempt is None and evs is not None:
            used_attempt = i  # record that the call succeeded even if empty
    if not events and last_error and diag is not None:
        diag["error"] = last_error

    if diag is not None:
        diag["events_returned"] = len(events)
        diag["attempt_used"] = used_attempt
        diag["sample_event_titles"] = [
            (e.get("title") if isinstance(e, dict) else getattr(e, "title", ""))
            for e in events[:8]
        ]
        # Show normalized inputs + per-event match attempts so we can
        # see exactly which event the matcher considered and why it
        # rejected each. Truncate at 8 to keep payload small.
        diag["normalized_away"] = _norm(away)
        diag["normalized_home"] = _norm(home)
        away_last_dbg = _last_token(away)
        home_last_dbg = _last_token(home)
        diag["away_last_token"] = away_last_dbg
        diag["home_last_token"] = home_last_dbg
        attempts_dbg = []
        for e in events[:8]:
            t = e.get("title") if isinstance(e, dict) else getattr(e, "title", "")
            tn = _norm(t)
            attempts_dbg.append({
                "title":         t,
                "title_norm":    tn,
                "p1_away":       _name_match(t, away),
                "p1_home":       _name_match(t, home),
                "p2_away_tok":   bool(away_last_dbg and away_last_dbg in tn),
                "p2_home_tok":   bool(home_last_dbg and home_last_dbg in tn),
            })
        diag["match_attempts"] = attempts_dbg

    matched = _match_event_to_game(events, away, home)
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
            # Approach A: markets.list(eventSlug=[slug])
            try:
                mresp = client.markets.list({
                    "eventSlug": [slug],
                    "closed":    False,
                    "limit":     100,
                })
                ms = mresp.get("markets") if isinstance(mresp, dict) else getattr(mresp, "markets", None)
                fetched_markets = ms or []
                if diag is not None:
                    diag["markets_list_count"] = len(fetched_markets)
            except Exception as e:
                if diag is not None:
                    diag["markets_list_error"] = str(e)[:200]
            # Approach B: events.retrieve_by_slug (returns event with
            # markets sometimes — depends on PMM endpoint behavior).
            if not fetched_markets:
                try:
                    full = client.events.retrieve_by_slug(slug)
                    ev_full = full.get("event") if isinstance(full, dict) else getattr(full, "event", None)
                    if ev_full is not None:
                        mk = ev_full.get("markets") if isinstance(ev_full, dict) else getattr(ev_full, "markets", None)
                        if mk:
                            fetched_markets = mk
                            if diag is not None:
                                diag["retrieved_full_via_slug"] = slug
                except Exception as e:
                    if diag is not None:
                        diag["retrieve_by_slug_error"] = str(e)[:200]
            # Attach fetched markets back onto the matched event so
            # downstream code (_event_to_dict) sees them.
            if fetched_markets:
                if isinstance(matched, dict):
                    matched["markets"] = fetched_markets
                else:
                    try:
                        setattr(matched, "markets", fetched_markets)
                    except Exception:
                        # If we can't mutate the SDK object, wrap into
                        # a plain dict carrying the markets.
                        matched = {
                            "id":        matched.get("id") if hasattr(matched, "get") else getattr(matched, "id", None),
                            "slug":      slug,
                            "title":     matched.get("title") if hasattr(matched, "get") else getattr(matched, "title", ""),
                            "startTime": matched.get("startTime") if hasattr(matched, "get") else getattr(matched, "startTime", ""),
                            "markets":   fetched_markets,
                        }
        if diag is not None:
            mk = matched.get("markets") if isinstance(matched, dict) else getattr(matched, "markets", None)
            mk_list = mk or []
            diag["matched_markets_count"] = len(mk_list)
            # Dump RAW keys + values on the first market so we can see
            # what data shape the API actually returned. _market_to_dict
            # might be looking for the wrong fields.
            if mk_list:
                first = mk_list[0]
                if isinstance(first, dict):
                    diag["first_market_keys"] = list(first.keys())
                    # Stringify values briefly so we can read them
                    sample = {}
                    for k, v in list(first.items())[:30]:
                        try:
                            s = str(v) if v is not None else None
                            if s and len(s) > 100:
                                s = s[:100] + "..."
                            sample[k] = s
                        except Exception:
                            sample[k] = "<?>"
                    diag["first_market_sample"] = sample
                else:
                    diag["first_market_keys"] = [
                        a for a in dir(first)
                        if not a.startswith("_") and not callable(getattr(first, a, None))
                    ]

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
                if diag is not None:
                    diag["search_fallback_returned"] = len(search_events)
                    diag["search_sample_titles"] = [
                        (e.get("title") if isinstance(e, dict) else getattr(e, "title", ""))
                        for e in search_events[:8]
                    ]
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
    def g(k):
        return m.get(k) if isinstance(m, dict) else getattr(m, k, None)
    team = g("team")
    team_dict = None
    if team:
        tg = (lambda k: team.get(k) if isinstance(team, dict) else getattr(team, k, None))
        team_dict = {
            "name":         tg("name"),
            "abbreviation": tg("abbreviation"),
            "alias":        tg("alias"),
            "safeName":     tg("safeName"),
        }
    return {
        "id":      g("id"),
        "slug":    g("slug"),
        "title":   g("title"),
        "outcome": g("outcome"),
        "active":  g("active"),
        "closed":  g("closed"),
        "team":    team_dict,
    }


# ──────────────────────────── Market classification ────────────────────────────

# Total line pattern. PMM titles often look like:
#   "Cleveland Cavaliers vs. Detroit Pistons - Total Over/Under 215.5"
# or outcomes look like "Over 215.5" / "Under 215.5".
_TOTAL_LINE_RE = re.compile(r"\b(?:o|over|u|under|total)\s*(\d+(?:\.\d+)?)\b", re.IGNORECASE)
# Spread line pattern. Outcomes look like "Cleveland Cavaliers -7.5"
# or titles include "Spread" with the line. Handles both `-7.5` and
# `+7.5` forms.
_SPREAD_LINE_RE = re.compile(r"([+-]\d+(?:\.\d+)?)\b")
# Heuristic keyword sets for market type. Polymarket varies these by
# era / sport so we keep them loose.
_TOTAL_KEYWORDS = ("total", "over/under", "over under", "points scored")
_SPREAD_KEYWORDS = ("spread", "handicap")
_ML_KEYWORDS = ("moneyline", "match winner", "to win")


def _classify_market(m: dict, away: str, home: str
                     ) -> tuple[str, float | None, str | None] | None:
    """Identify a Polymarket market as ml/spread/total and extract its
    line + side. Returns (market_type, line, side) or None.

      market_type: "ml" | "spread" | "total"
      line:        float for spread/total, None for ml
      side:        "home" | "away" for ml/spread, "over" | "under" for total

    Heuristic — Polymarket's market titles vary, so we look at title +
    outcome + team name together. Returns None when we can't confidently
    classify (don't guess on ambiguous markets)."""
    title = (m.get("title") or "").lower()
    outcome = (m.get("outcome") or "").lower()
    team = (m.get("team") or {}).get("name") or ""

    # TOTAL — title or outcome mentions over/under
    if any(k in title for k in _TOTAL_KEYWORDS) or outcome.startswith(("over ", "under ", "o ", "u ")):
        mt = _TOTAL_LINE_RE.search(outcome) or _TOTAL_LINE_RE.search(title)
        if not mt:
            return None
        line = float(mt.group(1))
        if outcome.startswith(("over", "o ")) or "over" in outcome:
            side = "over"
        elif outcome.startswith(("under", "u ")) or "under" in outcome:
            side = "under"
        else:
            return None
        return ("total", line, side)

    # SPREAD — title says "spread" or outcome contains a signed point
    spread_kw = any(k in title for k in _SPREAD_KEYWORDS)
    spread_pt = _SPREAD_LINE_RE.search(outcome) or _SPREAD_LINE_RE.search(title)
    if spread_kw or (spread_pt and team):
        if not spread_pt:
            return None
        line = float(spread_pt.group(1))
        # Side = which team's spread this is. Match outcome's team
        # field against home/away.
        if team and _name_match(team, home):
            side = "home"
        elif team and _name_match(team, away):
            side = "away"
        else:
            # Title-based fallback
            if _name_match(outcome, home):
                side = "home"
            elif _name_match(outcome, away):
                side = "away"
            else:
                return None
        return ("spread", line, side)

    # MONEYLINE — outcome is a team name with no point spread embedded.
    # No keyword required — PMM moneyline market titles vary widely
    # (often just "Cavaliers vs Pistons"). If a market has a team and
    # NO numeric line, we treat it as ML.
    if team and not _SPREAD_LINE_RE.search(outcome) and not _TOTAL_LINE_RE.search(outcome):
        if _name_match(team, home):
            return ("ml", None, "home")
        if _name_match(team, away):
            return ("ml", None, "away")

    return None


# ──────────────────────────── BBO fetch ────────────────────────────

def _get_bbo(client, slug: str) -> dict | None:
    """Fetch best bid/ask for a market slug. Caches 30s.

    Returns {bid: float|None, ask: float|None, mid: float|None,
             bid_american: int|None, ask_american: int|None,
             mid_american: int|None}
    or None on failure (silent — caller falls back gracefully).
    """
    cached = _BBO_CACHE.get(slug)
    if cached and (time.time() - cached[0]) < _BBO_CACHE_TTL_SEC:
        return cached[1]
    try:
        resp = client.markets.bbo(slug)
    except Exception as e:
        log.debug("bbo %s failed: %s", slug, e)
        _BBO_CACHE[slug] = (time.time(), None)
        return None

    def field(k):
        return resp.get(k) if isinstance(resp, dict) else getattr(resp, k, None)

    bid = _safe_amount(field("bestBid"))
    ask = _safe_amount(field("bestAsk"))
    mid = ((bid + ask) / 2) if (bid is not None and ask is not None) else (bid or ask)
    out = {
        "bid": bid,
        "ask": ask,
        "mid": mid,
        "bid_american":  _prob_to_american(bid) if bid is not None else None,
        "ask_american":  _prob_to_american(ask) if ask is not None else None,
        "mid_american":  _prob_to_american(mid) if mid is not None else None,
    }
    _BBO_CACHE[slug] = (time.time(), out)
    return out


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
    }
    classify_diag = [] if diag is not None else None
    for m in ev["markets"]:
        # Skip markets that have closed (settled, expired, etc.) — we
        # only want live tradeable markets for the limit-order target.
        if m.get("closed") or not m.get("active"):
            continue
        result = _classify_market(m, away, home)
        if classify_diag is not None:
            classify_diag.append({
                "slug":       m.get("slug"),
                "title":      m.get("title"),
                "outcome":    m.get("outcome"),
                "team":       (m.get("team") or {}).get("name"),
                "classified": result,
            })
        if not result:
            continue
        mt, line, side = result
        entry = {
            "side":  side,
            "line":  line,
            "slug":  m.get("slug"),
            "title": m.get("title"),
        }
        if with_bbo and m.get("slug"):
            entry["quote"] = _get_bbo(client, m["slug"])
        out[mt].append(entry)

    if classify_diag is not None:
        diag["markets_classified"] = classify_diag
        diag["counts"] = {k: len(out.get(k, [])) for k in ("ml", "spread", "total")}
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
