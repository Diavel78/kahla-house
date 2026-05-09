"""Pick Bot — Flask-side dossier builder.

This is a port of `kahla-scanner/scripts/handicapper.py`. The scanner
subproject doesn't ship with Vercel (only app.py does), so the dossier
logic is duplicated here as a self-contained module Flask can call.

Differences vs the scanner version:
  • Uses `requests` (already a Flask dep) instead of `httpx`.
  • Takes a Supabase client as a parameter instead of importing one —
    Flask already manages a singleton via app.py:get_supabase().
  • Math helpers (american_to_prob, devig_two_way, sharp-side / score)
    are reimplemented inline. Tiny — kept here rather than imported so
    the module is single-file portable.

Same dossier shape, same matching/devig/sharp-score rules. If the rules
change, change them in BOTH this file AND
`kahla-scanner/scripts/handicapper.py`. The kahla-scanner version is
authoritative — it backs the live picker logic that's been running.
"""
from __future__ import annotations

import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import requests

log = logging.getLogger(__name__)

ALLOWED_BOOKS = {
    "PIN", "DK", "FD", "MGM", "CAE", "HR", "BET365",
    "BR", "BOL", "LV", "BVD", "ESPN", "FAN", "MB",
}
ENTRY_BOOKS = ALLOWED_BOOKS - {"PIN"}

# Odds API mappings — duplicated from kahla-scanner/scrapers/odds_api.py
# so handicapper_web (the only Vercel-deployed module) can hit the live
# endpoint when the user clicks "Pick". Keep in sync with the ingester
# if a new book or sport is added.
ODDS_API_BASE = "https://api.the-odds-api.com/v4"
SPORT_KEYS = {
    "MLB":   "baseball_mlb",
    "NBA":   "basketball_nba",
    "NHL":   "icehockey_nhl",
    "NFL":   "americanfootball_nfl",
    "CBB":   "basketball_ncaab",
    "NCAAF": "americanfootball_ncaaf",
    "UFC":   "mma_mixed_martial_arts",
}
BOOK_CODES = {
    "pinnacle":      "PIN",
    "draftkings":    "DK",
    "fanduel":       "FD",
    "betmgm":        "MGM",
    "caesars":       "CAE",
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
LIVE_MATCH_WINDOW_MIN = 30

# Per-sport event-list cache for the live Odds API fetch. When a user
# rapid-clicks several games in the same sport within seconds, we don't
# need to re-hit the API for each click — the events list barely
# changes minute-to-minute. Cache the full /odds response per sport for
# 60s and reuse it on subsequent clicks. Each cache hit saves 6 credits.
#
# Module-level dict: survives across requests on the same warm Vercel
# container. Cold starts reset it (fine — pays one extra API call once,
# then the cache builds back up).
_LIVE_EVENTS_CACHE: dict[str, tuple[float, list]] = {}
_LIVE_CACHE_TTL_SEC = 60

_ESPN_PATH: dict[str, tuple[str, str]] = {
    "MLB":   ("baseball",   "mlb"),
    "NBA":   ("basketball", "nba"),
    "NHL":   ("hockey",     "nhl"),
    "NFL":   ("football",   "nfl"),
    "CBB":   ("basketball", "mens-college-basketball"),
    "NCAAF": ("football",   "college-football"),
}
_ACTION_LEAGUE: dict[str, str] = {
    "MLB":   "mlb",
    "NBA":   "nba",
    "NHL":   "nhl",
    "NFL":   "nfl",
    "CBB":   "ncaab",
    "NCAAF": "ncaaf",
}

HTTP_TIMEOUT = 10
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)


# ──────────────────────────── Math helpers ────────────────────────────

def _american_to_prob(price: int) -> float:
    if price == 0:
        raise ValueError("American price cannot be 0")
    if price > 0:
        return 100.0 / (price + 100.0)
    return -price / (-price + 100.0)


def _prob_to_american(prob: float | None) -> int | None:
    if prob is None or not (0 < prob < 1):
        return None
    if prob >= 0.5:
        return int(round(-prob / (1 - prob) * 100))
    return int(round((1 - prob) / prob * 100))


def _devig_two_way(p_a: float, p_b: float) -> float:
    total = p_a + p_b
    if total <= 0:
        raise ValueError("Sum of probs must be > 0")
    return p_a / total


def _amer_to_cents(p: Any) -> float | None:
    if p is None:
        return None
    try:
        p = float(p)
    except (TypeError, ValueError):
        return None
    if p < 0:
        return -p - 100
    if p > 0:
        return -(p - 100)
    return 0


def _move_score_ml(opener_amer: Any, current_amer: Any) -> int | None:
    o = _amer_to_cents(opener_amer)
    c = _amer_to_cents(current_amer)
    if o is None or c is None:
        return None
    return min(10, round(abs(c - o)))


def _move_score_spr_tot(opener_line: Any, current_line: Any,
                        opener_price: Any, current_price: Any) -> int | None:
    if opener_price is None or current_price is None:
        return None
    pt = abs((current_line or 0) - (opener_line or 0))
    if pt > 0:
        return min(10, round(pt * 10))
    px = abs(current_price - opener_price)
    return min(10, round(px))


def _sharp_for_ml(home_op, home_cu, away_op, away_cu) -> tuple | None:
    h_diff = (home_cu["price_american"] - home_op["price_american"]) if (home_op and home_cu) else None
    a_diff = (away_cu["price_american"] - away_op["price_american"]) if (away_op and away_cu) else None
    if h_diff is not None and a_diff is not None:
        if h_diff == a_diff:
            return None
        if h_diff < a_diff:
            side, op, cu = "home", home_op, home_cu
        else:
            side, op, cu = "away", away_op, away_cu
    elif h_diff is not None:
        if h_diff < 0:
            side, op, cu = "home", home_op, home_cu
        else:
            return None
    elif a_diff is not None:
        if a_diff < 0:
            side, op, cu = "away", away_op, away_cu
        else:
            return None
    else:
        return None
    score = _move_score_ml(op["price_american"], cu["price_american"])
    if score is None:
        return None
    return side, score, op, cu


def _sharp_for_spread(h_op, h_cu, a_op, a_cu) -> tuple | None:
    h_avail = bool(h_op and h_cu)
    a_avail = bool(a_op and a_cu)
    if not (h_avail or a_avail):
        return None

    if h_avail and a_avail:
        h_pt = (h_cu.get("line") or 0) - (h_op.get("line") or 0)
        a_pt = (a_cu.get("line") or 0) - (a_op.get("line") or 0)
        side = None
        if abs(h_pt - a_pt) >= 0.5:
            side = "home" if h_pt < a_pt else "away"
        else:
            h_px = h_cu["price_american"] - h_op["price_american"]
            a_px = a_cu["price_american"] - a_op["price_american"]
            if abs(h_px - a_px) >= 1:
                side = "home" if h_px < a_px else "away"
        if not side:
            return None
        op, cu = (h_op, h_cu) if side == "home" else (a_op, a_cu)
    else:
        # One-sided fallback: derive movement from whichever side has a
        # complete pair. Sharp side = the team whose spread got HARDER.
        # ref line tightened (e.g. -1.5 → -2)   → ref harder
        # ref line eased     (e.g. -1.5 → -1)   → ref easier (other harder)
        # ref vig more negative                 → ref harder
        # ref vig less negative                 → ref easier
        ref_op, ref_cu = (h_op, h_cu) if h_avail else (a_op, a_cu)
        ref_is_home = h_avail
        pt_diff = (ref_cu.get("line") or 0) - (ref_op.get("line") or 0)
        px_diff = ref_cu["price_american"] - ref_op["price_american"]
        if abs(pt_diff) >= 0.5:
            ref_harder = pt_diff < 0
        elif abs(px_diff) >= 1:
            ref_harder = px_diff < 0
        else:
            return None
        if ref_is_home:
            side = "home" if ref_harder else "away"
        else:
            side = "away" if ref_harder else "home"
        op, cu = ref_op, ref_cu  # only ref-side snapshots available

    score = _move_score_spr_tot(op.get("line"), cu.get("line"),
                                 op["price_american"], cu["price_american"])
    if score is None:
        return None
    return side, score, op, cu


def _sharp_for_total(o_op, o_cu, u_op, u_cu) -> tuple | None:
    o_avail = bool(o_op and o_cu)
    u_avail = bool(u_op and u_cu)
    if not (o_avail or u_avail):
        return None

    # Reference snapshot: prefer over (most totals are over-quoted in
    # our data), fall back to under. Line direction reads the same from
    # either side; vig direction is INVERTED (under getting more
    # expensive means UNDER is sharp, mirror of over).
    if o_avail:
        ref_op, ref_cu = o_op, o_cu
        ref_is_over = True
    else:
        ref_op, ref_cu = u_op, u_cu
        ref_is_over = False

    pt_diff = (ref_cu.get("line") or 0) - (ref_op.get("line") or 0)
    px_diff = ref_cu["price_american"] - ref_op["price_american"]

    if pt_diff > 0:
        side = "over"   # line raised → over needs more runs → sharp OVER
    elif pt_diff < 0:
        side = "under"  # line lowered → under has less room → sharp UNDER
    elif px_diff < 0:
        # Reference side got more expensive (harder).
        side = "over" if ref_is_over else "under"
    elif px_diff > 0:
        # Reference side got cheaper (easier) → other side is sharp.
        side = "under" if ref_is_over else "over"
    else:
        return None

    score = _move_score_spr_tot(ref_op.get("line"), ref_cu.get("line"),
                                 ref_op["price_american"], ref_cu["price_american"])
    if score is None:
        return None

    # Prefer sharp-side snapshots for the display pair when available.
    if side == "over" and o_avail:
        return side, score, o_op, o_cu
    if side == "under" and u_avail:
        return side, score, u_op, u_cu
    return side, score, ref_op, ref_cu


# ──────────────────────────── Match resolution ────────────────────────────

def _split_event_name(name: str) -> tuple[str | None, str | None]:
    if " @ " in name:
        a, h = name.split(" @ ", 1)
        return a.strip(), h.strip()
    return None, None


def _parse_query(query: str) -> list[str]:
    q = re.sub(r"[?!.,]", " ", query.lower())
    q = re.sub(r"\b(today|tonight|tomorrow|thoughts|pick|game|vs|versus|v|@|at|vs\.|on)\b",
               " ", q)
    q = re.sub(r"\s+", " ", q).strip()
    parts = [p.strip() for p in re.split(r"\s+vs?\s+|\s+@\s+", q) if p.strip()]
    if len(parts) >= 2:
        return parts
    return [w for w in q.split() if len(w) >= 3]


def _team_score(team_name: str, tokens: list[str]) -> int:
    n = team_name.lower()
    return sum(1 for t in tokens if t in n)


def _find_market(sb, query: str, sport_hint: str | None
                 ) -> tuple[dict | None, list[dict]]:
    tokens = _parse_query(query)
    if not tokens:
        return None, []
    now = datetime.now(timezone.utc)
    after = (now - timedelta(minutes=90)).isoformat()
    before = (now + timedelta(hours=48)).isoformat()
    q = (sb.table("markets")
         .select("id,sport,event_name,event_start,status")
         .eq("status", "active")
         .gte("event_start", after)
         .lte("event_start", before)
         .order("event_start"))
    if sport_hint:
        q = q.eq("sport", sport_hint.upper())
    rows = q.limit(500).execute().data or []
    scored = []
    for m in rows:
        away, home = _split_event_name(m.get("event_name") or "")
        if not (away and home):
            continue
        s = _team_score(away, tokens) + _team_score(home, tokens)
        if s == 0:
            continue
        scored.append((s, m))
    scored.sort(key=lambda x: (-x[0], x[1]["event_start"]))
    if not scored:
        return None, []
    return scored[0][1], [m for _, m in scored[:5]]


# ──────────────────────────── Snapshots / odds ────────────────────────────

def _latest_snapshots(sb, market_id: str) -> dict:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    rows = (sb.table("book_snapshots")
            .select("book,market_type,side,price_american,line,captured_at")
            .eq("market_id", market_id)
            .gte("captured_at", cutoff)
            .order("captured_at", desc=True)
            .limit(5000)
            .execute().data) or []
    out: dict = {}
    for r in rows:
        if r["book"] not in ALLOWED_BOOKS:
            continue
        key = (r["book"], r["market_type"], r["side"])
        if key not in out:
            out[key] = r
    return out


def _pin_opener(sb, market_id: str) -> dict:
    rows = (sb.table("book_snapshots")
            .select("market_type,side,price_american,line,captured_at")
            .eq("market_id", market_id)
            .eq("book", "PIN")
            .order("captured_at")
            .limit(1000)
            .execute().data) or []
    out: dict = {}
    for r in rows:
        key = (r["market_type"], r["side"])
        if key not in out:
            out[key] = r
    return out


# ──────────────────────── Live Odds API fetch ────────────────────────

def _odds_api_key() -> str | None:
    return (os.getenv("ODDS_API_KEY") or "").strip() or None


def _fetch_live_event(sport: str, event_start_iso: str,
                      away: str, home: str) -> tuple[dict | None, str | None]:
    """Hit The Odds API and return (event_payload, error_msg).

    Returns (event_dict, None) on success, (None, "reason string") on
    any failure. Error string is surfaced to the dossier so the UI can
    show why the live fetch fell back to cached.

    Cost: 6 credits per call (3 markets × 2 regions). Same call shape as
    the cron — gets all events for the sport, we filter to the one game.
    """
    sport_key = SPORT_KEYS.get(sport)
    if not sport_key:
        return None, f"sport {sport} not in SPORT_KEYS"
    api_key = _odds_api_key()
    if not api_key:
        return None, "ODDS_API_KEY not set in Vercel env"
    try:
        bet_dt = datetime.fromisoformat(event_start_iso.replace("Z", "+00:00"))
    except Exception as e:
        return None, f"bad event_start: {e}"
    # Cache hit? Reuse the events list, skip the API call. Saves 6
    # credits per click within the TTL window. The user's flow of
    # rapid-clicking several games in the same sport benefits the most.
    cached = _LIVE_EVENTS_CACHE.get(sport)
    cache_age: float | None = None
    if cached and (time.time() - cached[0]) < _LIVE_CACHE_TTL_SEC:
        events = cached[1]
        cache_age = time.time() - cached[0]
    else:
        url = f"{ODDS_API_BASE}/sports/{sport_key}/odds"
        params = {
            "api_key":    api_key,
            "regions":    "us,eu",
            "markets":    "h2h,spreads,totals",
            "oddsFormat": "american",
            "dateFormat": "iso",
        }
        try:
            r = requests.get(url, params=params, timeout=12)
        except Exception as e:
            return None, f"http exception: {str(e)[:120]}"
        if r.status_code != 200:
            return None, f"http {r.status_code}: {r.text[:120]}"
        try:
            events = r.json() or []
        except Exception as e:
            return None, f"bad json: {e}"
        _LIVE_EVENTS_CACHE[sport] = (time.time(), events)

    away_n, home_n = (away or "").lower(), (home or "").lower()
    # UFC: cards span ~5-6h with each fight having its own commence_time
    # in The Odds API (different from the single card-start time we have
    # in markets.event_start). Use a wider window so we still match the
    # individual fight no matter how late it starts on the card.
    window_min = 360 if sport == "UFC" else LIVE_MATCH_WINDOW_MIN
    window = timedelta(minutes=window_min)

    def _name_match(ev_name: str, our_name: str) -> bool:
        # Substring containment in either direction handles the common
        # case (full name match). UFC fallback: any token of length ≥ 3
        # in our name appears in the API name. Catches diacritics,
        # nickname differences, partial spellings (B. Susurkaev vs
        # Baysangur Susurkaev, etc.).
        if not (ev_name and our_name):
            return False
        if our_name in ev_name or ev_name in our_name:
            return True
        if sport == "UFC":
            tokens = [t for t in re.split(r"\s+", our_name) if len(t) >= 3]
            return any(t in ev_name for t in tokens)
        return False

    for ev in events:
        ev_home = (ev.get("home_team") or "").lower()
        ev_away = (ev.get("away_team") or "").lower()
        if not ev_home or not ev_away:
            continue
        if not (_name_match(ev_home, home_n) and _name_match(ev_away, away_n)):
            continue
        try:
            ev_dt = datetime.fromisoformat(
                (ev.get("commence_time") or "").replace("Z", "+00:00"))
        except Exception:
            continue
        if abs((ev_dt - bet_dt).total_seconds()) > window.total_seconds():
            continue
        return ev, None
    # No match — surface sample events from the response so we can
    # diagnose without re-hitting the API. Most failures are name
    # mismatches (Odds API returns slight variants) or commence_time
    # drift past our window.
    if not events:
        return None, f"odds API returned 0 events for {sport}"
    samples = ", ".join(
        f"{(e.get('away_team') or '?')}@{(e.get('home_team') or '?')}"
        for e in events[:3]
    )
    return None, f"no match in {len(events)} events. samples: {samples}"


def _live_event_to_latest(ev: dict) -> dict:
    """Translate a single Odds API event payload into the same `latest`
    shape `_latest_snapshots` builds: {(book, market_type, side): snapshot}.

    Markets/sides we care about:
      h2h     → moneyline / home, away (price only, no line)
      spreads → spread / home, away (price + line)
      totals  → total / over, under (price + line)
    """
    out: dict = {}
    home_name = (ev.get("home_team") or "").lower()
    away_name = (ev.get("away_team") or "").lower()
    captured  = datetime.now(timezone.utc).isoformat()

    for bk in ev.get("bookmakers") or []:
        bk_key = (bk.get("key") or "").lower()
        short = BOOK_CODES.get(bk_key)
        if not short or short not in ALLOWED_BOOKS:
            continue
        for mk in bk.get("markets") or []:
            mk_key = mk.get("key")
            if mk_key == "h2h":
                mt = "moneyline"
            elif mk_key == "spreads":
                mt = "spread"
            elif mk_key == "totals":
                mt = "total"
            else:
                continue
            for oc in mk.get("outcomes") or []:
                name  = (oc.get("name") or "").strip()
                price = oc.get("price")
                line  = oc.get("point")
                if price is None:
                    continue
                if mt == "total":
                    side = name.lower()  # "Over" / "Under"
                    if side not in ("over", "under"):
                        continue
                else:
                    nlc = name.lower()
                    if nlc == home_name or home_name and home_name in nlc:
                        side = "home"
                    elif nlc == away_name or away_name and away_name in nlc:
                        side = "away"
                    else:
                        continue
                try:
                    price = int(round(float(price)))
                except (TypeError, ValueError):
                    continue
                line_val = None
                if line is not None:
                    try:
                        line_val = float(line)
                    except (TypeError, ValueError):
                        line_val = None
                out[(short, mt, side)] = {
                    "book":           short,
                    "market_type":    mt,
                    "side":           side,
                    "price_american": price,
                    "line":           line_val,
                    "captured_at":    captured,
                }
    return out


def _build_market_block(market_type: str, sides: tuple[str, str],
                        latest: dict, pin_opener: dict) -> dict:
    a, b = sides
    pin_a = latest.get(("PIN", market_type, a))
    pin_b = latest.get(("PIN", market_type, b))
    op_a  = pin_opener.get((market_type, a))
    op_b  = pin_opener.get((market_type, b))

    fair_a = fair_b = None
    if pin_a and pin_b:
        line_match = True
        if market_type == "spread":
            la, lb = pin_a.get("line"), pin_b.get("line")
            line_match = la is not None and lb is not None and abs(la + lb) < 0.001
        elif market_type == "total":
            line_match = pin_a.get("line") == pin_b.get("line")
        if line_match:
            try:
                p_a = _american_to_prob(int(pin_a["price_american"]))
                p_b = _american_to_prob(int(pin_b["price_american"]))
                fair_a = _devig_two_way(p_a, p_b)
                fair_b = 1.0 - fair_a
            except (ValueError, TypeError):
                pass

    if market_type == "moneyline":
        sr = _sharp_for_ml(
            pin_opener.get(("moneyline", "home")),
            latest.get(("PIN", "moneyline", "home")),
            pin_opener.get(("moneyline", "away")),
            latest.get(("PIN", "moneyline", "away")),
        )
    elif market_type == "spread":
        sr = _sharp_for_spread(
            pin_opener.get(("spread", "home")),
            latest.get(("PIN", "spread", "home")),
            pin_opener.get(("spread", "away")),
            latest.get(("PIN", "spread", "away")),
        )
    else:
        sr = _sharp_for_total(
            pin_opener.get(("total", "over")),
            latest.get(("PIN", "total", "over")),
            pin_opener.get(("total", "under")),
            latest.get(("PIN", "total", "under")),
        )

    movement = None
    if sr:
        side, score, op, cu = sr
        movement = {
            "sharp_side":  side,
            "sharp_score": score,
            "opener_price": op["price_american"],
            "opener_line":  op.get("line"),
            "current_price": cu["price_american"],
            "current_line":  cu.get("line"),
        }

    def _best_entry(side: str, fair_prob: float | None) -> dict | None:
        target_line = None
        pin_snap = pin_a if side == a else pin_b
        if pin_snap and market_type != "moneyline":
            target_line = pin_snap.get("line")
        best = None
        for book in ENTRY_BOOKS:
            snap = latest.get((book, market_type, side))
            if not snap:
                continue
            if market_type != "moneyline" and target_line is not None:
                if snap.get("line") != target_line:
                    continue
            price = snap.get("price_american")
            if price is None:
                continue
            if best is None or price > best["price_american"]:
                best = {
                    "book": book,
                    "price_american": int(price),
                    "line": snap.get("line"),
                }
        if best and fair_prob is not None:
            try:
                implied = _american_to_prob(best["price_american"])
                best["edge_pp"] = round((fair_prob - implied) * 100, 2)
            except Exception:
                pass
        return best

    all_books = []
    for book in sorted(ALLOWED_BOOKS):
        sa = latest.get((book, market_type, a))
        sb_ = latest.get((book, market_type, b))
        if not (sa or sb_):
            continue
        all_books.append({
            "book": book,
            f"{a}_price": sa["price_american"] if sa else None,
            f"{a}_line":  sa.get("line") if sa else None,
            f"{b}_price": sb_["price_american"] if sb_ else None,
            f"{b}_line":  sb_.get("line") if sb_ else None,
        })

    def _pin_block(snap: dict | None, fp: float | None) -> dict | None:
        if not snap:
            return None
        return {
            "price": int(snap["price_american"]),
            "line":  snap.get("line"),
            "fair_prob":     round(fp, 4) if fp is not None else None,
            "fair_american": _prob_to_american(fp) if fp is not None else None,
        }

    def _opener_block(snap: dict | None) -> dict | None:
        if not snap:
            return None
        return {
            "price": int(snap["price_american"]),
            "line":  snap.get("line"),
            "captured": snap.get("captured_at"),
        }

    return {
        "pin_current": {a: _pin_block(pin_a, fair_a),
                        b: _pin_block(pin_b, fair_b)},
        "pin_opener":  {a: _opener_block(op_a),
                        b: _opener_block(op_b)},
        "movement":    movement,
        "best_entry":  {a: _best_entry(a, fair_a),
                        b: _best_entry(b, fair_b)},
        "all_books":   all_books,
    }


# ──────────────────────────── Splits / ESPN / MLB ────────────────────────────

def _http_get(url: str, **kwargs) -> dict | None:
    try:
        r = requests.get(url, timeout=HTTP_TIMEOUT, **kwargs)
        if r.status_code != 200:
            return None
        return r.json()
    except Exception as e:
        log.warning("http get %s failed: %s", url, e)
        return None


def _fetch_splits(sport: str, away: str, home: str) -> dict | None:
    league = _ACTION_LEAGUE.get(sport)
    if not league:
        return None
    today = datetime.now(ZoneInfo("America/New_York")).strftime("%Y%m%d")
    url = f"https://api.actionnetwork.com/web/v2/scoreboard/{league}"
    headers = {
        "User-Agent": USER_AGENT,
        "Origin":  "https://www.actionnetwork.com",
        "Referer": "https://www.actionnetwork.com/",
    }
    data = _http_get(url, params={"period": "game", "date": today}, headers=headers)
    if not data:
        return None
    games = data.get("games") or []
    away_n, home_n = away.lower(), home.lower()

    def _name(t: dict | None) -> str:
        if not t:
            return ""
        return (t.get("full_name") or t.get("display_name")
                or t.get("short_name") or "").lower()

    for g in games:
        gh, ga = _name(g.get("home_team")), _name(g.get("away_team"))
        if not gh or not ga:
            continue
        if not ((home_n in gh or gh in home_n) and (away_n in ga or ga in away_n)):
            continue
        return _walk_splits(g)
    return None


def _walk_splits(node: Any, depth: int = 0) -> dict | None:
    if depth > 8 or not isinstance(node, (dict, list)):
        return None
    if isinstance(node, dict):
        keys = list(node.keys())
        if any(re.search(r"(bet|ticket|money|handle).*percent", k, re.I)
               for k in keys):
            ab = _pct(node, ["away_bets_percent", "away_tickets_percent"])
            hb = _pct(node, ["home_bets_percent", "home_tickets_percent"])
            am = _pct(node, ["away_money_percent", "away_handle_percent"])
            hm = _pct(node, ["home_money_percent", "home_handle_percent"])
            if any(v is not None for v in (ab, hb, am, hm)):
                sd = round(hm - hb, 1) if (hm is not None and hb is not None) else None
                return {"away_bets": ab, "home_bets": hb,
                        "away_money": am, "home_money": hm,
                        "sharp_diff": sd}
        for v in node.values():
            r = _walk_splits(v, depth + 1)
            if r:
                return r
    elif isinstance(node, list):
        for v in node:
            r = _walk_splits(v, depth + 1)
            if r:
                return r
    return None


def _pct(d: dict, keys: list[str]) -> float | None:
    for k in keys:
        v = d.get(k)
        if v is None:
            continue
        try:
            return round(float(v), 1)
        except (TypeError, ValueError):
            pass
    return None


def _espn_scoreboard(sport: str, date_yyyymmdd: str) -> list:
    pair = _ESPN_PATH.get(sport)
    if not pair:
        return []
    grp, lg = pair
    url = f"https://site.api.espn.com/apis/site/v2/sports/{grp}/{lg}/scoreboard"
    data = _http_get(url, params={"dates": date_yyyymmdd})
    if not data:
        return []
    return data.get("events", []) or []


def _espn_match_event(events: list, away: str, home: str,
                      bet_start: datetime | None) -> dict | None:
    away_n, home_n = away.lower(), home.lower()
    for g in events:
        comp = (g.get("competitions") or [{}])[0]
        cs = comp.get("competitors") or []
        if len(cs) != 2:
            continue
        h = next((c for c in cs if c.get("homeAway") == "home"), cs[0])
        a = next((c for c in cs if c.get("homeAway") == "away"), cs[1])
        h_name = ((h.get("team") or {}).get("displayName") or "").lower()
        a_name = ((a.get("team") or {}).get("displayName") or "").lower()
        if not h_name or not a_name:
            continue
        if not ((home_n in h_name or h_name in home_n) and
                (away_n in a_name or a_name in away_n)):
            continue
        comp_dt_s = comp.get("date") or g.get("date") or ""
        try:
            comp_dt = datetime.fromisoformat(comp_dt_s.replace("Z", "+00:00"))
        except Exception:
            comp_dt = None
        if bet_start and comp_dt:
            if abs((bet_start - comp_dt).total_seconds()) > 90 * 60:
                continue
        return {"event": g, "home": h, "away": a}
    return None


def _team_block(comp_team: dict | None) -> dict:
    if not comp_team:
        return {}
    team = comp_team.get("team") or {}
    records = comp_team.get("records") or []
    overall = next((r for r in records if r.get("type") == "total"), records[0] if records else None)
    return {
        "id":           team.get("id"),
        "name":         team.get("displayName"),
        "abbreviation": team.get("abbreviation"),
        "record":       (overall or {}).get("summary") if overall else None,
        "score":        comp_team.get("score"),
    }


def _espn_team_injuries(sport: str, team_id: str | None) -> list:
    if not team_id:
        return []
    pair = _ESPN_PATH.get(sport)
    if not pair:
        return []
    grp, lg = pair
    url = (f"https://site.web.api.espn.com/apis/site/v2/sports/"
           f"{grp}/{lg}/teams/{team_id}/injuries")
    data = _http_get(url)
    if not data:
        return []
    items = data.get("items", []) or []
    out = []
    for it in items[:25]:
        ath = it.get("athlete") or {}
        out.append({
            "name":   ath.get("displayName") or ath.get("shortName"),
            "pos":    ((ath.get("position") or {}).get("abbreviation")),
            "status": it.get("status"),
            "type":   (it.get("type") or {}).get("description"),
            "detail": it.get("shortComment") or it.get("longComment"),
            "date":   it.get("date"),
        })
    return out


def _espn_team_recent(sport: str, team_id: str | None, n: int = 10) -> list:
    if not team_id:
        return []
    pair = _ESPN_PATH.get(sport)
    if not pair:
        return []
    grp, lg = pair
    url = (f"https://site.web.api.espn.com/apis/site/v2/sports/"
           f"{grp}/{lg}/teams/{team_id}/schedule")
    data = _http_get(url)
    if not data:
        return []
    events = data.get("events", []) or []
    out = []
    for ev in events:
        comp = (ev.get("competitions") or [{}])[0]
        state = ((comp.get("status") or {}).get("type") or {}).get("state")
        if state != "post":
            continue
        cs = comp.get("competitors") or []
        if len(cs) != 2:
            continue
        me = next((c for c in cs if (c.get("team") or {}).get("id") == team_id), None)
        opp = next((c for c in cs if (c.get("team") or {}).get("id") != team_id), None)
        if not (me and opp):
            continue
        try:
            me_score = int(me.get("score") or 0)
            opp_score = int(opp.get("score") or 0)
        except (ValueError, TypeError):
            continue
        result = "W" if me_score > opp_score else ("L" if me_score < opp_score else "T")
        out.append({
            "date":  comp.get("date") or ev.get("date"),
            "vs":    (opp.get("team") or {}).get("abbreviation"),
            "home":  me.get("homeAway") == "home",
            "score": f"{me_score}-{opp_score}",
            "result": result,
        })
    out.sort(key=lambda x: x["date"], reverse=True)
    return out[:n]


def _mlb_probables(event_iso: str, away: str, home: str) -> dict:
    try:
        dt = datetime.fromisoformat(event_iso.replace("Z", "+00:00"))
    except Exception:
        return {}
    date_str = dt.astimezone(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    url = "https://statsapi.mlb.com/api/v1/schedule"
    params = {
        "sportId": 1, "date": date_str,
        "hydrate": "probablePitcher,team",
    }
    data = _http_get(url, params=params)
    if not data:
        return {}
    away_n, home_n = away.lower(), home.lower()
    for d in data.get("dates", []) or []:
        for g in d.get("games", []) or []:
            home_t = ((g.get("teams") or {}).get("home") or {}).get("team", {})
            away_t = ((g.get("teams") or {}).get("away") or {}).get("team", {})
            hn = (home_t.get("name") or "").lower()
            an = (away_t.get("name") or "").lower()
            if not hn or not an:
                continue
            if not ((home_n in hn or hn in home_n) and
                    (away_n in an or an in away_n)):
                continue
            return {
                "venue": (g.get("venue") or {}).get("name"),
                "away":  _mlb_pitcher_block(((g.get("teams") or {}).get("away") or {}).get("probablePitcher")),
                "home":  _mlb_pitcher_block(((g.get("teams") or {}).get("home") or {}).get("probablePitcher")),
                "away_team_id": away_t.get("id"),
                "home_team_id": home_t.get("id"),
            }
    return {}


# ────────────────────────── Team comparison ──────────────────────────
#
# Display-only context for the dossier. Powers a side-by-side "Team
# comparison" block on /handicapper so the user can see season averages
# / recent form / records. Never feeds into _suggest_pick — the market
# (PIN move + splits) is still the signal of record. This is just
# reference data so the user can confirm or counter the bot's read.

def _mlb_team_compare(away_id, home_id, away_label: str, home_label: str) -> dict | None:
    """Build the MLB team-compare block: RPG, ERA, OPS, BA, record.
    Hits MLB Stats API team season stats for hitting + pitching. Two
    calls per team = 4 calls total per dossier when the click is on
    MLB. Free, no auth, no rate-limit issues at our volume."""
    if not (away_id and home_id):
        return None
    season = datetime.now(timezone.utc).year

    def _team(tid: int) -> dict:
        out: dict = {}
        # Hitting: runs, OPS, batting avg, games (for RPG calc).
        url = f"https://statsapi.mlb.com/api/v1/teams/{tid}/stats"
        h = _http_get(url, params={"stats": "season", "group": "hitting", "season": season})
        if h:
            sp = ((h.get("stats") or [{}])[0].get("splits") or [{}])
            s  = (sp[0].get("stat") or {}) if sp else {}
            try:
                games = int(s.get("gamesPlayed") or 0)
                runs  = int(s.get("runs") or 0)
                out["rpg"] = round(runs / games, 2) if games else None
            except Exception:
                out["rpg"] = None
            out["ops"] = _to_float(s.get("ops"))
            out["ba"]  = _to_float(s.get("avg"))
        # Pitching: ERA + record (W-L).
        p = _http_get(url, params={"stats": "season", "group": "pitching", "season": season})
        if p:
            sp = ((p.get("stats") or [{}])[0].get("splits") or [{}])
            s  = (sp[0].get("stat") or {}) if sp else {}
            out["era"] = _to_float(s.get("era"))
            wl = f"{s.get('wins', 0)}-{s.get('losses', 0)}"
            if wl != "0-0":
                out["record"] = wl
        return out

    a = _team(away_id)
    h = _team(home_id)
    if not (a or h):
        return None
    fields = []
    for key, label, fmt, better in (
        ("record", "Record",       "raw", "high"),
        ("rpg",    "Runs / Game",  "2dp", "high"),
        ("era",    "Team ERA",     "2dp", "low"),
        ("ops",    "Team OPS",     "3dp", "high"),
        ("ba",     "Batting Avg",  "3dp", "high"),
    ):
        av, hv = a.get(key), h.get(key)
        if av is None and hv is None:
            continue
        fields.append({"key": key, "label": label,
                       "away": av, "home": hv,
                       "fmt": fmt, "better": better})
    return {
        "away_label": away_label,
        "home_label": home_label,
        "fields":     fields,
    } if fields else None


# ESPN team-statistics endpoint shape:
#   GET site.web.api.espn.com/apis/site/v2/sports/{path}/teams/{id}/statistics
#   → splits.categories[].stats[].{name, displayName, value, displayValue}
# The stat names vary per sport — _ESPN_COMPARE_FIELDS maps each sport
# to a curated 4-5 stat list with our preferred labels and "better
# direction" so the renderer can highlight the team with the edge on
# each row.
_ESPN_COMPARE_FIELDS = {
    "NBA": [
        ("avgPoints",                 "Points / Game",   "1dp", "high"),
        ("avgPointsAgainst",          "Points Allowed",  "1dp", "low"),
        ("fieldGoalPct",              "FG %",            "1dp", "high"),
        ("threePointFieldGoalPct",    "3P %",            "1dp", "high"),
    ],
    "NFL": [
        ("totalPointsPerGame",        "Points / Game",   "1dp", "high"),
        ("avgPointsAgainst",          "Points Allowed",  "1dp", "low"),
        ("yardsPerGame",              "Yards / Game",    "0dp", "high"),
        ("yardsAllowed",              "Yards Allowed",   "0dp", "low"),
    ],
    "NHL": [
        ("avgGoals",                  "Goals / Game",    "2dp", "high"),
        ("avgGoalsAgainst",           "Goals Allowed",   "2dp", "low"),
        ("powerPlayPct",              "Power Play %",    "1dp", "high"),
        ("penaltyKillPct",            "Penalty Kill %",  "1dp", "high"),
    ],
    "NCAAF": [
        ("totalPointsPerGame",        "Points / Game",   "1dp", "high"),
        ("avgPointsAgainst",          "Points Allowed",  "1dp", "low"),
        ("yardsPerGame",              "Yards / Game",    "0dp", "high"),
        ("yardsAllowed",              "Yards Allowed",   "0dp", "low"),
    ],
    "CBB": [
        ("avgPoints",                 "Points / Game",   "1dp", "high"),
        ("avgPointsAgainst",          "Points Allowed",  "1dp", "low"),
        ("fieldGoalPct",              "FG %",            "1dp", "high"),
        ("threePointFieldGoalPct",    "3P %",            "1dp", "high"),
    ],
}


def _espn_team_stats(sport: str, team_id: str | None) -> dict[str, float]:
    """Pull the flat {stat_name: value} map from ESPN's team statistics
    endpoint for a single team. Returns empty dict on any failure
    (offseason / endpoint change / etc.)."""
    if not team_id:
        return {}
    path = _ESPN_PATH.get(sport)
    if not path:
        return {}
    url = f"https://site.web.api.espn.com/apis/site/v2/sports/{path}/teams/{team_id}/statistics"
    data = _http_get(url)
    if not data:
        return {}
    out: dict[str, float] = {}
    splits = (data.get("splits") or {})
    for cat in splits.get("categories") or []:
        for s in cat.get("stats") or []:
            name = s.get("name")
            v = s.get("value")
            if name and v is not None:
                try:
                    out[name] = float(v)
                except (TypeError, ValueError):
                    pass
    return out


def _espn_team_record(team_block: dict | None) -> str | None:
    """Pull "W-L" from the ESPN team block (already part of espn.{home,
    away}). Some sports include OT-loss → "W-L-OTL" stays as-is."""
    if not team_block:
        return None
    rec = team_block.get("record")
    if isinstance(rec, str) and rec:
        return rec
    return None


def _espn_team_compare(sport: str, away_id: str | None, home_id: str | None,
                       away_label: str, home_label: str,
                       away_record: str | None, home_record: str | None) -> dict | None:
    """Build the team-compare block for non-MLB sports. Uses ESPN's team
    statistics endpoint."""
    field_map = _ESPN_COMPARE_FIELDS.get(sport)
    if not (field_map and (away_id or home_id)):
        return None
    a = _espn_team_stats(sport, away_id)
    h = _espn_team_stats(sport, home_id)

    fields = []
    if away_record or home_record:
        fields.append({"key": "record", "label": "Record",
                       "away": away_record, "home": home_record,
                       "fmt": "raw", "better": "high"})

    for stat_name, label, fmt, better in field_map:
        av, hv = a.get(stat_name), h.get(stat_name)
        if av is None and hv is None:
            continue
        fields.append({"key": stat_name, "label": label,
                       "away": av, "home": hv,
                       "fmt": fmt, "better": better})

    return {
        "away_label": away_label,
        "home_label": home_label,
        "fields":     fields,
    } if fields else None


def _to_float(v) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _mlb_pitcher_block(p: dict | None) -> dict:
    if not p:
        return {}
    pid = p.get("id")
    block = {
        "name":   p.get("fullName"),
        "id":     pid,
        # MLB Stats API's `probablePitcher` hydrate doesn't include
        # pitchHand by default — we backfill it from the person endpoint
        # below. Keep this as a first-pass fallback in case the schedule
        # response ever DOES include it.
        "throws": ((p.get("pitchHand") or {}).get("code")),
    }
    if not pid:
        return block
    # Single combined call: person details + season pitching stats.
    # `hydrate=stats(...)` nests the stats into the person response, so
    # we get throws + ERA/WHIP/etc in one round-trip.
    season = datetime.now(timezone.utc).year
    url = f"https://statsapi.mlb.com/api/v1/people/{pid}"
    params = {"hydrate": f"stats(group=[pitching],type=[season],season={season})"}
    data = _http_get(url, params=params)
    if not data:
        return block
    people = data.get("people") or []
    if not people:
        return block
    person = people[0]
    # Pitch hand from the person response — what fixes the "(?)" display.
    if not block["throws"]:
        block["throws"] = ((person.get("pitchHand") or {}).get("code"))
    stats_blocks = person.get("stats") or []
    splits = (stats_blocks[0].get("splits") if stats_blocks else []) or []
    if splits:
        s = splits[0].get("stat") or {}
        block.update({
            "era":      s.get("era"),
            "whip":     s.get("whip"),
            "ip":       s.get("inningsPitched"),
            "k":        s.get("strikeOuts"),
            "bb":       s.get("baseOnBalls"),
            "k_per_9":  s.get("strikeoutsPer9Inn"),
            "bb_per_9": s.get("walksPer9Inn"),
            "hr_per_9": s.get("homeRunsPer9"),
            "record":   f"{s.get('wins', 0)}-{s.get('losses', 0)}",
        })
    return block


# ──────────────────────────── Mechanical pick ────────────────────────────

# Polymarket-execution scoring. The user bets on Polymarket (limit
# orders at fair / sharp prices) — NOT at retail. So we don't filter on
# "+EV at DK/FD" anymore. The signal is:
#   1. Sharp move (PIN opener → current line/price magnitude + side)
#   2. Public splits divergence (% money − % bets on the sharp side)
# Combined score = how much the read agrees with itself. The
# Polymarket entry target = PIN devigged American on the sharp side.
SHARP_SCORE_MIN  = 4   # Sharp signal threshold.
SPLITS_MIN_PP    = 10  # |money% − bets%| considered "material".
SHARP_WEIGHT     = 0.7
SPLITS_WEIGHT    = 0.3


def _splits_signal_pp(splits: dict | None, sharp_side: str | None,
                      market_type: str) -> float:
    """Return the splits-divergence percentage points for the sharp side.
    Positive = money concentrated on this side (sharp signal agrees).
    Negative = public ticket count overweight on this side (square fade).

    Only computable for ML splits (Action Network doesn't expose money/
    bets per spread or total bucket). Returns 0 for SPR/TOT — splits
    contribute zero to those candidates' scores.
    """
    if not splits or market_type != "moneyline" or sharp_side not in ("home", "away"):
        return 0.0
    money = splits.get(f"{sharp_side}_money")
    bets  = splits.get(f"{sharp_side}_bets")
    if money is None or bets is None:
        return 0.0
    try:
        return float(money) - float(bets)
    except (TypeError, ValueError):
        return 0.0


def _suggest_picks(odds: dict, splits: dict | None = None) -> list[dict]:
    """Polymarket-execution picks. Returns a list — there can be more
    than one on a game. Behaviour:

    1. The TOP candidate across all markets is always returned (forced
       lean if needed, with `gates_cleared=False`).
    2. Additional picks from OTHER markets are appended only when they
       clear the sharp gate themselves (avoids 3 forced leans on a
       chalk-flat game).
    3. ML / SPR mutual exclusion — these are correlated (same
       directional bet), so we never recommend both. If both qualify,
       the higher combined_score wins, the other is dropped.

    Net result: 1-2 picks per game. TOT is always allowed alongside
    one of {ML, SPR}.

    Scoring (unchanged):
      sharp_score (0-10) — PIN movement magnitude on this side.
      splits_pp     — money% − bets% on this side (ML only).
      combined = 0.7·(sharp/10) + 0.3·(splits/30 capped 1.0)

    Returns [] when no PIN snapshot exists on any side of any market.

    Sizing tiers:
      sharp ≥ 7 + splits ≥ 10pp aligned → 10u whale
      sharp ≥ 5 + splits ≥ 5pp  aligned → 5u high
      sharp ≥ 4                          → 3u medium
      else                               → 1u low (forced lean)
    """
    candidates: list[dict] = []
    for mt in ("moneyline", "spread", "total"):
        blk = odds.get(mt) or {}
        mv = blk.get("movement") or {}
        sharp_side  = mv.get("sharp_side")
        sharp_score = mv.get("sharp_score") or 0
        sides = ("over", "under") if mt == "total" else ("away", "home")
        for side in sides:
            pin = (blk.get("pin_current") or {}).get(side) or {}
            fair_prob     = pin.get("fair_prob")
            fair_american = pin.get("fair_american")
            # Need at minimum a fair line to call this a Polymarket target.
            if fair_prob is None or fair_american is None:
                continue

            score_for_side = sharp_score if side == sharp_side else 0
            splits_pp = _splits_signal_pp(splits, sharp_side, mt) if score_for_side > 0 else 0.0
            cs = (SHARP_WEIGHT * (score_for_side / 10.0)
                  + SPLITS_WEIGHT * min(max(0.0, splits_pp) / 30.0, 1.0))
            gates_cleared = score_for_side >= SHARP_SCORE_MIN

            candidates.append({
                "market_type":    mt,
                "side":           side,
                "sharp_score":    score_for_side,
                "splits_pp":      round(splits_pp, 1),
                "fair_prob":      fair_prob,
                "fair_american":  fair_american,
                "pin_current":    pin.get("price"),
                "pin_line":       pin.get("line"),
                "combined_score": round(cs, 4),
                "gates_cleared":  gates_cleared,
            })

    if not candidates:
        return []

    # Best candidate per market_type (with sizing applied).
    by_market: dict[str, dict] = {}
    for c in candidates:
        s = c["sharp_score"]
        sp = c["splits_pp"]
        if s >= 7 and sp >= SPLITS_MIN_PP:
            c["units"], c["confidence"] = 10, "whale"
        elif s >= 5 and sp >= 5:
            c["units"], c["confidence"] = 5, "high"
        elif s >= SHARP_SCORE_MIN:
            c["units"], c["confidence"] = 3, "medium"
        else:
            c["units"], c["confidence"] = 1, "low"
        cur = by_market.get(c["market_type"])
        if (not cur) or ((c["gates_cleared"], c["combined_score"]) >
                         (cur["gates_cleared"], cur["combined_score"])):
            by_market[c["market_type"]] = c

    # Top pick across all markets — always shown, even as a forced lean.
    all_top = max(by_market.values(),
                  key=lambda c: (c["gates_cleared"], c["combined_score"]))
    chosen: list[dict] = [all_top]

    # Add other markets that have cleared the gate themselves.
    for mt in ("moneyline", "spread", "total"):
        c = by_market.get(mt)
        if not c or c is all_top:
            continue
        if c["gates_cleared"]:
            chosen.append(c)

    # ML / SPR exclusion — never both. If both made it in, drop the
    # lower-scoring one.
    has_ml  = any(c["market_type"] == "moneyline" for c in chosen)
    has_spr = any(c["market_type"] == "spread"    for c in chosen)
    if has_ml and has_spr:
        ml  = next(c for c in chosen if c["market_type"] == "moneyline")
        spr = next(c for c in chosen if c["market_type"] == "spread")
        drop = spr if ml["combined_score"] >= spr["combined_score"] else ml
        chosen = [c for c in chosen if c is not drop]

    # Stable order: ML/SPR first, then TOT.
    chosen.sort(key=lambda c: 0 if c["market_type"] != "total" else 1)
    return chosen


# ──────────────────────────── Public entry point ────────────────────────────

def build_dossier(sb, query: str | None, sport_hint: str | None,
                  market_id: str | None = None,
                  live: bool = False) -> dict:
    """Top-level call. Returns the dossier dict shaped for the analyst /
    web page. Same shape as the kahla-scanner CLI version.

    Two entry modes:
      • market_id given — direct lookup, skips the freeform team-search.
        Used by the click-to-pick game cards on /handicapper.
      • query given — fuzzy team-name match against active markets.
        Used by the search bar.

    `live=True` adds an on-demand Odds API call for the matched game and
    replaces the Supabase-cached `latest` snapshots with live data. PIN
    opener still comes from Supabase (live fetch only has the current
    line, not history). Costs 6 Odds API credits per click. Used by the
    "Pick" button on the game list so dossiers reflect the moment, not
    the last 30-min cron tick.
    """
    alts: list = []
    market: dict | None = None
    if market_id:
        try:
            market = (sb.table("markets")
                      .select("id,sport,event_name,event_start,status")
                      .eq("id", market_id)
                      .single().execute().data)
        except Exception:
            market = None
        if not market:
            return {
                "ok": False,
                "error": f"market {market_id} not found",
                "query": query or market_id,
                "hint": "Game may have ended or been removed.",
            }
    else:
        if not query:
            return {
                "ok": False,
                "error": "query required",
                "hint": "Pass either ?q=... or ?market_id=...",
            }
        market, alts = _find_market(sb, query, sport_hint)
        if not market:
            return {
                "ok": False,
                "error": "no market matched query",
                "query": query,
                "tokens": _parse_query(query),
                "hint": "Check spelling, or try 'Yankees vs Red Sox' style.",
            }

    sport = market["sport"]
    away, home = _split_event_name(market["event_name"])
    event_start = market["event_start"]
    try:
        bet_dt = datetime.fromisoformat(event_start.replace("Z", "+00:00"))
        starts_in_min = round((bet_dt - datetime.now(timezone.utc)).total_seconds() / 60)
    except Exception:
        bet_dt = None
        starts_in_min = None

    latest = _latest_snapshots(sb, market["id"])
    pin_op = _pin_opener(sb, market["id"])

    # Live odds refresh — replace `latest` with a fresh Odds API call so
    # the dossier reflects current lines, not the last 30-min cron tick.
    # Failures are reported via `live_error` so the UI can explain why
    # cached fell-back happened.
    live_used = False
    live_error: str | None = None
    if live and away and home and event_start:
        ev, err = _fetch_live_event(sport, event_start, away, home)
        if ev:
            live_latest = _live_event_to_latest(ev)
            if live_latest:
                latest = live_latest
                live_used = True
            else:
                live_error = "live response had no usable book lines"
        else:
            live_error = err
    elif live:
        live_error = "missing event metadata"
    odds = {
        "moneyline": _build_market_block("moneyline", ("away", "home"),
                                          latest, pin_op),
        "spread":    _build_market_block("spread",    ("away", "home"),
                                          latest, pin_op),
        "total":     _build_market_block("total",     ("over", "under"),
                                          latest, pin_op),
    }

    splits = _fetch_splits(sport, away, home) if (away and home) else None

    espn_block: dict = {}
    if sport in _ESPN_PATH and bet_dt and away and home:
        date_key = bet_dt.astimezone(ZoneInfo("America/New_York")).strftime("%Y%m%d")
        events = _espn_scoreboard(sport, date_key)
        m = _espn_match_event(events, away, home, bet_dt)
        if m:
            home_t = _team_block(m["home"])
            away_t = _team_block(m["away"])
            home_t["injuries"] = _espn_team_injuries(sport, home_t.get("id"))
            away_t["injuries"] = _espn_team_injuries(sport, away_t.get("id"))
            home_t["recent"]   = _espn_team_recent(sport, home_t.get("id"))
            away_t["recent"]   = _espn_team_recent(sport, away_t.get("id"))
            comp = (m["event"].get("competitions") or [{}])[0]
            espn_block = {
                "venue":      ((comp.get("venue") or {}).get("fullName")),
                "broadcasts": [b.get("names") for b in comp.get("broadcasts") or [] if b.get("names")],
                "home":       home_t,
                "away":       away_t,
            }

    mlb_extra = {}
    team_compare = None
    if sport == "MLB" and event_start and away and home:
        probables = _mlb_probables(event_start, away, home)
        mlb_extra["probable_pitchers"] = probables
        team_compare = _mlb_team_compare(
            probables.get("away_team_id"),
            probables.get("home_team_id"),
            away, home,
        )
    elif sport in _ESPN_COMPARE_FIELDS and espn_block:
        away_t = espn_block.get("away") or {}
        home_t = espn_block.get("home") or {}
        team_compare = _espn_team_compare(
            sport,
            away_t.get("id"), home_t.get("id"),
            away or "", home or "",
            _espn_team_record(away_t), _espn_team_record(home_t),
        )

    suggestions = _suggest_picks(odds, splits)
    # Keep the singular `suggestion` field as an alias for the top pick
    # so any caller still expecting it doesn't break. New code should
    # use `suggestions` (list) so multi-pick games render correctly.
    suggestion = suggestions[0] if suggestions else None

    return {
        "ok":              True,
        "query":           query or market["event_name"],
        "market_id":       market["id"],
        "sport":           sport,
        "event_name":      market["event_name"],
        "event_start_utc": event_start,
        "starts_in_min":   starts_in_min,
        "away":            away,
        "home":            home,
        "venue":           espn_block.get("venue"),
        "odds":            odds,
        "splits":          splits,
        "espn":            {
            "home":       espn_block.get("home"),
            "away":       espn_block.get("away"),
            "broadcasts": espn_block.get("broadcasts"),
        } if espn_block else None,
        "mlb":             mlb_extra or None,
        "team_compare":    team_compare,
        "suggestion":      suggestion,
        "suggestions":     suggestions,
        "alt_matches":     [
            {"id": m["id"], "event_name": m["event_name"],
             "event_start": m["event_start"], "sport": m["sport"]}
            for m in alts[1:]
        ],
        "live_used":       live_used,
        "live_error":      live_error,
        "generated_at":    datetime.now(timezone.utc).isoformat(),
    }
