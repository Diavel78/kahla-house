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
import math
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
    "NHL":   "icehockey_nhl",  # also try `icehockey_nhl_championship`
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


def _american_to_decimal(price: int) -> float:
    """American odds → decimal odds (total return per 1 staked)."""
    if price > 0:
        return 1.0 + price / 100.0
    return 1.0 + 100.0 / abs(price)


def _amer_to_cents(p: Any) -> float | None:
    """Convert American odds to a continuous "cents-from-pickem" value.
    Higher = more favored. -150 → 50, +150 → -50. Bridges the ±100
    discontinuity so subtractions across the boundary are meaningful."""
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


# ─────────────────────── Recency-weighted sharp score ───────────────────────
#
# Replaces the original "all-time first snapshot vs current" anchor with a
# weighted-sum-of-consecutive-deltas across the last 18h of PIN history.
# Each delta gets multiplied by a recency weight based on how recent the
# move was (newer endpoint of the delta). The result emphasizes late
# steam (final 15 min before tip) over yesterday's news arrival.
#
# Weight buckets:
#   0-15 min   → 1.00   (final-tick steam, full weight)
#   15-60 min  → 0.60
#   1-2 h      → 0.35
#   2-6 h      → 0.18
#   6-18 h     → 0.08
#   >18 h      → 0.00   (out of range — filtered at fetch time)
#
# Calibration scenarios under these weights:
#   • 5c PIN move in last 15 min  → score 5  (fresh sharp signal)
#   • 5c PIN move 1h ago          → score 3  (still relevant)
#   • 5c PIN move 12h ago         → score 0  (old news, ignored)
#   • 10c slow drip over 5h       → score ~3 (diluted by slow timing)
#   • 10c spike in last 15 min    → score 10 (capped — late steam)
#
# Same direction convention as the original helpers:
#   • ML: sharp side = side whose weighted cent-sum is MORE POSITIVE
#     (cents-positive = more favored = harder bet)
#   • SPR: sharp side = side whose weighted LINE delta is more negative
#     (line tightened = harder spread)
#   • TOT: weighted line delta on the over side > 0 → sharp OVER,
#     < 0 → sharp UNDER. Line flat → fall back to vig direction.

_RECENCY_WEIGHTS: tuple[tuple[float, float], ...] = (
    (15,   1.00),
    (60,   0.60),
    (120,  0.35),
    (360,  0.18),
    (1080, 0.08),
)


def _recency_weight(age_min: float) -> float:
    """Weight multiplier for a delta whose newer endpoint is `age_min`
    minutes old. Outside 18h returns 0 — those snapshots should already
    be filtered out at fetch time."""
    if age_min < 0:
        age_min = 0
    for cap, w in _RECENCY_WEIGHTS:
        if age_min < cap:
            return w
    return 0.0


def _snap_age_min(snap: dict, now: datetime) -> float:
    ts = snap.get("captured_at")
    if not ts:
        return 1e9  # treat unknown timestamps as ancient → zero weight
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except Exception:
        return 1e9
    return max(0.0, (now - dt).total_seconds() / 60.0)


def _weighted_signed_delta(
    snaps: list[dict],
    field: str,
    *,
    transform: Any = None,
    now: datetime | None = None,
) -> float:
    """Weighted sum of consecutive deltas in `field` across `snaps`,
    where each delta's weight is `_recency_weight(age_of_newer_snap)`.
    `transform`, if given, is applied to each raw value before
    subtraction — used to convert American prices to cents.

    Returns 0.0 when there's nothing to compare (fewer than 2 snaps).
    """
    if not snaps or len(snaps) < 2:
        return 0.0
    now = now or datetime.now(timezone.utc)
    total = 0.0
    prev_val = snaps[0].get(field)
    if transform is not None:
        prev_val = transform(prev_val)
    if prev_val is None:
        prev_val = 0
    for cur in snaps[1:]:
        cur_val = cur.get(field)
        if transform is not None:
            cur_val = transform(cur_val)
        if cur_val is None:
            continue
        delta = cur_val - prev_val
        if delta:
            total += delta * _recency_weight(_snap_age_min(cur, now))
        prev_val = cur_val
    return total


def _weighted_sharp_for_ml(home_snaps: list[dict],
                           away_snaps: list[dict]) -> tuple | None:
    """Recency-weighted ML sharp side + score.

    Returns (side, score, op_snap, cu_snap) where op_snap is the OLDEST
    snap in the 18h window for the chosen side (displayed as "opener")
    and cu_snap is the most recent. Same return shape as the legacy
    `_sharp_for_ml` so callers don't have to change.
    """
    now = datetime.now(timezone.utc)
    h_w = _weighted_signed_delta(home_snaps, "price_american",
                                  transform=_amer_to_cents, now=now)
    a_w = _weighted_signed_delta(away_snaps, "price_american",
                                  transform=_amer_to_cents, now=now)

    h_avail = len(home_snaps) >= 2
    a_avail = len(away_snaps) >= 2
    if not (h_avail or a_avail):
        return None

    if h_avail and a_avail:
        # Sharp side = side whose weighted cents-sum is MORE positive
        # (got more favored, harder to bet). Ties skip.
        if abs(h_w - a_w) < 0.5:
            return None
        if h_w > a_w:
            side, snaps, weighted = "home", home_snaps, h_w
        else:
            side, snaps, weighted = "away", away_snaps, a_w
    elif h_avail:
        if h_w <= 0.5:
            return None
        side, snaps, weighted = "home", home_snaps, h_w
    else:
        if a_w <= 0.5:
            return None
        side, snaps, weighted = "away", away_snaps, a_w

    score = min(10, round(abs(weighted)))
    if score <= 0:
        return None
    return side, score, snaps[0], snaps[-1]


def _weighted_sharp_for_spread(home_snaps: list[dict],
                               away_snaps: list[dict]) -> tuple | None:
    """Recency-weighted SPR sharp side + score. Line move is primary;
    vig drift is fallback (only used when weighted line move is < 0.05
    points). Never additive — same "line OR vig" rule as the original.
    """
    now = datetime.now(timezone.utc)
    h_avail = len(home_snaps) >= 2
    a_avail = len(away_snaps) >= 2
    if not (h_avail or a_avail):
        return None

    h_line = _weighted_signed_delta(home_snaps, "line", now=now) if h_avail else 0.0
    a_line = _weighted_signed_delta(away_snaps, "line", now=now) if a_avail else 0.0
    h_px = _weighted_signed_delta(home_snaps, "price_american",
                                    transform=_amer_to_cents, now=now) if h_avail else 0.0
    a_px = _weighted_signed_delta(away_snaps, "price_american",
                                    transform=_amer_to_cents, now=now) if a_avail else 0.0

    def _pick_two_sided() -> tuple[str, list[dict], float, bool] | None:
        # Line first. Negative weighted-line on a side = that side's spread tightened
        # (harder); compare which moved harder.
        line_diff = h_line - a_line
        if abs(line_diff) >= 0.05:
            if h_line < a_line:
                return "home", home_snaps, abs(h_line) * 10, True
            return "away", away_snaps, abs(a_line) * 10, True
        # Line flat — use vig (cents). More-positive cents = harder bet.
        px_diff = h_px - a_px
        if abs(px_diff) >= 1.0:
            if h_px > a_px:
                return "home", home_snaps, abs(h_px), False
            return "away", away_snaps, abs(a_px), False
        return None

    def _pick_one_sided(ref_snaps, ref_is_home) -> tuple[str, list[dict], float, bool] | None:
        rl = _weighted_signed_delta(ref_snaps, "line", now=now)
        rp = _weighted_signed_delta(ref_snaps, "price_american",
                                     transform=_amer_to_cents, now=now)
        if abs(rl) >= 0.05:
            ref_harder = rl < 0
            score_mag = abs(rl) * 10
            from_line = True
        elif abs(rp) >= 1.0:
            ref_harder = rp > 0  # cents went up = ref got more favored = harder
            score_mag = abs(rp)
            from_line = False
        else:
            return None
        if ref_is_home:
            side = "home" if ref_harder else "away"
        else:
            side = "away" if ref_harder else "home"
        # If sharp side != ref side, we don't have the sharp side's
        # snapshots to display — bail. Matches "one-sided: skip rather
        # than guess" from CLAUDE.md gotcha #21.
        if (side == "home") != ref_is_home:
            return None
        return side, ref_snaps, score_mag, from_line

    if h_avail and a_avail:
        pick = _pick_two_sided()
    elif h_avail:
        pick = _pick_one_sided(home_snaps, True)
    else:
        pick = _pick_one_sided(away_snaps, False)

    if not pick:
        return None
    side, snaps, score_mag, _from_line = pick
    score = min(10, round(score_mag))
    if score <= 0:
        return None
    return side, score, snaps[0], snaps[-1]


def _weighted_sharp_for_total(over_snaps: list[dict],
                              under_snaps: list[dict]) -> tuple | None:
    """Recency-weighted TOT sharp side + score. Sharp side rule
    (raised → over, lowered → under) is asymmetric for line moves but
    symmetric for vig — same as the original helper. Returns the
    5-tuple including ref_side so the bullet renderer can tag which
    side's prices are being shown.
    """
    now = datetime.now(timezone.utc)
    o_avail = len(over_snaps) >= 2
    u_avail = len(under_snaps) >= 2
    if not (o_avail or u_avail):
        return None

    # Pick whichever side has the bigger weighted move (line first,
    # then vig). Use ITS snapshots as the displayed reference so the
    # chip can't show a flat-looking pair while the score comes from
    # the other side.
    def _mag(snaps: list[dict]) -> tuple[float, float]:
        ln = abs(_weighted_signed_delta(snaps, "line", now=now))
        px = abs(_weighted_signed_delta(snaps, "price_american",
                                         transform=_amer_to_cents, now=now))
        return ln, px

    if o_avail and u_avail:
        if _mag(over_snaps) >= _mag(under_snaps):
            ref_snaps, ref_is_over = over_snaps, True
        else:
            ref_snaps, ref_is_over = under_snaps, False
    elif o_avail:
        ref_snaps, ref_is_over = over_snaps, True
    else:
        ref_snaps, ref_is_over = under_snaps, False

    line_w = _weighted_signed_delta(ref_snaps, "line", now=now)
    price_w = _weighted_signed_delta(ref_snaps, "price_american",
                                       transform=_amer_to_cents, now=now)

    if abs(line_w) >= 0.05:
        # Line move on the OVER side: positive = total raised → sharp OVER.
        # Line move on the UNDER side: positive means the under-side
        # "line" representation went up (totals are stored per side; for
        # the under side, the line field usually mirrors the over line,
        # so a positive shift still means total raised → sharp OVER).
        # We treat both the same and infer purely from the sign.
        side = "over" if line_w > 0 else "under"
        score_mag = abs(line_w) * 10
    elif abs(price_w) >= 1.0:
        # Line flat; use vig. Positive cents on the over snaps = over got
        # harder = sharp OVER. Positive cents on the under snaps = under
        # got harder = sharp UNDER.
        if price_w > 0:
            side = "over" if ref_is_over else "under"
        else:
            side = "under" if ref_is_over else "over"
        score_mag = abs(price_w)
    else:
        return None

    score = min(10, round(score_mag))
    if score <= 0:
        return None
    ref_side = "over" if ref_is_over else "under"
    return side, score, ref_snaps[0], ref_snaps[-1], ref_side


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
    """Latest snapshot per (book, market_type, side) for one market —
    NO time cutoff. PIN dedup means a steady line on PIN can have its
    only row hours or even days old (CLAUDE.md gotcha #10). A hard 24h
    cutoff would filter out PIN's actual current line on slow-moving
    games and make the dossier show "no PIN data" while PIN is in fact
    on the screen everywhere else.

    The query is scoped to one market_id so the result set is small —
    even a stable game has at most ~14 books × 3 markets × 2 sides ≈
    84 distinct keys. We over-fetch (limit 5000) to be safe against
    edge cases with many price ticks, then take the first row per key
    from the desc-ordered list.
    """
    rows = (sb.table("book_snapshots")
            .select("book,market_type,side,price_american,line,captured_at")
            .eq("market_id", market_id)
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


# 18h history window. PIN movement older than this is ignored — late
# steam is what matters; 18h+ old news adds noise to the score and
# fights the recency-weighted picture. Aligns with the ingest cron's
# 18h cap (we don't pull anything older than this anyway).
_PIN_HISTORY_HOURS = 18


def _attach_pmm_to_odds(odds: dict, pmm: dict, sport: str) -> None:
    """For each market_type block in `odds`, attach a `polymarket` field
    carrying:
        {
          event_slug, event_title,
          home: { line, slug, title, quote, projected: {...} },
          away: { ... },
          # for total:
          over:  { line, slug, title, quote, projected: {...} },
          under: { ... },
        }

    Each side's `projected` block holds the PIN-derived fair_prob /
    fair_american at PMM's line (push-rate adjusted from PIN fair).
    The UI uses `projected.fair_american` as the recommended
    limit-order price and compares it against `quote.ask_american` to
    show "is PMM offering a better price than fair?".
    """
    import pmm_markets
    from pmm_push_rates import project_fair_to_half_point

    mt_map = {"moneyline": "ml", "spread": "spread", "total": "total"}
    side_pairs = {"moneyline": ("away", "home"),
                  "spread":    ("away", "home"),
                  "total":     ("over", "under")}

    for market_type, blk in odds.items():
        pmm_key = mt_map.get(market_type)
        if not pmm_key:
            continue
        pin_current = blk.get("pin_current") or {}
        a, b = side_pairs[market_type]
        out: dict = {
            "event_slug":  pmm.get("event_slug"),
            "event_title": pmm.get("event_title"),
        }
        for side in (a, b):
            pin_side = pin_current.get(side) or {}
            pin_line = pin_side.get("line")
            pin_fair_prob = pin_side.get("fair_prob")
            entry = pmm_markets.best_line_for(pmm, pmm_key, side, pin_line)
            if not entry:
                out[side] = None
                continue
            block: dict = {
                "line":  entry.get("line"),
                "slug":  entry.get("slug"),
                "title": entry.get("title"),
                "quote": entry.get("quote"),
            }
            # Project PIN's devigged fair onto PMM's line. ML has no
            # line shift; just carry PIN fair through unchanged. SPR/TOT
            # apply push-rate math.
            if pmm_key == "ml":
                projected_prob = pin_fair_prob
                proj_meta = {"applicable": pin_fair_prob is not None,
                             "note": "ml — no line shift"}
            else:
                projected_prob, proj_meta = project_fair_to_half_point(
                    sport, pmm_key, pin_line, entry.get("line"),
                    side, pin_fair_prob,
                )
            block["projected"] = {
                "fair_prob":     round(projected_prob, 4) if projected_prob is not None else None,
                "fair_american": _prob_to_american(projected_prob) if projected_prob is not None else None,
                "meta":          proj_meta,
            }
            out[side] = block
        blk["polymarket"] = out


def _pin_history(sb, market_id: str) -> dict[tuple[str, str], list[dict]]:
    """All PIN snapshots in the last 18h, grouped by (market_type, side)
    and sorted ASC by captured_at. Replaces the legacy `_pin_opener`
    which returned only the all-time first snapshot per side.

    The full timeline lets the recency-weighted helpers compute
    consecutive-delta scores. Each side's list[0] is the "opener"
    relative to the 18h window — used as the displayed opener so the
    user sees something meaningful instead of a 4-day-old line.
    """
    cutoff = (datetime.now(timezone.utc)
              - timedelta(hours=_PIN_HISTORY_HOURS)).isoformat()
    rows = (sb.table("book_snapshots")
            .select("market_type,side,price_american,line,captured_at")
            .eq("market_id", market_id)
            .eq("book", "PIN")
            .gte("captured_at", cutoff)
            .order("captured_at")
            .limit(5000)
            .execute().data) or []
    out: dict[tuple[str, str], list[dict]] = {}
    for r in rows:
        key = (r["market_type"], r["side"])
        out.setdefault(key, []).append(r)
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

    def _norm(s: str) -> str:
        # Lowercase + collapse non-alphanumeric to spaces. Catches the
        # `Cortes-Acosta` vs `cortes acosta` style differences and
        # handles diacritics that lowercase to ASCII variants.
        return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()

    def _name_match(ev_name: str, our_name: str) -> bool:
        ev_n, our_nn = _norm(ev_name), _norm(our_name)
        if not (ev_n and our_nn):
            return False
        if our_nn in ev_n or ev_n in our_nn:
            return True
        if sport == "UFC":
            # Token fallback for UFC quirks (B. Susurkaev vs Baysangur
            # Susurkaev, hyphenated last names, etc.). Any 3+ char token
            # in our name appearing in the API name is enough.
            tokens = [t for t in our_nn.split() if len(t) >= 3]
            return any(t in ev_n for t in tokens)
        return False

    def _pair_match(ev_home: str, ev_away: str) -> tuple[bool, bool]:
        """Return (matched, swapped). swapped=True means the API's
        home_team is OUR away team (UFC home/away is arbitrary)."""
        if _name_match(ev_home, home_n) and _name_match(ev_away, away_n):
            return True, False
        if sport == "UFC":
            if _name_match(ev_home, away_n) and _name_match(ev_away, home_n):
                return True, True
        return False, False

    for ev in events:
        ev_home = (ev.get("home_team") or "").lower()
        ev_away = (ev.get("away_team") or "").lower()
        if not ev_home or not ev_away:
            continue
        matched, swapped = _pair_match(ev_home, ev_away)
        if not matched:
            continue
        try:
            ev_dt = datetime.fromisoformat(
                (ev.get("commence_time") or "").replace("Z", "+00:00"))
        except Exception:
            continue
        if abs((ev_dt - bet_dt).total_seconds()) > window.total_seconds():
            continue
        if swapped:
            # Flip home/away on a shallow copy so _live_event_to_latest
            # routes outcomes by the right side. Outcome names match by
            # team name (not the "home"/"away" label), so flipping the
            # event-level fields is enough — the name mapping in
            # _live_event_to_latest resolves correctly.
            ev = {**ev,
                  "home_team": ev.get("away_team"),
                  "away_team": ev.get("home_team")}
        return ev, None
    # No match — surface sample events from the response so we can
    # diagnose without re-hitting the API. Most failures are name
    # mismatches (Odds API returns slight variants), commence_time
    # drift past our window, or the game being under a different
    # sport_key entirely (e.g. NHL Stanley Cup specialty key).
    if not events:
        return None, f"odds API returned 0 events for sport_key={sport_key}"
    samples = ", ".join(
        f"{(e.get('away_team') or '?')}@{(e.get('home_team') or '?')}"
        for e in events[:8]
    )
    return None, (f"no match in {len(events)} events from sport_key={sport_key}. "
                  f"samples: {samples}")


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
                        latest: dict, pin_history: dict) -> dict:
    a, b = sides
    pin_a = latest.get(("PIN", market_type, a))
    pin_b = latest.get(("PIN", market_type, b))
    snaps_a = pin_history.get((market_type, a), [])
    snaps_b = pin_history.get((market_type, b), [])
    op_a  = snaps_a[0] if snaps_a else None
    op_b  = snaps_b[0] if snaps_b else None

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
        sr = _weighted_sharp_for_ml(
            pin_history.get(("moneyline", "home"), []),
            pin_history.get(("moneyline", "away"), []),
        )
    elif market_type == "spread":
        sr = _weighted_sharp_for_spread(
            pin_history.get(("spread", "home"), []),
            pin_history.get(("spread", "away"), []),
        )
    else:
        sr = _weighted_sharp_for_total(
            pin_history.get(("total", "over"), []),
            pin_history.get(("total", "under"), []),
        )

    movement = None
    if sr:
        # `_weighted_sharp_for_total` returns a 5-tuple including
        # `ref_side` — which side the displayed snapshots actually
        # belong to. For ML/SPR the ref_side IS the sharp side (those
        # functions pick the side with the bigger move). For TOT, when
        # the vig moved on the side that got EASIER, ref_side is the
        # OPPOSITE of sharp_side, so the bullet needs to tag the
        # displayed prices with which side they're from.
        if len(sr) == 5:
            side, score, op, cu, ref_side = sr
        else:
            side, score, op, cu = sr
            ref_side = side
        movement = {
            "sharp_side":  side,
            "sharp_score": score,
            "ref_side":    ref_side,
            "opener_price": op["price_american"],
            "opener_line":  op.get("line"),
            "opener_captured": op.get("captured_at"),
            "current_price": cu["price_american"],
            "current_line":  cu.get("line"),
            "window_hours":  _PIN_HISTORY_HOURS,
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


# ─── Splits ───
# We don't re-implement the Action Network scraper here; we reuse the
# one in `app.py:_fetch_action_splits` (same orchestrator that powers
# the /odds page splits row). Late-imported to avoid the circular load
# that'd otherwise happen since app.py imports build_dossier from us.


def _team_match(home: str, away: str, ev_home: str, ev_away: str) -> bool:
    """Two-way substring match. Action uses 'Mariners', we have 'Seattle
    Mariners'; both directions need to work. Also normalize diacritics
    so 'Montréal Canadiens' matches 'Montreal Canadiens' — without
    this, NHL splits silently fail every Montreal game."""
    if not (home and away and ev_home and ev_away):
        return False
    import unicodedata
    def _norm(s: str) -> str:
        # NFKD splits accented chars into base + combining mark; the
        # mark has category "Mn" (Mark, nonspacing) which we drop.
        return "".join(c for c in unicodedata.normalize("NFKD", s)
                       if unicodedata.category(c) != "Mn").lower()
    h, a = _norm(home), _norm(away)
    eh, ea = _norm(ev_home), _norm(ev_away)
    return ((h in eh or eh in h) and (a in ea or ea in a))



def _fetch_splits(sport: str, away: str, home: str) -> dict:
    """Splits for one (away, home) pair. Calls into app.py's
    `_fetch_action_splits` — the SAME orchestrator that powers the
    /odds page splits row, which is known-working — instead of
    duplicating the scraper. Matches the returned events by two-way
    team-name containment and returns the matched event's ML splits.

    ALWAYS returns a dict so the frontend can show the empty state
    when nothing matched. `sources` lists what contributed (just
    `action` for now — Covers / VegasInsider were dropped because
    they were broken AND Action carries money% which is the only
    real sharp signal).

    Late import of app.py to avoid circular import at module load."""
    sources_tried = ["action"]
    diagnostics: dict[str, dict] = {}
    matched_ml: dict | None = None

    try:
        from app import _fetch_action_splits as _app_action  # late import
        action = _app_action(_ACTION_LEAGUE.get(sport, sport).lower()) or {}
    except Exception as e:
        diagnostics["action"] = {
            "matched":         False,
            "events_returned": 0,
            "sample_games":    [],
            "fetch_debug":     {"error": f"app import: {e}"},
        }
        action = {"events": []}

    events = action.get("events") or []
    for ev in events:
        if _team_match(home, away,
                       ev.get("home_team", ""),
                       ev.get("away_team", "")):
            matched_ml = ev.get("ml") or None
            break

    if "action" not in diagnostics:
        sample = [f"{e.get('away_team', '?')} @ {e.get('home_team', '?')}"
                  for e in events[:5]]
        diagnostics["action"] = {
            "matched":         matched_ml is not None,
            "events_returned": len(events),
            "sample_games":    sample,
            "fetch_debug":     {
                "source": action.get("source"),
                "url":    action.get("url"),
                "ok":     action.get("ok"),
                "error":  action.get("error"),
                "next_debug": action.get("next_debug"),
                "api_debug":  action.get("api_debug"),
            },
        }

    if matched_ml is None:
        return {
            "away_bets":     None, "home_bets":     None,
            "away_money":    None, "home_money":    None,
            "sharp_diff":    None,
            "sources":       [],
            "sources_tried": sources_tried,
            "per_source":    diagnostics,
        }
    out = dict(matched_ml)
    out["sources"]       = ["action"]
    out["sources_tried"] = sources_tried
    out["per_source"]    = diagnostics
    return out



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
    try:
        target = datetime.fromisoformat(event_iso.replace("Z", "+00:00"))
    except Exception:
        target = None
    # Collect EVERY game matching by team name. A doubleheader returns two
    # (same teams, same day) — pick the one whose scheduled start is closest
    # to this dossier's event_start, so game 2 doesn't silently inherit game
    # 1's probable pitchers / game_pk / venue.
    cands = []
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
            cands.append((g, home_t, away_t))
    if not cands:
        return {}
    g, home_t, away_t = cands[0]
    if target and len(cands) > 1:
        dated = []
        for c in cands:
            try:
                gd = datetime.fromisoformat((c[0].get("gameDate") or "").replace("Z", "+00:00"))
                dated.append((c, gd))
            except Exception:
                continue
        if dated:
            g, home_t, away_t = min(
                dated, key=lambda x: abs((x[1] - target).total_seconds()))[0]
    return {
        "venue": (g.get("venue") or {}).get("name"),
        "away":  _mlb_pitcher_block(((g.get("teams") or {}).get("away") or {}).get("probablePitcher")),
        "home":  _mlb_pitcher_block(((g.get("teams") or {}).get("home") or {}).get("probablePitcher")),
        "away_team_id": away_t.get("id"),
        "home_team_id": home_t.get("id"),
        "game_pk":      g.get("gamePk"),
    }


# MLB lineup layer — we model the starter, but the OFFENSE projection
# assumes the team's standard run output. When regulars sit (day-after-
# night, a getaway day, September rest) the posted lineup is materially
# weaker. We can't price every bat free, so we detect the high-value case:
# a top-OPS hitter who's NOT in tonight's posted batting order → dock a
# small chunk of that team's projected runs per missing regular. Lineups
# post ~3-4h pre-game; before that the boxscore battingOrder is empty and
# this is a clean no-op (the dossier auto-refresh picks it up later).
_MLB_REST_RUNS = 0.18      # runs docked per missing top-OPS regular
_MLB_REST_MAX  = 0.6       # cap per side


def _mlb_posted_lineup(game_pk) -> dict | None:
    if not game_pk:
        return None
    data = _http_get(f"https://statsapi.mlb.com/api/v1/game/{game_pk}/boxscore")
    if not data:
        return None
    teams = data.get("teams") or {}
    out: dict = {}
    for side in ("home", "away"):
        names = set()
        for p in ((teams.get(side) or {}).get("players") or {}).values():
            if p.get("battingOrder"):     # only starters carry a batting order
                nm = ((p.get("person") or {}).get("fullName") or "").lower()
                if nm:
                    names.add(nm)
        if names:
            out[side] = names
    return out or None


def _mlb_hitting_leaders(team_id, season: int, limit: int = 4) -> list:
    """Top hitters by OPS (original-case full names). One call, [] on fail."""
    if not team_id:
        return []
    data = _http_get(f"https://statsapi.mlb.com/api/v1/teams/{team_id}/leaders",
                     params={"leaderCategories": "onBasePlusSlugging",
                             "season": season, "leaderGameTypes": "R",
                             "limit": limit})
    if not data:
        return []
    out = []
    for lc in (data.get("teamLeaders") or data.get("leaders") or []):
        for ld in lc.get("leaders") or []:
            nm = ((ld.get("person") or {}).get("fullName") or "")
            if nm:
                out.append(nm)
    return out


def _mlb_lineup_dock(game_pk, away_id, home_id, season: int) -> dict | None:
    """Dock projected runs for top-OPS regulars absent from the posted
    lineup. None when no lineup posted / nobody material is missing."""
    lineup = _mlb_posted_lineup(game_pk)
    if not lineup:
        return None
    dock: dict = {}
    notes: dict = {}
    for side, tid in (("home", home_id), ("away", away_id)):
        present = lineup.get(side)
        miss = []
        if present:
            for full in _mlb_hitting_leaders(tid, season):
                ln = full.lower()
                if not any(ln in p or p in ln for p in present):
                    miss.append(full)
        dock[side] = round(min(len(miss) * _MLB_REST_RUNS, _MLB_REST_MAX), 2)
        notes[side] = miss
    if not (dock.get("home") or dock.get("away")):
        return None
    return {"home": dock["home"], "away": dock["away"],
            "home_out": notes["home"], "away_out": notes["away"]}


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

# Spread-only chalk filter. A SPR pick at -150 or worse is just a
# leveraged ML — same directional bet at materially worse price/risk.
# Drop SPR candidates this chalky and let the ML candidate (if it
# qualifies) take its place. Doesn't apply to ML/TOT — heavy chalk
# there has no better expression to switch to.
SPR_CHALK_FAIR_CAP = -150

# When BOTH an ML and a SPR candidate exist on the same side, drop
# the ML if its fair is at or below this cap. The leveraged SPR is
# the cleaner expression of a chalky directional bet at this price.
# ML alone (no SPR alternative) still stands — see the ML/SPR
# exclusion block in `_suggest_picks`.
ML_CHALK_FAIR_CAP = -140


# ──────────────────────────── Kelly sizing ────────────────────────────
#
# This bot bets AT the devigged fair price (Polymarket limit orders), so
# there's no price discrepancy to Kelly off directly — the edge is the
# SIGNAL: recency-weighted PIN movement, aligned public money, and the
# independent power-rating model agreeing. We translate signal strength
# into a provisional edge estimate (fair-prob percentage points), then
# size with quarter-Kelly. These per-signal coefficients are a
# conservative first guess; the CLV column (migration 007) measures the
# bot's realized edge over time so Stage-4 self-tuning can replace them
# with calibrated numbers. Sizing snaps to the existing 1/3/5u tiers:
# a pick that doesn't clear the sharp gate is always a 1u low lean; a
# gated pick is 3u medium, promoted to 5u high only when the ¼-Kelly
# stake clears KELLY_HIGH_PCT (genuinely strong multi-signal agreement).
KELLY_FRACTION       = 0.25     # quarter-Kelly — survives variance
EDGE_PER_SHARP_POINT = 0.40     # pp of edge per point of sharp_score (≤4pp at 10)
EDGE_PER_SPLITS_PP   = 0.10     # pp of edge per aligned money−bets pp (≤3pp at 30)
# The power rating is the bot's INDEPENDENT number. It feeds sizing as a
# CAPPED confirmation nudge when it agrees with the sharp side — but ONLY
# for sports where the walk-forward backtest showed real predictive signal
# (scripts/backtest_power_ratings.py). Everywhere else the card still
# renders (informative, pitcher-aware for MLB) but contributes 0 to the
# edge, because an unvalidated model has no business sizing a bet.
#
# Backtest verdict (May 2026, ~900 games/sport):
#   NBA   — 66.6% vs 54.1% baseline, Brier 0.215, calibration rises → SIGNAL
#   CBB   — 66% vs 57.6%, Brier 0.235, but thin/noisy + off-season → hold
#   MLB   — 52.5% vs 55.8% baseline, Brier 0.277, flat calibration → NOISE
#           (team core can't predict baseball without the pitcher; the
#            pitcher layer may rescue it but can't be backtested yet)
#   NHL   — 55.6% vs 52.1%, Brier 0.256 (> coinflip), flat → too weak
#   NFL/NCAAF — insufficient data (off-season)
#
# CRUCIAL: the backtest is TEAM-RATINGS ONLY — it can't include the MLB
# starting pitcher (no historical probables stored). The LIVE MLB model
# IS pitcher-aware (Phase 2b), so the "MLB = noise" verdict judges a model
# we don't actually run. MLB is therefore UNTESTED, not disproven — so we
# leave it ON (the 1.5pp cap bounds the risk) and judge the pitcher-aware
# version via LIVE CLV bucketed by model-agree/disagree over ~2 weeks.
# NHL stays OFF: it has no pitcher layer, so the backtest DOES fairly
# represent its live model, and it was weak. NBA stays ON (proven).
MODEL_FEEDS_SIZING   = True
MODEL_SIZING_SPORTS  = {"NBA", "MLB"}  # NBA backtest-proven; MLB pitcher-aware, CLV-pending
MODEL_EDGE_WEIGHT    = 0.25     # fraction of (model_prob − PIN_fair) credited
MODEL_EDGE_CAP_PP    = 1.5      # cap on the nudge — widen as CLV proves the model
EDGE_CAP_PP          = 6.0      # hard cap so a crude input can't blow up sizing
KELLY_HIGH_PCT       = 2.5      # ¼-Kelly stake ≥ this %BR → 5u high


def _kelly_units(fair_prob, fair_american, edge_pp,
                 gates_cleared) -> tuple[int, str, float]:
    """(units, confidence, kelly_pct). Quarter-Kelly stake from the
    signal-derived edge, snapped to the 1/3/5u tiers. Ungated picks are
    always a 1u low lean. Gated picks float between 3u and 5u by stake."""
    if not gates_cleared:
        return 1, "low", 0.0
    if fair_prob is None or fair_american is None or not edge_pp or edge_pp <= 0:
        return 3, "medium", 0.0
    true_p = min(max(fair_prob + edge_pp / 100.0, 0.01), 0.99)
    try:
        dec = _american_to_decimal(int(fair_american))
    except (TypeError, ValueError):
        return 3, "medium", 0.0
    b = dec - 1.0
    if b <= 0:
        return 3, "medium", 0.0
    f = true_p - (1.0 - true_p) / b      # full Kelly fraction
    kelly_pct = max(0.0, f) * KELLY_FRACTION * 100.0
    if kelly_pct >= KELLY_HIGH_PCT:
        return 5, "high", round(kelly_pct, 2)
    return 3, "medium", round(kelly_pct, 2)


# ──────────────────────────── Power rating ────────────────────────────
#
# An OUR-number-vs-the-market check, built from the team-comparison
# stats the dossier already fetches (zero extra API calls, zero cost).
# Crude (no SOS, no Elo) but genuinely independent of the betting line —
# so when it AGREES with the sharp side it's a confirmation we credit
# toward sizing, and when it DISAGREES it's a caution flag. v1 projects a
# margin from offense-vs-defense season averages, converts to a win prob
# via a per-sport logistic, and compares to PIN's devigged fair.
#   off  — team-compare key for points/runs/goals scored per game
#   def  — team-compare key for points/runs/goals allowed per game
#   hfa  — home-field advantage in those same units
#   scale— logistic scale (margin that moves win-prob ~1 logit)
_POWER_MODELS = {
    "MLB":   {"off": "rpg",                "def": "era",               "hfa": 0.20, "scale": 1.6},
    "NBA":   {"off": "avgPoints",          "def": "avgPointsAgainst",  "hfa": 2.5,  "scale": 7.0},
    "CBB":   {"off": "avgPoints",          "def": "avgPointsAgainst",  "hfa": 3.5,  "scale": 7.5},
    "NFL":   {"off": "totalPointsPerGame", "def": "avgPointsAgainst",  "hfa": 2.0,  "scale": 8.0},
    "NCAAF": {"off": "totalPointsPerGame", "def": "avgPointsAgainst",  "hfa": 3.0,  "scale": 9.0},
    "NHL":   {"off": "avgGoals",           "def": "avgGoalsAgainst",   "hfa": 0.20, "scale": 1.6},
}


def _pr_attach_market_compare(block: dict, odds: dict | None,
                              p_home: float, p_away: float,
                              proj_total: float) -> dict:
    """Attach model-vs-PIN edges + total lean to a power-rating block.
    Shared by the v1 (raw-stat) and v2 (opponent-adjusted) projections so
    both produce the identical shape the UI + sizing expect."""
    ml = (odds or {}).get("moneyline") or {}
    pin_cur = ml.get("pin_current") or {}
    pin_home = (pin_cur.get("home") or {}).get("fair_prob")
    pin_away = (pin_cur.get("away") or {}).get("fair_prob")
    if pin_home is not None:
        block["edge_home_pp"] = round((p_home - pin_home) * 100.0, 1)
    if pin_away is not None:
        block["edge_away_pp"] = round((p_away - pin_away) * 100.0, 1)

    tot = (odds or {}).get("total") or {}
    over_pin = (tot.get("pin_current") or {}).get("over") or {}
    tline = over_pin.get("line")
    if tline is not None:
        try:
            tl = float(tline)
            block["total_line"] = tl
            block["total_diff"] = round(proj_total - tl, 2)
            block["total_lean"] = ("over" if proj_total > tl
                                   else "under" if proj_total < tl else None)
        except (TypeError, ValueError):
            pass
    return block


def _power_rating_v1(sport: str, team_compare: dict | None,
                     odds: dict | None) -> dict | None:
    """v1 fallback — crude projection from raw team-compare season stats
    (no opponent adjustment). Used only when no opponent-adjusted ratings
    snapshot exists for the sport yet. Silent — never raises."""
    model = _POWER_MODELS.get(sport)
    if not (model and team_compare):
        return None
    vals: dict = {}
    for f in team_compare.get("fields") or []:
        vals[f.get("key")] = (f.get("away"), f.get("home"))
    off = vals.get(model["off"])
    deff = vals.get(model["def"])
    if not (off and deff):
        return None
    a_off, h_off = _to_float(off[0]), _to_float(off[1])
    a_def, h_def = _to_float(deff[0]), _to_float(deff[1])
    if None in (a_off, h_off, a_def, h_def):
        return None
    exp_home = (h_off + a_def) / 2.0
    exp_away = (a_off + h_def) / 2.0
    margin = (exp_home - exp_away) + model["hfa"]
    proj_total = exp_home + exp_away
    try:
        p_home = 1.0 / (1.0 + math.exp(-margin / model["scale"]))
    except OverflowError:
        p_home = 1.0 if margin > 0 else 0.0
    p_home = min(max(p_home, 0.01), 0.99)
    p_away = 1.0 - p_home
    block: dict = {
        "exp_home":          round(exp_home, 2),
        "exp_away":          round(exp_away, 2),
        "proj_margin_home":  round(margin, 2),
        "proj_total":        round(proj_total, 2),
        "p_home":            round(p_home, 4),
        "p_away":            round(p_away, 4),
        "fair_home_american": _prob_to_american(p_home),
        "fair_away_american": _prob_to_american(p_away),
        "source":            "v1-stats",
    }
    return _pr_attach_market_compare(block, odds, p_home, p_away, proj_total)


def _load_power_snapshot(sb, sport: str) -> dict | None:
    """Latest opponent-adjusted ratings snapshot for a sport (written by
    the kahla-scanner compute_power_ratings cron). None if absent."""
    try:
        rows = (sb.table("power_ratings")
                .select("league_avg,ratings,params,n_games,computed_at")
                .eq("sport", sport)
                .order("computed_at", desc=True)
                .limit(1).execute().data) or []
    except Exception:
        return None
    return rows[0] if rows else None


def _pr_find_team(ratings: dict, name: str | None) -> dict | None:
    if not name:
        return None
    if name in ratings:
        return ratings[name]
    nl = name.lower()
    for k, v in ratings.items():
        kl = (k or "").lower()
        if kl and (nl in kl or kl in nl):
            return v
    return None


def _pr_find_key(ratings: dict, name: str | None) -> str | None:
    """The canonical game_results team name for a dossier team — so the
    rest/schedule query matches exactly. Same substring logic as
    _pr_find_team but returns the key."""
    if not name:
        return None
    if name in ratings:
        return name
    nl = name.lower()
    for k in ratings:
        kl = (k or "").lower()
        if kl and (nl in kl or kl in nl):
            return k
    return None


# Rest / schedule fatigue. Only sports where the second night of a
# back-to-back is a real, measurable drag (daily-grind MLB doesn't apply —
# "days rest" isn't a fatigue signal when everyone plays every day; its
# fatigue lives in the bullpen instead). Margin penalty in the sport's
# scoring units, applied to a team caught on a B2B vs a rested opponent.
_REST_PARAMS = {
    "NBA": {"b2b_margin": 2.0},    # second night ≈ -2 pts
    "NHL": {"b2b_margin": 0.30},   # tired legs/goalie ≈ -0.3 goals
}


def _rest_days(sb, team_key: str | None, event_start_iso: str | None) -> int | None:
    """Calendar days since the team's last completed game before this one,
    from game_results. None when unknown. Silent — never raises."""
    if not (team_key and event_start_iso):
        return None
    try:
        rows = (sb.table("game_results")
                .select("event_start")
                .or_(f"home.eq.{team_key},away.eq.{team_key}")
                .lt("event_start", event_start_iso)
                .order("event_start", desc=True)
                .limit(1).execute().data) or []
    except Exception:
        return None
    if not rows:
        return None
    try:
        g = datetime.fromisoformat(event_start_iso.replace("Z", "+00:00"))
        l = datetime.fromisoformat(str(rows[0]["event_start"]).replace("Z", "+00:00"))
        return (g.date() - l.date()).days
    except Exception:
        return None


# Injuries → rating. The biggest free lever — a key player sitting is the
# largest single mover, and we already FETCH ESPN injuries. Two flavors:
#   • NBA: value an OUT player by his scoring (PPG from ESPN team leaders),
#     dock his team's offense by a fraction of it, AND raise the OPPONENT's
#     offense by a smaller fraction (a two-way star's DEFENSE is gone too —
#     the asymmetry fix: a defensive anchor out helps the other team score).
#   • NFL/NCAAF: a QB out is the dominant football injury (≈ a touchdown of
#     line value). We fire a fixed offense dock only when the team's PASSING
#     leader (the starter) is the one ruled out — so a 3rd-string QB on the
#     report doesn't trigger it.
# MLB is dominated by the starter we already model (+ the lineup layer);
# NHL value lives in the goalie layer. Heavily guarded everywhere.
_INJURY_NBA_SPORTS = {"NBA"}
_INJURY_QB_SPORTS  = {"NFL", "NCAAF"}
# Net team-scoring hit is FAR less than an out player's raw PPG —
# replacements score and usage redistributes. A star's on/off net is ~25%
# of his points, so a 27-PPG star out ≈ a ~6.75-pt offense hit (defensible)
# rather than 13.5. Capped per side so multiple guys out can't nuke it.
_INJURY_FACTOR    = 0.25
_INJURY_MAX_PTS   = 10.0
# Asymmetry: part of a two-way star's value is defense — when he's out his
# team's D weakens, so the OPPONENT scores more. We don't have per-player
# defensive metrics free, so approximate it as a fraction of the (PPG-based)
# offensive hit. Crude but directionally right + bounded.
_INJURY_DEF_SHARE = 0.35
_QB_OUT_PTS       = 6.5     # starter→backup QB dropoff in projected points


def _espn_leaders(sport: str, team_id: str | None, keyword: str,
                  alt: tuple = ()) -> dict:
    """{player_name_lower: stat_value} from ESPN's team-leaders block for
    the category whose name contains `keyword` (e.g. 'point' for NBA PPG,
    'passing' for the starting QB). One call. Empty on any failure."""
    pair = _ESPN_PATH.get(sport)
    if not (pair and team_id):
        return {}
    grp, lg = pair
    url = f"https://site.api.espn.com/apis/site/v2/sports/{grp}/{lg}/teams/{team_id}"
    data = _http_get(url)
    if not data:
        return {}
    team = data.get("team") or {}
    out: dict = {}
    for cat in team.get("leaders") or []:
        nm = (cat.get("name") or cat.get("abbreviation") or "").lower()
        if keyword not in nm and nm not in alt:
            continue
        for ld in cat.get("leaders") or []:
            ath = ld.get("athlete") or {}
            name = (ath.get("displayName") or "").lower()
            val = _to_float(ld.get("displayValue")) or _to_float(ld.get("value"))
            if name and val is not None:
                out[name] = val
    return out


def _espn_scoring_leaders(sport: str, team_id: str | None) -> dict:
    return _espn_leaders(sport, team_id, "point", ("scoring", "ppg", "pts"))


def _injury_out(status: str | None) -> bool:
    """True when an ESPN injury status means the player won't play.
    Day-to-day / questionable / probable are uncertain — don't count."""
    s = (status or "").lower()
    return ("out" in s or "injured reserve" in s or "season" in s
            or "suspension" in s or "-day-il" in s)


def _match_leader(name: str, leaders: dict):
    """Resolve an injured player's name to a leaders-dict value via exact
    then two-way substring containment. None when no match."""
    nm = (name or "").lower()
    if not nm:
        return None
    if nm in leaders:
        return leaders[nm]
    for lname, v in leaders.items():
        if nm in lname or lname in nm:
            return v
    return None


def _injury_penalties(sport: str, espn_block: dict | None) -> dict | None:
    """Per-side injury adjustment. Returns offense docks (`home`/`away`),
    opponent-scoring bumps from lost defense (`home_def_loss`/`away_def_loss`,
    NBA only), and the out-player name lists. None when nothing applies.
    Guarded — a missing leaders fetch yields a 0 for that side."""
    if not espn_block:
        return None
    nba = sport in _INJURY_NBA_SPORTS
    qb  = sport in _INJURY_QB_SPORTS
    if not (nba or qb):
        return None
    dock: dict = {}
    dloss: dict = {}
    notes: dict = {}
    for side in ("home", "away"):
        tb = espn_block.get(side) or {}
        p, dl, outs = 0.0, 0.0, []
        injuries = [i for i in (tb.get("injuries") or []) if _injury_out(i.get("status"))]
        if nba:
            leaders = _espn_scoring_leaders(sport, tb.get("id"))
            for inj in injuries:
                ppg = _match_leader(inj.get("name"), leaders) if leaders else None
                if ppg is not None:
                    p += ppg * _INJURY_FACTOR
                    dl += ppg * _INJURY_FACTOR * _INJURY_DEF_SHARE
                    outs.append(inj.get("name"))
        elif qb:
            qbs = _espn_leaders(sport, tb.get("id"), "passing")
            for inj in injuries:
                if (inj.get("pos") or "").upper() != "QB":
                    continue
                if qbs and _match_leader(inj.get("name"), qbs) is not None:
                    p += _QB_OUT_PTS          # starter QB out
                    outs.append(inj.get("name"))
                    break                      # one QB hit per team
        dock[side]  = round(min(p, _INJURY_MAX_PTS), 2)
        dloss[side] = round(dl, 2)
        notes[side] = outs
    if not (dock.get("home") or dock.get("away")):
        return None
    return {"home": dock["home"], "away": dock["away"],
            "home_def_loss": dloss["home"], "away_def_loss": dloss["away"],
            "home_out": notes["home"], "away_out": notes["away"]}


# MLB starting-pitcher blend. The starter pitches ~6 of 9 innings, so he
# carries ~60% of run prevention on his day; the rest is bullpen (≈ team
# def). ERA is regressed toward league average by innings pitched so a
# tiny early-season / just-recalled sample (e.g. a 5-IP 5.40) doesn't
# dominate — a full-season workload trusts the ERA, a sliver regresses
# most of the way back to average.
_SP_INNINGS_SHARE = 0.6
_SP_IP_REGRESS    = 45.0          # innings at which the rate gets ~half its weight
_SP_ERA_CLAMP     = (1.5, 8.0)    # sane bounds on a starter's runs/9
# FIP (fielding-independent pitching) is built only from K / BB / HR — the
# outcomes a pitcher controls — so it predicts future run prevention
# better than past ERA and stabilizes much faster on small samples (it
# strips out defense + sequencing luck). We blend it with ERA as the
# starter's true-talent run rate. Constant ~3.15 puts FIP on the ERA scale.
_FIP_CONSTANT = 3.15
_FIP_WEIGHT   = 0.6               # FIP favored over ERA (more predictive)


def _ip_to_float(ip) -> float | None:
    """MLB innings-pitched are stored ballpark-style: '51.1' = 51 ⅓
    innings (.1 = 1 out, .2 = 2 outs), NOT 51.1 decimal. Parse correctly."""
    if ip is None:
        return None
    try:
        s = str(ip)
        if "." in s:
            whole, frac = s.split(".", 1)
            outs = int(frac[0]) if frac and frac[0] in "012" else 0
            return float(whole or 0) + outs / 3.0
        return float(s)
    except (TypeError, ValueError):
        return None


def _fip(pitcher: dict | None) -> float | None:
    """Fielding-independent pitching from the starter's per-9 peripherals
    (which the dossier already fetches). FIP = (13·HR9 + 3·BB9 − 2·K9)/9 +
    constant. None when any peripheral is missing."""
    if not pitcher:
        return None
    k9 = _to_float(pitcher.get("k_per_9"))
    bb9 = _to_float(pitcher.get("bb_per_9"))
    hr9 = _to_float(pitcher.get("hr_per_9"))
    if None in (k9, bb9, hr9):
        return None
    return (13.0 * hr9 + 3.0 * bb9 - 2.0 * k9) / 9.0 + _FIP_CONSTANT


def _starter_runs(pitcher: dict | None, league_pitch: float) -> float | None:
    """A starter's expected runs-allowed-per-9: a FIP/ERA talent blend
    regressed toward the league baseline by innings pitched. FIP leads
    (more predictive, stabilizes faster) but falls back to ERA when the
    peripherals are missing. None when there's no usable ERA."""
    if not pitcher:
        return None
    era = _to_float(pitcher.get("era"))
    if era is None:
        return None
    clamp = lambda v: min(max(v, _SP_ERA_CLAMP[0]), _SP_ERA_CLAMP[1])
    era = clamp(era)
    fip = _fip(pitcher)
    talent = clamp(_FIP_WEIGHT * fip + (1.0 - _FIP_WEIGHT) * era) if fip is not None else era
    ip = _ip_to_float(pitcher.get("ip")) or 0.0
    reliability = ip / (ip + _SP_IP_REGRESS)
    return reliability * talent + (1.0 - reliability) * league_pitch


# Bullpen — the SP blend covers ~60% of innings (the starter); the other
# ~40% is the bullpen, which the SP-blend approximates with the full-staff
# `team_def` rating. That's a proxy: a great rotation can mask a leaky pen
# and vice-versa. MLB Stats API exposes a reliever-only season split in ONE
# call (statSplits + sitCodes=rp), so we can use the real bullpen ERA for
# the non-starter share instead of the whole-staff number. Guarded — None
# on any failure → caller keeps the team_def proxy.
def _mlb_bullpen_era(team_id, season: int) -> float | None:
    if not team_id:
        return None
    url = f"https://statsapi.mlb.com/api/v1/teams/{team_id}/stats"
    data = _http_get(url, params={"stats": "statSplits", "sitCodes": "rp",
                                  "group": "pitching", "season": season,
                                  "gameType": "R"})
    if not data:
        return None
    try:
        splits = ((data.get("stats") or [{}])[0].get("splits") or [])
        for sp in splits:
            era = _to_float((sp.get("stat") or {}).get("era"))
            if era is not None:
                return era
    except Exception:
        return None
    return None


# NHL goalie layer — the starting goalie is to hockey what the starter is
# to baseball: he carries most of goal prevention on his night, and the
# season-long team `def` rating is blind to who's actually in the crease.
# We blend the team's #1 goalie's GAA (ESPN team leaders) 50/50 with the
# team_def rating. When the goalie is the usual starter, GAA ≈ what team_def
# already reflects → the blend is ~neutral (no harm). When a backup with a
# worse GAA gets the net, it correctly raises the opponent's expected goals.
# A confirmed-starter feed would be better (catches the specific backup
# start) — this v1 uses the GAA leader as the likely #1. Guarded → no-op.
def _nhl_goalies(espn_block: dict | None) -> dict | None:
    if not espn_block:
        return None
    out: dict = {}
    for side in ("home", "away"):
        tb = espn_block.get(side) or {}
        gaa_by = _espn_leaders("NHL", tb.get("id"), "average",
                               ("goalsagainstaverage", "gaa"))
        if not gaa_by:
            out[side] = None
            out[side + "_name"] = None
            continue
        name, gaa = next(iter(gaa_by.items()))   # leaders are ranked; #1 first
        out[side] = gaa
        out[side + "_name"] = name
    if out.get("home") is None and out.get("away") is None:
        return None
    return out


# MLB park run factors — environment multiplier on projected scoring
# (100 = neutral; >100 hitter-friendly, <100 pitcher-friendly). Applied
# to BOTH teams' expected runs, so it mostly moves the TOTAL (Coors vs
# Petco is a huge swing) with a small amplification of the margin.
# Approximate 3-year run factors from public park-factor data; keyed by
# the HOME team (the park is theirs). Unknown parks default to 100.
_MLB_PARK_FACTORS: dict[str, float] = {
    "rockies": 112, "red sox": 104, "reds": 104, "yankees": 103,
    "phillies": 102, "orioles": 101, "rangers": 101, "cubs": 101,
    "diamondbacks": 101, "royals": 101, "angels": 100, "braves": 100,
    "blue jays": 100, "nationals": 100, "twins": 100, "white sox": 100,
    "astros": 99, "dodgers": 99, "cardinals": 99, "brewers": 99,
    "pirates": 98, "mets": 98, "guardians": 98, "marlins": 97,
    "rays": 97, "tigers": 97, "athletics": 97, "padres": 96,
    "giants": 95, "mariners": 94,
}


def _park_factor(home: str | None) -> float:
    if not home:
        return 100.0
    hl = home.lower()
    for key, pf in _MLB_PARK_FACTORS.items():
        if key in hl:
            return float(pf)
    return 100.0


def _power_rating_v2(sb, sport: str, odds: dict | None,
                     away: str | None, home: str | None,
                     pitchers: dict | None = None,
                     event_start: str | None = None,
                     injuries: dict | None = None,
                     goalies: dict | None = None,
                     lineup: dict | None = None) -> dict | None:
    """The real model — projects from the cron-computed opponent-adjusted
    ratings snapshot. Flask reads the precomputed ratings (it can't import
    the kahla-scanner engine) and does the lightweight off/def → margin/
    total projection inline. For MLB, the opponent's run-prevention is
    blended with TONIGHT's starting pitcher (regressed for sample) so a
    Ginn-vs-Giolito mismatch actually moves the number instead of being
    invisible. None when no snapshot or teams unmatched."""
    snap = _load_power_snapshot(sb, sport)
    if not snap:
        return None
    ratings = snap.get("ratings") or {}
    league_avg = _to_float(snap.get("league_avg")) or 0.0
    params = snap.get("params") or {}
    hfa = _to_float(params.get("hfa")) or 0.0
    scale = _to_float(params.get("scale")) or 1.0
    h = _pr_find_team(ratings, home)
    a = _pr_find_team(ratings, away)
    if not (h and a):
        return None
    h_off, h_def = _to_float(h.get("off")), _to_float(h.get("def"))
    a_off, a_def = _to_float(a.get("off")), _to_float(a.get("def"))
    if None in (h_off, h_def, a_off, a_def):
        return None

    # MLB pitcher adjustment — the starter dominates run prevention on his
    # day, and the season-long team `def` rating is blind to who's actually
    # on the mound tonight. Blend the opponent's team defense with the
    # opposing starter's (sample-regressed) ERA on the runs scale. Each
    # team's offense is projected against the OPPONENT's starter.
    sp_note = None
    bp_note = None
    if sport == "MLB" and pitchers:
        season = datetime.now(timezone.utc).year
        away_sp = _starter_runs(pitchers.get("away"), league_avg)
        home_sp = _starter_runs(pitchers.get("home"), league_avg)
        # Real reliever ERA for the ~40% non-starter innings, lightly
        # regressed toward the full-staff rating to temper a thin/early
        # sample. Falls back to team_def when the split is unavailable.
        away_bp = _mlb_bullpen_era(pitchers.get("away_team_id"), season)
        home_bp = _mlb_bullpen_era(pitchers.get("home_team_id"), season)
        a_pen = (0.75 * away_bp + 0.25 * a_def) if away_bp is not None else a_def
        h_pen = (0.75 * home_bp + 0.25 * h_def) if home_bp is not None else h_def
        if away_sp is not None:
            a_def = _SP_INNINGS_SHARE * away_sp + (1 - _SP_INNINGS_SHARE) * a_pen
        if home_sp is not None:
            h_def = _SP_INNINGS_SHARE * home_sp + (1 - _SP_INNINGS_SHARE) * h_pen
        if away_sp is not None or home_sp is not None:
            sp_note = {"away_sp_runs": (round(away_sp, 2) if away_sp is not None else None),
                       "home_sp_runs": (round(home_sp, 2) if home_sp is not None else None)}
        if away_bp is not None or home_bp is not None:
            bp_note = {"away_bp_era": (round(away_bp, 2) if away_bp is not None else None),
                       "home_bp_era": (round(home_bp, 2) if home_bp is not None else None)}

    # NHL goalie — blend the starting goalie's GAA into his team's defense
    # (50/50 with team_def). Neutral when it's the usual starter; raises the
    # opponent's expected goals when a worse-GAA backup is in the crease.
    goalie_note = None
    if sport == "NHL" and goalies:
        ag, hg = goalies.get("away"), goalies.get("home")
        if ag is not None:
            a_def = 0.5 * ag + 0.5 * a_def
        if hg is not None:
            h_def = 0.5 * hg + 0.5 * h_def
        if ag is not None or hg is not None:
            goalie_note = {"away_gaa": (round(ag, 2) if ag is not None else None),
                           "home_gaa": (round(hg, 2) if hg is not None else None),
                           "away_name": goalies.get("away_name"),
                           "home_name": goalies.get("home_name")}

    exp_home = h_off + (a_def - league_avg) + hfa / 2.0
    exp_away = a_off + (h_def - league_avg) - hfa / 2.0

    # Injuries — dock the injured team's offense, AND (NBA) raise the
    # OPPONENT's offense by the lost-defense share (a two-way star out
    # weakens his team's D, so the other team scores more).
    inj_note = None
    if injuries:
        hp = _to_float(injuries.get("home")) or 0.0
        ap = _to_float(injuries.get("away")) or 0.0
        h_dloss = _to_float(injuries.get("home_def_loss")) or 0.0
        a_dloss = _to_float(injuries.get("away_def_loss")) or 0.0
        if hp or ap or h_dloss or a_dloss:
            exp_home = max(0.0, exp_home - hp + a_dloss)   # away D weakened → home scores more
            exp_away = max(0.0, exp_away - ap + h_dloss)   # home D weakened → away scores more
            inj_note = {"home": round(hp, 2), "away": round(ap, 2),
                        "home_def_loss": round(h_dloss, 2),
                        "away_def_loss": round(a_dloss, 2),
                        "home_out": injuries.get("home_out") or [],
                        "away_out": injuries.get("away_out") or []}

    # MLB lineup — dock projected runs for top-OPS regulars resting tonight.
    lineup_note = None
    if sport == "MLB" and lineup:
        lh = _to_float(lineup.get("home")) or 0.0
        la = _to_float(lineup.get("away")) or 0.0
        if lh or la:
            exp_home = max(0.0, exp_home - lh)
            exp_away = max(0.0, exp_away - la)
            lineup_note = {"home": round(lh, 2), "away": round(la, 2),
                           "home_out": lineup.get("home_out") or [],
                           "away_out": lineup.get("away_out") or []}

    # MLB park factor — scale both sides' expected runs by the venue's
    # run environment (Coors inflates, Petco suppresses). Moves the total
    # most; lightly amplifies the margin.
    park_factor = None
    if sport == "MLB":
        pf = _park_factor(home)
        if pf and pf != 100.0:
            exp_home *= pf / 100.0
            exp_away *= pf / 100.0
        park_factor = pf

    margin = exp_home - exp_away
    proj_total = exp_home + exp_away

    # Rest / schedule — penalize a team caught on the second night of a
    # back-to-back vs a rested opponent (NBA/NHL). Margin-only; total left
    # alone. Two cheap game_results lookups, gated to those sports.
    rest_note = None
    rp = _REST_PARAMS.get(sport)
    if rp and event_start:
        hk = _pr_find_key(ratings, home)
        ak = _pr_find_key(ratings, away)
        hr = _rest_days(sb, hk, event_start)
        ar = _rest_days(sb, ak, event_start)
        if hr is not None and ar is not None:
            h_b2b, a_b2b = hr <= 1, ar <= 1
            pen = rp["b2b_margin"]
            if h_b2b and not a_b2b:
                margin -= pen
            elif a_b2b and not h_b2b:
                margin += pen
            rest_note = {"home_rest": hr, "away_rest": ar,
                         "home_b2b": h_b2b, "away_b2b": a_b2b}

    try:
        p_home = 1.0 / (1.0 + math.exp(-margin / scale)) if scale > 0 else (
            1.0 if margin > 0 else 0.0)
    except OverflowError:
        p_home = 1.0 if margin > 0 else 0.0
    p_home = min(max(p_home, 0.01), 0.99)
    p_away = 1.0 - p_home
    block: dict = {
        "exp_home":          round(exp_home, 2),
        "exp_away":          round(exp_away, 2),
        "proj_margin_home":  round(margin, 2),
        "proj_total":        round(proj_total, 2),
        "p_home":            round(p_home, 4),
        "p_away":            round(p_away, 4),
        "fair_home_american": _prob_to_american(p_home),
        "fair_away_american": _prob_to_american(p_away),
        "home_net":          _to_float(h.get("net")),
        "away_net":          _to_float(a.get("net")),
        "source":            "v2-adjusted",
        "n_games":           snap.get("n_games"),
        "sp_adjusted":       sp_note is not None,
        "sp":                sp_note,
        "bp_adjusted":       bp_note is not None,
        "bp":                bp_note,
        "goalie":            goalie_note,
        "park_factor":       park_factor,
        "rest":              rest_note,
        "injuries":          inj_note,
        "lineup":            lineup_note,
    }
    return _pr_attach_market_compare(block, odds, p_home, p_away, proj_total)


def _power_rating(sb, sport: str, team_compare: dict | None,
                  odds: dict | None, away: str | None = None,
                  home: str | None = None,
                  pitchers: dict | None = None,
                  event_start: str | None = None,
                  injuries: dict | None = None,
                  goalies: dict | None = None,
                  lineup: dict | None = None) -> dict | None:
    """Prefer the real opponent-adjusted ratings (cron-computed snapshot);
    fall back to the v1 raw-season-stat projection when no snapshot exists
    for the sport yet, or the teams aren't in it. Silent — never raises."""
    try:
        v2 = _power_rating_v2(sb, sport, odds, away, home, pitchers,
                              event_start, injuries, goalies, lineup)
    except Exception:
        v2 = None
    block = v2 or _power_rating_v1(sport, team_compare, odds)
    if block is not None:
        # Per-sport sizing gate: only sports that cleared the backtest
        # feed the Kelly edge. Everywhere else the card still shows but
        # contributes 0 (see MODEL_SIZING_SPORTS).
        block["feeds_sizing"] = sport in MODEL_SIZING_SPORTS
    return block


def _model_edge_for_side(power: dict | None, market_type: str,
                         side: str) -> float:
    """Power-rating edge contribution for a candidate side, in fair-prob
    pp, credited only when the model AGREES with the side (confirmation
    bonus — we never let a crude model talk us INTO a side it dislikes,
    and never bet harder against our own number). 0 when no model, no
    agreement, or the sport hasn't cleared the backtest gate
    (`power.feeds_sizing` — set per-sport in _power_rating). So MLB/NHL
    models show on the card but contribute 0 to the edge."""
    if not power or not MODEL_FEEDS_SIZING or not power.get("feeds_sizing"):
        return 0.0
    if market_type in ("moneyline", "spread"):
        e = power.get(f"edge_{side}_pp")
        if e is not None and e > 0:
            return min(e * MODEL_EDGE_WEIGHT, MODEL_EDGE_CAP_PP)
        return 0.0
    if market_type == "total":
        if power.get("total_lean") == side:
            # Each point of model-vs-line gap → a modest edge nudge.
            nudge = abs(power.get("total_diff") or 0.0) * MODEL_EDGE_WEIGHT
            return min(nudge, MODEL_EDGE_CAP_PP)
        return 0.0
    return 0.0


# ──────────────────────────── Weather ────────────────────────────
#
# Outdoor-sport weather for the dossier — wind / temp / precip at the
# venue near first pitch / kickoff. Free via Open-Meteo (no API key, no
# signup, no quota). Only MLB + NFL are modeled — they're the
# weather-sensitive outdoor sports we can enumerate stadiums for; indoor
# sports (NBA / NHL / CBB) and the hundreds of NCAAF venues are skipped.
# Climate-controlled domes / fixed-roof / usually-closed-retractable
# stadiums are flagged dome=True and skip the fetch entirely. v1 reports
# the conditions as a reference card (the analyst / Claude reads it); it
# does NOT auto-size off wind — that needs park orientation we don't
# encode yet.
#   key (mascot substring of the home team name) → (lat, lon, name, dome)
_MLB_PARKS: dict[str, tuple] = {
    "diamondbacks": (33.45, -112.07, "Chase Field", True),
    "braves":       (33.89, -84.47,  "Truist Park", False),
    "orioles":      (39.28, -76.62,  "Camden Yards", False),
    "red sox":      (42.35, -71.10,  "Fenway Park", False),
    "cubs":         (41.95, -87.66,  "Wrigley Field", False),
    "white sox":    (41.83, -87.63,  "Rate Field", False),
    "reds":         (39.10, -84.51,  "Great American Ball Park", False),
    "guardians":    (41.50, -81.69,  "Progressive Field", False),
    "rockies":      (39.76, -104.99, "Coors Field", False),
    "tigers":       (42.34, -83.05,  "Comerica Park", False),
    "astros":       (29.76, -95.36,  "Daikin Park", True),
    "royals":       (39.05, -94.48,  "Kauffman Stadium", False),
    "angels":       (33.80, -117.88, "Angel Stadium", False),
    "dodgers":      (34.07, -118.24, "Dodger Stadium", False),
    "marlins":      (25.78, -80.22,  "loanDepot park", True),
    "brewers":      (43.03, -87.97,  "American Family Field", True),
    "twins":        (44.98, -93.28,  "Target Field", False),
    "mets":         (40.76, -73.85,  "Citi Field", False),
    "yankees":      (40.83, -73.93,  "Yankee Stadium", False),
    "athletics":    (38.58, -121.51, "Sutter Health Park", False),
    "phillies":     (39.91, -75.17,  "Citizens Bank Park", False),
    "pirates":      (40.45, -80.01,  "PNC Park", False),
    "padres":       (32.71, -117.16, "Petco Park", False),
    "giants":       (37.78, -122.39, "Oracle Park", False),
    "mariners":     (47.59, -122.33, "T-Mobile Park", False),
    "cardinals":    (38.62, -90.19,  "Busch Stadium", False),
    "rays":         (27.77, -82.65,  "Tropicana Field", True),
    "rangers":      (32.75, -97.08,  "Globe Life Field", True),
    "blue jays":    (43.64, -79.39,  "Rogers Centre", True),
    "nationals":    (38.87, -77.01,  "Nationals Park", False),
}
_NFL_STADIUMS: dict[str, tuple] = {
    "cardinals":   (33.53, -112.26, "State Farm Stadium", True),
    "falcons":     (33.76, -84.40,  "Mercedes-Benz Stadium", True),
    "ravens":      (39.28, -76.62,  "M&T Bank Stadium", False),
    "bills":       (42.77, -78.79,  "Highmark Stadium", False),
    "panthers":    (35.23, -80.85,  "Bank of America Stadium", False),
    "bears":       (41.86, -87.62,  "Soldier Field", False),
    "bengals":     (39.10, -84.52,  "Paycor Stadium", False),
    "browns":      (41.51, -81.70,  "Huntington Bank Field", False),
    "cowboys":     (32.75, -97.09,  "AT&T Stadium", True),
    "broncos":     (39.74, -105.02, "Empower Field", False),
    "lions":       (42.34, -83.05,  "Ford Field", True),
    "packers":     (44.50, -88.06,  "Lambeau Field", False),
    "texans":      (29.68, -95.41,  "NRG Stadium", True),
    "colts":       (39.76, -86.16,  "Lucas Oil Stadium", True),
    "jaguars":     (30.32, -81.64,  "EverBank Stadium", False),
    "chiefs":      (39.05, -94.48,  "Arrowhead Stadium", False),
    "raiders":     (36.09, -115.18, "Allegiant Stadium", True),
    "chargers":    (33.95, -118.34, "SoFi Stadium", True),
    "rams":        (33.95, -118.34, "SoFi Stadium", True),
    "dolphins":    (25.96, -80.24,  "Hard Rock Stadium", False),
    "vikings":     (44.97, -93.26,  "U.S. Bank Stadium", True),
    "patriots":    (42.09, -71.26,  "Gillette Stadium", False),
    "saints":      (29.95, -90.08,  "Caesars Superdome", True),
    "giants":      (40.81, -74.07,  "MetLife Stadium", False),
    "jets":        (40.81, -74.07,  "MetLife Stadium", False),
    "eagles":      (39.90, -75.17,  "Lincoln Financial Field", False),
    "steelers":    (40.45, -80.02,  "Acrisure Stadium", False),
    "49ers":       (37.40, -121.97, "Levi's Stadium", False),
    "seahawks":    (47.60, -122.33, "Lumen Field", False),
    "buccaneers":  (27.98, -82.50,  "Raymond James Stadium", False),
    "titans":      (36.17, -86.77,  "Nissan Stadium", False),
    "commanders":  (38.91, -76.86,  "Northwest Stadium", False),
}
_WEATHER_CACHE: dict[tuple, tuple[float, dict]] = {}


def _stadium_coords(sport: str, home: str | None) -> dict | None:
    table = {"MLB": _MLB_PARKS, "NFL": _NFL_STADIUMS}.get(sport)
    if not (table and home):
        return None
    h = home.lower()
    for key, (lat, lon, name, dome) in table.items():
        if key in h:
            return {"lat": lat, "lon": lon, "name": name, "dome": dome}
    return None


def _compass(deg) -> str | None:
    d = _to_float(deg)
    if d is None:
        return None
    dirs = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
            "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
    return dirs[int((d % 360) / 22.5 + 0.5) % 16]


def _wmo_summary(code) -> str | None:
    c = _to_float(code)
    if c is None:
        return None
    c = int(c)
    if c == 0:                 return "Clear"
    if c in (1, 2, 3):         return "Partly cloudy"
    if c in (45, 48):          return "Fog"
    if 51 <= c <= 57:          return "Drizzle"
    if 61 <= c <= 67:          return "Rain"
    if 71 <= c <= 77:          return "Snow"
    if 80 <= c <= 82:          return "Rain showers"
    if 85 <= c <= 86:          return "Snow showers"
    if 95 <= c <= 99:          return "Thunderstorm"
    return None


def _fetch_weather(sport: str, home: str | None,
                   event_start: str | None) -> dict | None:
    """Open-Meteo conditions at the venue near event_start. Free, no key.
    Returns None for indoor/unmodeled sports; a {'dome': True, ...} block
    for climate-controlled venues (no fetch); otherwise temp/wind/precip
    at the nearest forecast hour. Silent on any failure."""
    coords = _stadium_coords(sport, home)
    if not coords:
        return None
    if coords.get("dome"):
        return {"dome": True, "stadium": coords["name"],
                "note": "Indoor / fixed roof — weather not a factor."}
    if not event_start:
        return None
    try:
        dt = (datetime.fromisoformat(event_start.replace("Z", "+00:00"))
              .astimezone(timezone.utc))
    except Exception:
        return None
    hour_key = dt.strftime("%Y-%m-%dT%H:00")
    ck = (round(coords["lat"], 3), round(coords["lon"], 3), hour_key)
    hit = _WEATHER_CACHE.get(ck)
    now = time.time()
    if hit and now - hit[0] < 1800:
        return hit[1]
    data = _http_get(
        "https://api.open-meteo.com/v1/forecast",
        params={
            "latitude": coords["lat"], "longitude": coords["lon"],
            "hourly": ("temperature_2m,precipitation_probability,"
                       "wind_speed_10m,wind_direction_10m,weather_code"),
            "temperature_unit": "fahrenheit", "wind_speed_unit": "mph",
            "timezone": "UTC", "forecast_days": 3,
        },
    )
    hourly = (data or {}).get("hourly") or {}
    times = hourly.get("time") or []
    if not times:
        return None
    idx = None
    if hour_key in times:
        idx = times.index(hour_key)
    else:
        best = None
        for i, t in enumerate(times):
            try:
                tt = datetime.fromisoformat(t).replace(tzinfo=timezone.utc)
            except Exception:
                continue
            diff = abs((tt - dt).total_seconds())
            if best is None or diff < best[0]:
                best = (diff, i)
        if best:
            idx = best[1]
    if idx is None:
        return None

    def _at(key):
        arr = hourly.get(key) or []
        return arr[idx] if idx < len(arr) else None

    out = {
        "dome":         False,
        "stadium":      coords["name"],
        "temp_f":       _at("temperature_2m"),
        "wind_mph":     _at("wind_speed_10m"),
        "wind_dir_deg": _at("wind_direction_10m"),
        "wind_dir":     _compass(_at("wind_direction_10m")),
        "precip_pct":   _at("precipitation_probability"),
        "summary":      _wmo_summary(_at("weather_code")),
        "for_hour_utc": hour_key,
    }
    _WEATHER_CACHE[ck] = (now, out)
    return out


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


def _suggest_picks(odds: dict, splits: dict | None = None,
                   power: dict | None = None) -> list[dict]:
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

    Sizing is quarter-Kelly off the signal-derived edge_pp (sharp +
    aligned splits + power-rating confirmation), snapped to tiers:
      gate not cleared (sharp < 4)       → 1u low (forced lean)
      gate cleared, ¼-Kelly < 2.5%BR     → 3u medium
      gate cleared, ¼-Kelly ≥ 2.5%BR     → 5u high
    Whale (10u) stays disabled. See _kelly_units / EDGE_* constants.
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

            # Provisional edge estimate (fair-prob pp) that drives Kelly
            # sizing + finally populates the edge_pp column. The sharp +
            # splits terms only count on the side the sharp money points
            # at; the power-rating term is a confirmation bonus when our
            # independent model agrees with this side.
            model_edge = _model_edge_for_side(power, mt, side)
            edge_pp = round(min(
                score_for_side * EDGE_PER_SHARP_POINT
                + max(0.0, splits_pp) * EDGE_PER_SPLITS_PP
                + model_edge,
                EDGE_CAP_PP,
            ), 2)

            # PMM-projected fair: if a Polymarket market exists for
            # this side AND the push-rate projection is applicable
            # (delta ≤ 0.5 between PIN and PMM lines), prefer the
            # projected fair at PMM's line as the limit-order target.
            # When projection ISN'T applicable (multi-point gap, missing
            # push rate), keep PIN's line + PIN fair as a coherent pair
            # so we never display "PMM line + PIN fair" together
            # (inconsistent and misleading).
            pmm_block = (blk.get("polymarket") or {}).get(side)
            pmm_proj = (pmm_block or {}).get("projected") or {}
            pmm_line = (pmm_block or {}).get("line")
            pmm_quote = (pmm_block or {}).get("quote") or {}
            projected_american = pmm_proj.get("fair_american")
            use_pmm = projected_american is not None
            target_fair_american = projected_american if use_pmm else fair_american
            target_line = pmm_line if use_pmm else pin.get("line")

            candidates.append({
                "market_type":    mt,
                "side":           side,
                "sharp_score":    score_for_side,
                "splits_pp":      round(splits_pp, 1),
                "edge_pp":        edge_pp,
                "model_edge_pp":  round(model_edge, 2),
                "fair_prob":      fair_prob,
                "fair_american":  target_fair_american,   # PMM-projected when applicable
                "pin_fair_american": fair_american,        # raw PIN fair at PIN's line, for reference
                "pin_current":    pin.get("price"),
                "pin_line":       pin.get("line"),
                "pmm_line":       pmm_line if use_pmm else None,  # only surface when projection was applied
                "pmm_ask_american": pmm_quote.get("ask_american"),
                "pmm_bid_american": pmm_quote.get("bid_american"),
                "pmm_mid_american": pmm_quote.get("mid_american"),
                "pmm_slug":       (pmm_block or {}).get("slug"),
                "uses_pmm_projection": use_pmm,
                "combined_score": round(cs, 4),
                "gates_cleared":  gates_cleared,
            })

    if not candidates:
        return []

    # Drop heavily-juiced SPR — it's a worse-EV restatement of the ML
    # bet on the same side. ML candidate (if any) takes its slot.
    candidates = [
        c for c in candidates
        if not (c["market_type"] == "spread"
                and c.get("fair_american") is not None
                and c["fair_american"] <= SPR_CHALK_FAIR_CAP)
    ]
    if not candidates:
        return []

    # Best candidate per market_type (with Kelly sizing applied).
    # Sizing is now quarter-Kelly off the signal-derived edge_pp, snapped
    # to the 1/3/5u tiers, replacing the old fixed "sharp≥5 AND splits≥5"
    # thresholds. The sharp gate (gates_cleared) still decides whether a
    # pick is real (≥3u) vs a forced 1u lean; Kelly only chooses 3u vs 5u
    # among gated picks. Whale (10u) stays disabled — live results showed
    # it was a FADE indicator (23% over 35 picks). The CLV column will
    # let Stage-4 recalibrate the edge coefficients from realized data.
    by_market: dict[str, dict] = {}
    for c in candidates:
        units, conf, kelly_pct = _kelly_units(
            c.get("fair_prob"), c.get("fair_american"),
            c.get("edge_pp"), c.get("gates_cleared"))
        c["units"], c["confidence"], c["kelly_pct"] = units, conf, kelly_pct
        cur = by_market.get(c["market_type"])
        if (not cur) or ((c["gates_cleared"], c["combined_score"]) >
                         (cur["gates_cleared"], cur["combined_score"])):
            by_market[c["market_type"]] = c

    # ML / SPR symmetric chalk filter — only one of them survives.
    # SPR is a leveraged restatement of the ML directional bet, so we
    # never want both:
    #   • ML fair ≤ -140 (chalky)   → drop ML, keep SPR (cleaner
    #     expression of a chalky directional bet at +EV prices).
    #   • ML fair > -140 (lighter)  → drop SPR, keep ML (the variance
    #     of the leveraged SPR isn't worth it when ML is reasonable;
    #     bet the cleaner side).
    # Runs BEFORE the gate-clearing step so SPR doesn't get into
    # `chosen` just because its sharp_score happens to be higher than
    # a non-sharp ML on a near-pickem game.
    ml_c = by_market.get("moneyline")
    sp_c = by_market.get("spread")
    if ml_c and sp_c:
        ml_fair = ml_c.get("fair_american")
        if ml_fair is not None and ml_fair <= ML_CHALK_FAIR_CAP:
            del by_market["moneyline"]
        else:
            del by_market["spread"]

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
    pin_op = _pin_history(sb, market["id"])

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

    # Polymarket lookup — the user bets on Polymarket, so we want the
    # actual PMM line + current bid/ask alongside PIN's devigged fair.
    # Plus we project PIN's fair onto PMM's line via push-rate math so
    # the dossier can show a meaningful limit-order target.
    # Lookup is silent-fail: any exception falls back to no PMM data.
    pmm_data: dict | None = None
    pmm_error: str | None = None
    pmm_diag: dict = {}
    if away and home and event_start:
        try:
            from app import get_client as _get_pmm_client  # lazy import: app.py
            import pmm_markets
            try:
                client = _get_pmm_client()
            except Exception as e:
                client = None
                pmm_error = f"pmm client unavailable: {str(e)[:120]}"
            if client:
                pmm_data = pmm_markets.lookup(
                    client, sport, away, home, event_start, diag=pmm_diag,
                )
                if not pmm_data:
                    pmm_error = "no matching PMM event"
        except Exception as e:
            pmm_error = f"pmm lookup failed: {str(e)[:160]}"
            pmm_data = None

    # Attach PMM market info onto each odds[market_type] block so the
    # UI can render PMM line + bid/ask next to PIN fair, plus the
    # PIN-derived fair projected onto PMM's line as the limit-order
    # target. Skipped silently when no PMM data.
    if pmm_data:
        _attach_pmm_to_odds(odds, pmm_data, sport)

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

    # Independent power rating — prefers the cron-computed opponent-
    # adjusted ratings snapshot (Supabase), falls back to the v1 raw-stat
    # projection. Plus outdoor-venue weather (free Open-Meteo). Silent-fail.
    injury_pen = _injury_penalties(sport, espn_block)
    goalie_blk = _nhl_goalies(espn_block) if sport == "NHL" else None
    _pp = (mlb_extra or {}).get("probable_pitchers") or {}
    lineup_dock = (_mlb_lineup_dock(_pp.get("game_pk"), _pp.get("away_team_id"),
                                    _pp.get("home_team_id"),
                                    datetime.now(timezone.utc).year)
                   if sport == "MLB" and _pp else None)
    power_rating = _power_rating(sb, sport, team_compare, odds, away, home,
                                 (mlb_extra or {}).get("probable_pitchers"),
                                 event_start, injury_pen, goalie_blk, lineup_dock)
    weather = _fetch_weather(sport, home, event_start) if (home and event_start) else None

    suggestions = _suggest_picks(odds, splits, power_rating)
    # Keep the singular `suggestion` field as an alias for the top pick
    # so any caller still expecting it doesn't break. New code should
    # use `suggestions` (list) so multi-pick games render correctly.
    suggestion = suggestions[0] if suggestions else None

    # Data freshness — two distinct signals so the UI can tell apart
    # "PIN hasn't moved" from "cron is broken":
    #   • pin_latest_captured: timestamp of the most recent PIN snap row.
    #     Because book_snapshots is dedup'd, this only advances when
    #     PIN ACTUALLY MOVES. A 12-min-old PIN snap on a near-tip game
    #     is normal if PIN held its line.
    #   • cron_last_run: timestamp of the most recent SUCCESSFUL Odds
    #     API ingest for this sport from odds_ingest_runs. Advances
    #     every 5 min near tip regardless of whether PIN moved.
    # If pin is old but cron is fresh: PIN just held steady. Healthy.
    # If both are old: cron is stuck. Look at odds_ingest_runs heartbeats.
    pin_latest_captured = None
    for (book, _mt, _side), snap in latest.items():
        if book != "PIN":
            continue
        ts = snap.get("captured_at")
        if not ts:
            continue
        if pin_latest_captured is None or str(ts) > str(pin_latest_captured):
            pin_latest_captured = ts

    cron_last_run = None
    try:
        res = (sb.table("odds_ingest_runs")
               .select("fetched_at")
               .eq("sport", sport)
               .eq("status", "ok")
               .order("fetched_at", desc=True)
               .limit(1)
               .execute().data) or []
        if res:
            cron_last_run = res[0].get("fetched_at")
    except Exception:
        # Table missing or query failure — fall through. The freshness
        # label just won't show the "cron Nm ago" half.
        pass

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
        "weather":         weather,
        "power_rating":    power_rating,
        "suggestion":      suggestion,
        "suggestions":     suggestions,
        "alt_matches":     [
            {"id": m["id"], "event_name": m["event_name"],
             "event_start": m["event_start"], "sport": m["sport"]}
            for m in alts[1:]
        ],
        "live_used":       live_used,
        "live_error":      live_error,
        "data_freshness":  {
            "pin_latest_captured": pin_latest_captured,
            "cron_last_run":       cron_last_run,
            "source":              "live" if live_used else "cached",
        },
        "pmm_meta":        {
            "matched":     bool(pmm_data),
            "event_slug":  (pmm_data or {}).get("event_slug"),
            "event_title": (pmm_data or {}).get("event_title"),
            "error":       pmm_error,
            # Diagnostic dump — what tag was used, time window, events
            # PMM returned, sample titles, classification results.
            # Surfaces here so the UI / Copy-for-Claude can show it for
            # iteration on matching/classification heuristics.
            "diag":        pmm_diag,
        },
        "generated_at":    datetime.now(timezone.utc).isoformat(),
    }
