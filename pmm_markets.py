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
# LANDMINE (July 2026): this is the GENERAL event-search cache used by
# EVERY lookup() — it was accidentally deleted alongside the adjacent
# _WC_CACHE in the soccer kill, which made every lookup raise NameError
# pre-HTTP (silently caught → the whole PMM feed went dark: live-tracker
# rings grey, dossier polymarket blocks empty, pm-snapshot logging no
# PMM rows). Do not remove.
_EVENT_CACHE: dict[str, tuple[float, dict | None]] = {}
_EVENT_CACHE_TTL_SEC = 5 * 60


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
    # Tricode as a whole TOKEN ('nym', 'phi') — PMM's July 2026 abbreviated
    # format. ≥3 chars only ('ny' as a token is too collision-prone);
    # token-bounded so 'phi' never fires inside 'philadelphia' (harmless
    # there anyway — the city variant already matched earlier).
    padded = " " + q_norm + " "
    for code in _team_code_cands(team):
        if len(code) < 3:
            continue
        p = padded.find(f" {code} ")
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



def _team_code_cands(team: str, sport: str | None = None) -> set[str]:
    """Candidate 2-4 letter tricodes for a team, lowercase — 'New York
    Mets' → {'nym','ny','new'}, 'Philadelphia Phillies' → {'phi','pp',...}.
    Needed since PMM's July 2026 schema deploy retitled near-term events
    to tricodes ('NYM vs PHI') and slugs ('mlb-nym-phi-2026-07-16').
    Primary source: app.py's _TEAM_TO_KALSHI (standard tricodes, verified
    live by the Kalshi matcher; late import — circular-safe, and a miss
    just means heuristics only). Heuristic initial/prefix patterns cover
    unmapped teams; a wrong candidate can only fail to match (Pass 0
    requires BOTH teams' codes in their slots), never mis-match."""
    cands: set[str] = set()
    if sport:
        try:
            from app import _TEAM_TO_KALSHI as _K   # late import
            tn = _norm(team)
            for mascot, code in (_K.get(sport.upper()) or {}).items():
                if mascot in tn:
                    cands.add(code.lower())
                    break
        except Exception:
            pass
    parts = _norm(team).split()
    if parts:
        cands.add("".join(p[0] for p in parts))          # nym / gsw / sas
        cands.add(parts[0][:3])                          # phi / bos / tor
        if len(parts) >= 2:
            cands.add("".join(p[0] for p in parts[:-1]))  # ny / la / tb
            if len(parts) >= 3:
                cands.add(parts[0][:2] + parts[1][0])     # okc
    return {c for c in cands if 2 <= len(c) <= 4}


# PMM game-event slug: '{league}-{away}-{home}-{YYYY-MM-DD}' (observed live
# July 2026: 'mlb-nym-phi-2026-07-18'). The date is the US/Eastern game date.
_PMM_SLUG_RE = re.compile(r"^[a-z0-9]+-([a-z0-9]{2,4})-([a-z0-9]{2,4})-(\d{4}-\d{2}-\d{2})(?:-\d+)?$")


def _ev_get(ev, name, default=None):
    return ev.get(name, default) if isinstance(ev, dict) else getattr(ev, name, default)


def _bet_et_date(bet_dt: datetime | None):
    """Our event_start (UTC) → US/Eastern calendar date (PMM's slug-date
    convention — a 23:00Z start is a 7pm ET game, same ET date)."""
    if bet_dt is None:
        return None
    try:
        from zoneinfo import ZoneInfo
        return bet_dt.astimezone(ZoneInfo("America/New_York")).date()
    except Exception:
        return bet_dt.date()


def _ev_date_ok(ev, bet_dt: datetime | None) -> bool:
    """Date guard: reject an event that provably belongs to a DIFFERENT
    calendar day than our game. THE July 2026 landmine this kills: PMM
    lists a whole series days ahead, so name-only matching grabbed the
    same teams' SATURDAY event for a WEDNESDAY game (sparse markets →
    'no totals/NRFI on Polymarket', and worse, the wrong game's prices
    logged into pm_snapshots). Slug date preferred (exact ET-date match);
    startTime as fallback (±14h). Events with NO extractable date pass —
    guard on evidence, never on absence."""
    if bet_dt is None:
        return True
    mo = _PMM_SLUG_RE.match(str(_ev_get(ev, "slug") or ""))
    if mo:
        et = _bet_et_date(bet_dt)
        if et is not None:
            try:
                return datetime.strptime(mo.group(3), "%Y-%m-%d").date() == et
            except ValueError:
                pass
    st = _ev_get(ev, "startTime")
    if st:
        try:
            sdt = datetime.fromisoformat(str(st).replace("Z", "+00:00"))
            if sdt.tzinfo is None:
                sdt = sdt.replace(tzinfo=timezone.utc)
            return abs((sdt - bet_dt).total_seconds()) <= 14 * 3600
        except Exception:
            pass
    return True


def _match_event_to_game(events: list, away: str, home: str,
                         sport: str | None = None,
                         bet_dt: datetime | None = None) -> Any | None:
    """Walk a list of PMM events looking for one referring to the
    away vs home game. Tries progressively looser matches:
      0. Slug/title tricodes + exact date ('mlb-nym-phi-2026-07-16',
         'NYM vs PHI') — PMM's post-July-2026 format for near-term events
      1. Title contains BOTH full team names
      2. Title contains BOTH team last-tokens (e.g., 'orioles' + 'rays')
      2.5. Title contains both city/region tokens
      3. Any market within the event has team.name matching one of
         our teams AND title mentions the other
    EVERY pass is date-guarded when bet_dt is known (see _ev_date_ok) —
    a series-mate event on another day can never match, whatever its
    title format. Returns the matched event (untouched SDK object) or None."""
    away_last = _last_token(away)
    home_last = _last_token(home)
    # Pass 0: tricode slug/title + date. The slug encodes away/home ORDER
    # (MLB league ordering 'away' — 'mlb-nym-phi' = NYM @ PHI) but we
    # accept the swapped orientation too (PMM orders some leagues home-
    # first); requiring BOTH codes keeps it unambiguous either way.
    a_cands = _team_code_cands(away, sport)
    h_cands = _team_code_cands(home, sport)
    if a_cands and h_cands:
        for ev in events:
            if not _ev_date_ok(ev, bet_dt):
                continue
            mo = _PMM_SLUG_RE.match(str(_ev_get(ev, "slug") or ""))
            codes = None
            if mo:
                codes = {mo.group(1), mo.group(2)}
            else:
                toks = set(_norm(_ev_get(ev, "title") or "").split())
                codes = {t for t in toks if 2 <= len(t) <= 4}
            if (codes and any(c in a_cands for c in codes)
                    and any(c in h_cands for c in codes)
                    # one shared token can't satisfy both teams
                    and len(codes & (a_cands | h_cands)) >= 2):
                return ev
    for ev in events:
        if not _ev_date_ok(ev, bet_dt):
            continue
        title = _ev_get(ev, "title") or ""
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
            if not _ev_date_ok(ev, bet_dt):
                continue
            title = _ev_get(ev, "title") or ""
            t_norm = _norm(title)
            if away_city in t_norm and home_city in t_norm:
                return ev
    for ev in events:
        if not _ev_date_ok(ev, bet_dt):
            continue
        markets = _ev_get(ev, "markets") or []
        title = _ev_get(ev, "title") or ""
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
        m = _match_event_to_game(events, away, home, sport=sport, bet_dt=bet_dt)
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
                matched = _match_event_to_game(search_events, away, home,
                                               sport=sport, bet_dt=bet_dt)
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
# TYPE_TOTAL too — we exclude those via a v1 BLOCKLIST (see below).
_MARKET_TYPE_BUCKET: dict[str, str] = {
    "SPORTS_MARKET_TYPE_MONEYLINE": "ml",
    "SPORTS_MARKET_TYPE_SPREAD":    "spread",
    "SPORTS_MARKET_TYPE_TOTAL":     "total",
}
# v1 (sportsMarketType) substrings that mark a NON-full-game variant sharing
# the same V2 bucket (first-five-innings, first-inning, halves, quarters,
# periods, player props). We take everything in the V2 bucket EXCEPT these.
# BLOCKLIST, not allowlist (June 2026 landmine): Polymarket renamed the v1
# slugs from short 'spreads'/'totals'/'moneyline' to
# 'baseball_team_full_game_spread' / '..._total', which silently broke the old
# exact-match allowlist and zeroed ALL ml/spread/total PMM data (pm-snapshot +
# live dossier). A blocklist survives their renames; only genuinely-new variant
# names need adding here.
_VARIANT_MARKERS: tuple[str, ...] = (
    "first_five", "first_inning", "inning",
    "first_half", "second_half", "_half",
    "quarter", "period", "player",
)


def _classify_market(m: dict, away: str, home: str
                     ) -> tuple[str, float | None, str | None] | None:
    """Identify a Polymarket market as ml/spread/total and extract its
    line + side. Returns (market_type, line, side) or None.

      market_type: "ml" | "spread" | "total"
      line:        float for spread/total, None for ml
      side:        "home" | "away" for ml/spread, "over" | "under" for total

    Uses the real API schema (post-discovery May 2026; v1 slugs renamed by
    Polymarket June 2026 → blocklist not allowlist):
      sportsMarketTypeV2: 'SPORTS_MARKET_TYPE_MONEYLINE' / '_SPREAD' / '_TOTAL'
      sportsMarketType:   v1 slug, e.g. 'baseball_team_full_game_spread'
                          (variants: '..._first_five_spread', player props)
      line:               numeric line (e.g. 4.5)
      question:           human-readable like "Spread: Baltimore Orioles (+4.5)"
                          → encodes which team is the YES side
    """
    v2 = m.get("sportsMarketTypeV2") or ""
    v1 = (m.get("sportsMarketType") or "").lower()
    bucket = _MARKET_TYPE_BUCKET.get(v2)
    if not bucket:
        return None
    # Skip sub-period / prop variants (first-five, first-inning, halves, etc.)
    # — same V2 bucket, but not the full-game main line.
    if any(mk in v1 for mk in _VARIANT_MARKERS):
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


def _classify_ufc_distance(m: dict) -> str | None:
    """UFC 'go the distance' prop sniff (Fight IQ P3) — lives outside the
    ml/spread/total buckets, exactly like NRFI. YES = the fight reaches
    the final bell. Question shape on PMM: 'Will X vs Y go the
    distance?' — UNVERIFIED from the sandbox (same posture as the NRFI
    schema); the diag.all_markets dump is ground truth if it misses."""
    # ONLY the true "go the distance" market. Polymarket ALSO lists a separate
    # "Will X vs Y go to a decision, draw, or no contest?" market — a different
    # line with a different price; matching it here put TWO markets in
    # ufc_distance and the model priced against whichever came first (Kamaka/
    # Riley: the decision market at mid 0.545 instead of go-the-distance 0.585).
    q = (m.get("question") or "").lower()
    return "yes" if "go the distance" in q else None


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
        "ufc_distance": [],
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
            # Not a primary ml/spread/total — try the derivative sniffs
            # (NRFI first-inning; UFC distance) before giving up.
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
            ds = _classify_ufc_distance(m)
            if ds:
                quote = _get_market_quote(m) if with_bbo else None
                out["ufc_distance"].append({"side": "yes", "line": None,
                                            "slug": m.get("slug"),
                                            "title": m.get("question"),
                                            "quote": quote})
                out["ufc_distance"].append({"side": "no", "line": None,
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


