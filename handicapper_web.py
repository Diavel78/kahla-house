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

import json
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
    (15,   0.75),   # was 1.00 — trimmed June 2026: a 118-pick review showed
    (60,   0.50),   # was 0.60   last-hour picks underperform, so the freshest
    (120,  0.35),   #            ticks were over-trusted as "steam" when they're
    (360,  0.18),   #            often retail noise. Kept mild (not gutted) so
    (1080, 0.08),   #            genuine prime-window steam still scores enough
)                   #            to size up. The pick-time cap is the precise lever.


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
        exch_current = blk.get("exch_current") or {}
        a, b = side_pairs[market_type]
        out: dict = {
            "event_slug":  pmm.get("event_slug"),
            "event_title": pmm.get("event_title"),
        }
        for side in (a, b):
            pin_side = pin_current.get(side) or {}
            exch_side = exch_current.get(side) or {}
            pin_fair_prob = pin_side.get("fair_prob")
            # Anchor the PMM line on the EXCHANGE at-the-money line
            # (exch_current) — the venue-native main line — falling back to
            # PIN's line only if no exchange anchor exists. Anchoring on the
            # frozen PIN line is what pinned totals to the wrong number
            # (e.g. 6.5 when PMM's balanced line is 7.5) post-cutover.
            anchor_line = exch_side.get("line")
            if anchor_line is None:
                anchor_line = pin_side.get("line")
            pin_line = anchor_line   # name kept for the projection call below
            entry = pmm_markets.best_line_for(pmm, pmm_key, side, anchor_line)
            if not entry:
                out[side] = None
                continue
            block: dict = {
                "line":  entry.get("line"),
                "slug":  entry.get("slug"),
                "title": entry.get("title"),
                "quote": entry.get("quote"),
                # The synthesized NO side shares the YES market's slug —
                # downstream book reads (make/take, book pressure) must
                # flip the ladder for synthetic sides or they score the
                # wrong side of the market.
                "synthetic": bool(entry.get("synthetic")),
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


# ───────── Exchange-based sharp score (Odds-API retirement, June 2026) ─────
# The PMM+Kalshi replacement for the PIN sharp score, built from
# pm_snapshots cent history. Runs SIDE-BY-SIDE with the PIN score during
# the cutover (subscription dies June 25): every market's movement block
# carries x_side/x_score, every suggestion carries x_agree, and the
# paperlog records both — so we can verify agreement on live games before
# flipping the primary. Same recency weights + 18h window as PIN.
#
# Design differences from the PIN version (deliberate):
# • Exchange cents ARE probability points; PIN's "cents" are American-odds
#   cents (~4 American cents per prob point near even money). _X_PP_SCALE
#   maps prob-point moves onto the same 0-10 score scale — a calibration
#   constant the side-by-side data will tune.
# • Exchanges quote MULTIPLE spread/total lines simultaneously, so there is
#   no discrete "the line moved" event — line migration shows up as cent
#   drift on the at-the-money line. The score therefore reads cents on the
#   MAIN line (latest cents closest to 50) only; no line-vs-vig split.
# • Kalshi ML cross-check: when Kalshi has its own read and it points the
#   OTHER way, return no signal (mirrors the cross-confirm AND-gate; also
#   absorbs the same-city PMM side-flip — a flipped feed disagrees, so we
#   skip rather than score the wrong side. Gotcha #21 spirit.)

# _X_PP_SCALE: prob-points → 0-10 score units. Bumped 4.0→5.2 (June 2026,
# cutover) to remove the systematic cold bias measured on the side-by-side
# data: over 87 paired paperlog rows the exchange score averaged 3.18 vs
# PIN's 4.10 (~0.92 cold), so a straight flip would have under-cleared the
# gate. 4.0 × (4.10/3.18 ≈ 1.29) ≈ 5.2 recenters the exchange score onto
# the scale the SHARP_SCORE_MIN gate was tuned for. Re-tune from the
# `signal_blob.book_imb` / paperlog distribution as the dataset grows.
_X_PP_SCALE = 5.2     # prob-points → 0-10 score units (bias-corrected)
_X_MIN_PP   = 0.25    # weighted move below this (≈ score 1) → no signal

# NOTE (July 2026): the unconfirmed-ML demotion is DATA-DRIVEN per game,
# not a per-sport allowlist. _xsharp_ml returns tri-state `confirmed`:
# False only when Kalshi actually QUOTES the game (kalshi rows exist in
# pm_snapshots) and stayed silent/flat — that's the 3-of-9 noise case.
# None (Kalshi has no rows for the game at all — unlisted sport, or a
# matcher miss) means "didn't confirm" carries no information, so no
# demotion: UFC/NCAAF MLs gate on PMM steam alone, and any sport Kalshi
# starts quoting gets the confirmation gate automatically the moment its
# rows appear. A brief KALSHI_CONFIRM_SPORTS static set lived here for a
# few hours before the tri-state replaced it.


def _pm_history(sb, market_id: str) -> dict:
    """pm_snapshots rows for one game in the last 18h, grouped by
    (source, market_type, side, line) and sorted ASC by captured_at."""
    cutoff = (datetime.now(timezone.utc)
              - timedelta(hours=_PIN_HISTORY_HOURS)).isoformat()
    try:
        rows = (sb.table("pm_snapshots")
                .select("source,market_type,side,line,cents,captured_at")
                .eq("market_id", market_id)
                .gte("captured_at", cutoff)
                .order("captured_at")
                .limit(3000)
                .execute().data) or []
    except Exception:
        return {}
    out: dict = {}
    for r in rows:
        line = r.get("line")
        try:
            line = round(float(line), 2) if line is not None else None
        except (TypeError, ValueError):
            line = None
        k = (r.get("source"), r.get("market_type") or "ml", r.get("side"), line)
        out.setdefault(k, []).append(r)
    return out


def _x_main_series(hist: dict, mt: str, side: str) -> list[dict]:
    """The side's MAIN-line series: among this side's PMM per-line series,
    the one whose LATEST cents is closest to 50 (at-the-money). For ML
    (line=None) there's exactly one series."""
    best, best_d = [], 1e9
    for (src, m, s, _line), snaps in hist.items():
        if src != "pmm" or m != mt or s != side or len(snaps) < 2:
            continue
        d = abs((snaps[-1].get("cents") or 50) - 50)
        if d < best_d:
            best, best_d = snaps, d
    return best


def _xsharp_ml(hist: dict, now=None):
    """(side, score, n_snaps, confirmed) or None. Sharp side = side whose
    implied prob ROSE (more favored = harder to bet), from PMM cents.

    Kalshi is the CONFIRMATION venue (ML-only — it's the one market both
    exchanges quote). TRI-STATE `confirmed` (July 2026 — data-driven, was a
    bool + a static sport allowlist):
      • Kalshi disagrees in direction → return None (no signal; gotcha #21
        spirit + absorbs the same-city PMM side-flip).
      • Kalshi agrees → confirmed=True.
      • Kalshi QUOTES the game but is silent/flat → confirmed=False
        (a real unconfirmed signal; the caller demotes it to a lean, per
        the live finding that one-venue moves win only ~3-of-9).
      • Kalshi has NO rows for this game AT ALL → confirmed=None
        (single-venue — Kalshi doesn't list it, or our matcher failed;
        either way "didn't confirm" carries no information, so the caller
        must NOT demote. This is what lets UFC/NCAAF MLs gate on PMM
        steam alone without a per-sport allowlist).
    """
    now = now or datetime.now(timezone.utc)
    h = hist.get(("pmm", "ml", "home", None), [])
    a = hist.get(("pmm", "ml", "away", None), [])
    sig, n = None, 0
    if len(h) >= 2:
        sig, n = _weighted_signed_delta(h, "cents", now=now), len(h)
    elif len(a) >= 2:   # complementary book — away up == home down
        sig, n = -_weighted_signed_delta(a, "cents", now=now), len(a)
    if sig is None or abs(sig) < _X_MIN_PP:
        return None
    kh = hist.get(("kalshi", "ml", "home", None), [])
    ka = hist.get(("kalshi", "ml", "away", None), [])
    if not kh and not ka:
        confirmed = None        # single-venue: Kalshi has no read at all
    else:
        confirmed = False
        kw = None
        if len(kh) >= 2:
            kw = _weighted_signed_delta(kh, "cents", now=now)
        elif len(ka) >= 2:      # complementary — away up == home down
            kw = -_weighted_signed_delta(ka, "cents", now=now)
        if kw is not None and abs(kw) >= _X_MIN_PP:
            if (kw > 0) != (sig > 0):
                return None     # feeds disagree → no signal (skip, don't guess)
            confirmed = True    # feeds agree → confirmed pick
    score = min(10, round(abs(sig) * _X_PP_SCALE))
    if score < 1:
        return None
    return ("home" if sig > 0 else "away"), score, n, confirmed


def _xsharp_two_sided(hist: dict, mt: str, up_side: str, down_side: str,
                      now=None):
    """Shared SPR/TOT scorer: cents on the at-the-money line of the
    reference side (home / over). Cents UP on the reference side = that
    side more likely = sharp; DOWN = the other side."""
    now = now or datetime.now(timezone.utc)
    snaps = _x_main_series(hist, mt, up_side)
    ref = up_side
    if not snaps:
        snaps, ref = _x_main_series(hist, mt, down_side), down_side
        if not snaps:
            return None
    w = _weighted_signed_delta(snaps, "cents", now=now)
    if ref == down_side:
        w = -w                 # normalize to the up_side's perspective
    if abs(w) < _X_MIN_PP:
        return None
    score = min(10, round(abs(w) * _X_PP_SCALE))
    if score < 1:
        return None
    return (up_side if w > 0 else down_side), score, len(snaps)


def _attach_xsharp(sb, market_id: str, odds: dict) -> None:
    """Attach x_side/x_score/x_n onto each market's movement block (creating
    the block when PIN had nothing — leans can then carry an x read too).
    Silent-fail; never breaks the dossier."""
    try:
        hist = _pm_history(sb, market_id)
        if not hist:
            return
        now = datetime.now(timezone.utc)
        results = {
            "moneyline": _xsharp_ml(hist, now),
            "spread":    _xsharp_two_sided(hist, "spread", "home", "away", now),
            "total":     _xsharp_two_sided(hist, "total", "over", "under", now),
        }
        for mt, r in results.items():
            blk = odds.get(mt)
            if not blk or not r:
                continue
            mv = blk.get("movement") or {}
            mv["x_side"], mv["x_score"], mv["x_n"] = r[0], r[1], r[2]
            # ML carries a confirmation flag (Kalshi agree). SPR/TOT have no
            # second venue, so they're treated as confirmed (the gate is
            # ML-only) — set True so the demotion never touches them.
            mv["x_confirmed"] = (r[3] if len(r) > 3 else True)
            blk["movement"] = mv
    except Exception:
        pass


# ───────── Exchange CURRENT fair anchor (Odds-API retirement) ─────────
# The replacement for PIN's devigged fair as the Polymarket limit-order
# target + edge base. Kalshi mid is the independent anchor where it quotes
# (ML); PMM mid elsewhere (SPR/TOT — Kalshi is ML-only). Built from the
# LATEST pm_snapshots cents per side, devigged. Attached as `exch_current`
# alongside `pin_current` so _suggest_picks reads it as primary (PIN
# falls back only while its feed is still warm).

def _exch_latest(sb, market_id: str) -> dict:
    """Latest pm_snapshots cents per (source, market_type, side, line). First
    row per key wins (rows come back newest-first). NO time cutoff — pm_snapshots
    is deduped-on-change, so a stable line's last row can be many hours/days old
    yet is STILL the current price (same anchor logic as PIN — gotchas #10/#29).
    A 6h window wrongly blanked the fair for quiet markets, most visibly World
    Cup, whose 1-X-2 cents sit stable for hours pre-kickoff (the "Netherlands @
    Sweden blank fair" bug: only a 07:37 snapshot existed, the price hadn't
    moved, and 6h hid it). Scoped to one market_id + limit, so it stays small."""
    try:
        rows = (sb.table("pm_snapshots")
                .select("source,market_type,side,line,cents,captured_at")
                .eq("market_id", market_id)
                .order("captured_at", desc=True)
                .limit(2000)
                .execute().data) or []
    except Exception:
        return {}
    out: dict = {}
    for r in rows:
        line = r.get("line")
        try:
            line = round(float(line), 2) if line is not None else None
        except (TypeError, ValueError):
            line = None
        k = (r.get("source"), r.get("market_type") or "ml", r.get("side"), line)
        if k not in out:
            try:
                out[k] = float(r.get("cents"))
            except (TypeError, ValueError):
                pass
    return out


def _exch_block(prob: float | None, line, source: str) -> dict | None:
    if prob is None:
        return None
    prob = min(max(prob, 0.01), 0.99)
    return {
        "price":         _prob_to_american(prob),
        "line":          line,
        "fair_prob":     round(prob, 4),
        "fair_american": _prob_to_american(prob),
        "source":        source,
    }


def _exch_ml_fair(latest: dict):
    """(source, p_home, p_away) from the latest ML cents — Kalshi first
    (the independent anchor), PMM fallback. Devigs the pair when both
    sides exist; else uses the single side directly."""
    for src in ("kalshi", "pmm"):
        h = latest.get((src, "ml", "home", None))
        a = latest.get((src, "ml", "away", None))
        if h is not None and a is not None and (h + a) > 0:
            ph = _devig_two_way(h / 100.0, a / 100.0)
            return src, ph, 1.0 - ph
        if h is not None:
            return src, h / 100.0, 1.0 - h / 100.0
        if a is not None:
            return src, 1.0 - a / 100.0, a / 100.0
    return None


def _exch_two_sided_fair(latest: dict, mt: str, up: str, down: str):
    """((up_line, down_line), p_up, p_down) for SPR/TOT from PMM's at-the-money
    line. Kalshi is ML-only, so PMM is the anchor.

    The two sides relate to the line DIFFERENTLY by market, and getting this
    wrong fabricates a phantom side:
      • TOTAL  — over and under SHARE the line (over 8.5 / under 8.5). Pair at
        the same L; both sides get line L.
      • SPREAD — the sides are MIRROR lines (home -1.5 ↔ away +1.5). The devig
        pair is home@L vs away@(-L), and EACH SIDE KEEPS ITS OWN LINE. The old
        code paired both sides at one shared L and stamped the away side with
        the HOME line — fabricating a 'Reds -1.5' runline that PMM never quoted
        (the +292 phantom). Mirror-pairing fixes it: away keeps +1.5.
    Devigs the pair when both exist; else the single side at its own line +
    the complement (stamped on the correct mirror line)."""
    if mt != "spread":
        lines = {ln for (src, m, s, ln) in latest
                 if src == "pmm" and m == mt and s in (up, down)}
        best, best_d = None, 1e9
        for ln in lines:
            cu = latest.get(("pmm", mt, up, ln))
            cd = latest.get(("pmm", mt, down, ln))
            ref = cu if cu is not None else cd
            if ref is None:
                continue
            d = abs(ref - 50)
            if d < best_d:
                best, best_d = (ln, ln, cu, cd), d
    else:
        # Normalize every spread row to the up-side (home) line: a home@L row
        # is L; an away@A row maps to home line -A. Then home@HL pairs with
        # away@(-HL).
        home_lines = set()
        for (src, m, s, ln) in latest:
            if src != "pmm" or m != mt or ln is None:
                continue
            if s == up:
                home_lines.add(ln)
            elif s == down:
                home_lines.add(-ln)
        best, best_d = None, 1e9
        for hl in home_lines:
            cu = latest.get(("pmm", mt, up, hl))        # home @ hl
            cd = latest.get(("pmm", mt, down, -hl))     # away @ -hl (mirror)
            ref = cu if cu is not None else cd
            if ref is None:
                continue
            d = abs(ref - 50)
            if d < best_d:
                best, best_d = (hl, -hl, cu, cd), d      # up_line=hl, down_line=-hl
    if not best:
        return None
    up_ln, dn_ln, cu, cd = best
    if cu is not None and cd is not None and (cu + cd) > 0:
        pu = _devig_two_way(cu / 100.0, cd / 100.0)
    elif cu is not None:
        pu = cu / 100.0
    else:
        pu = 1.0 - cd / 100.0
    return (up_ln, dn_ln), pu, 1.0 - pu


def _attach_exch_current(sb, market_id: str, odds: dict) -> None:
    """Attach `exch_current` (Kalshi/PMM devigged fair) onto each odds
    block. Silent-fail; never breaks the dossier."""
    try:
        latest = _exch_latest(sb, market_id)
        if not latest:
            return
        ml = _exch_ml_fair(latest)
        if ml and odds.get("moneyline"):
            src, ph, pa = ml
            odds["moneyline"]["exch_current"] = {
                "home": _exch_block(ph, None, src),
                "away": _exch_block(pa, None, src),
            }
        for mt, up, down in (("spread", "home", "away"),
                             ("total", "over", "under")):
            r = _exch_two_sided_fair(latest, mt, up, down)
            if r and odds.get(mt):
                (up_ln, dn_ln), pu, pd = r
                # Per-side lines: SPREAD sides are mirrors (home -1.5 / away
                # +1.5), TOTAL sides share the line. Each side gets its OWN.
                odds[mt]["exch_current"] = {
                    up:   _exch_block(pu, up_ln, "pmm"),
                    down: _exch_block(pd, dn_ln, "pmm"),
                }
    except Exception:
        pass


# ─────────── World Cup (3-way 1-X-2) — self-contained soccer engine ───────────
# Soccer is a 3-outcome market (home win / draw / away win). PMM-ONLY: Kalshi
# has no World Cup series, so there's no cross-confirm venue (unlike MLB ML).
# This block is fully additive — it does NOT touch the 2-way build_dossier
# path. The exchange (PMM) devigged cents are the fair anchor (no soccer power
# model yet); the independent read is the user/Claude via Copy-for-Claude.

def _devig_three_way(p_h, p_d, p_a):
    """Normalize three raw implied probs to sum 1.0 (3-way devig)."""
    tot = (p_h or 0) + (p_d or 0) + (p_a or 0)
    if tot <= 0:
        return None
    return p_h / tot, p_d / tot, p_a / tot


def _wc_exch_fair(latest: dict):
    """3-way devigged fair + raw cents from the latest PMM 1-X-2 cents.
    Returns {home,draw,away: {cents, fair_prob, fair_american}} or None.
    A single missing leg is back-filled as the complement of the other two."""
    ch = latest.get(("pmm", "ml", "home", None))
    cd = latest.get(("pmm", "ml", "draw", None))
    ca = latest.get(("pmm", "ml", "away", None))
    if sum(1 for x in (ch, cd, ca) if x is not None) < 2:
        return None
    ph = (ch / 100.0) if ch is not None else None
    pd = (cd / 100.0) if cd is not None else None
    pa = (ca / 100.0) if ca is not None else None
    if None in (ph, pd, pa):
        miss = max(0.01, 1.0 - sum(p for p in (ph, pd, pa) if p is not None))
        if ph is None:   ph = miss
        elif pd is None: pd = miss
        else:            pa = miss
    dv = _devig_three_way(ph, pd, pa)
    if not dv:
        return None
    out = {}
    for side, c, p in (("home", ch, dv[0]), ("draw", cd, dv[1]), ("away", ca, dv[2])):
        p = min(max(p, 0.01), 0.99)
        out[side] = {"cents": (int(c) if c is not None else None),
                     "fair_prob": round(p, 4),
                     "fair_american": _prob_to_american(p)}
    return out


def _wc_sharp(sb, market_id: str, now=None):
    """3-way exchange sharp read from PMM cent movement (last 18h). Sharp
    side = the outcome whose cents ROSE the most (recency-weighted) = money
    flowing in = harder to back. Returns (side, score 1-10, n, confirmed)
    or None.

    Kalshi IS the confirmation venue for soccer too (series KXWCGAME, same
    1-X-2 sides) — mirrors _xsharp_ml: Kalshi disagrees on the picked side's
    direction → no signal (None); Kalshi agrees → confirmed=True; Kalshi
    silent/flat → confirmed=False (PMM-only, caller demotes to a lean).
    Matures as the pm-snapshot-wc clock accrues both feeds' history."""
    now = now or datetime.now(timezone.utc)
    hist = _pm_history(sb, market_id)
    best_side, best_sig, best_n = None, 0.0, 0
    for side in ("home", "draw", "away"):
        snaps = hist.get(("pmm", "ml", side, None), [])
        if len(snaps) < 2:
            continue
        sig = _weighted_signed_delta(snaps, "cents", now=now)
        if sig is not None and sig > best_sig:
            best_side, best_sig, best_n = side, sig, len(snaps)
    if best_side is None or best_sig < _X_MIN_PP:
        return None
    confirmed = False
    ksnaps = hist.get(("kalshi", "ml", best_side, None), [])
    if len(ksnaps) >= 2:
        kw = _weighted_signed_delta(ksnaps, "cents", now=now)
        if kw is not None and abs(kw) >= _X_MIN_PP:
            if kw <= 0:
                return None        # Kalshi moved the picked side DOWN → veto
            confirmed = True       # both feeds agree → confirmed
    score = min(10, round(best_sig * _X_PP_SCALE))
    if score < 1:
        return None
    return best_side, score, best_n, confirmed


def _wc_research(away: str, home: str) -> dict:
    """ESPN soccer research for one fixture: each side's recent form (W-D-L),
    group record, and rank — from the fifa.world scoreboard (the same feed
    _build_worldcup uses), matched by canonical country name in either
    orientation. Late-imports app.py (like _fetch_splits) so the shared ESPN
    soccer reader stays single-source. Silent-fail → {} (never breaks the
    dossier; the deep injury/lineup read is the Copy-for-Claude hand-off)."""
    try:
        import app as _app
        now = datetime.now(timezone.utc)
        dates = f"{(now - timedelta(days=2)):%Y%m%d}-{(now + timedelta(days=10)):%Y%m%d}"
        evs = _app._espn_scoreboard_raw("soccer", "fifa.world", dates=dates) or []
        ak, hk = _app._wc_country_key(away), _app._wc_country_key(home)
    except Exception:
        return {}

    def _ctx(comp, want):
        for c in (comp.get("competitors") or []):
            t = c.get("team") or {}
            nm = t.get("displayName") or t.get("name") or ""
            if _app._wc_country_key(nm) == want:
                recs = c.get("records") or []
                rec = (recs[0] or {}).get("summary") if recs else None
                cr = c.get("curatedRank")
                rank = cr.get("current") if isinstance(cr, dict) else None
                return {"form": c.get("form"), "record": rec, "rank": rank}
        return {}

    for ev in evs:
        comp = (ev.get("competitions") or [{}])[0]
        keys = set()
        for c in (comp.get("competitors") or []):
            t = c.get("team") or {}
            keys.add(_app._wc_country_key(t.get("displayName") or t.get("name") or ""))
        if ak in keys and hk in keys:
            return {"away": _ctx(comp, ak), "home": _ctx(comp, hk)}
    return {}


def build_worldcup_dossier(sb, market_id: str) -> dict:
    """Self-contained 3-way (1-X-2) dossier for one World Cup fixture:
    exchange (PMM) cents → 3-way devig fair (the OUR-number anchor; no soccer
    power model yet) + the cent-movement sharp read + ESPN form/record
    research + a suggestion (follow the exchange steam when it clears the
    sharp gate, else a 1u lean on the favorite) + PMM maker entry per side.
    Copy-for-Claude on the page hands this JSON to chat for the deep read."""
    try:
        m = (sb.table("markets").select("id,sport,event_name,event_start,status")
             .eq("id", market_id).single().execute().data)
    except Exception as e:
        return {"ok": False, "error": f"market lookup: {e}"}
    if not m or m.get("sport") != "WORLDCUP":
        return {"ok": False, "error": "not a World Cup market"}

    away, home = _split_event_name(m.get("event_name") or "")
    latest = _exch_latest(sb, market_id)
    fair = _wc_exch_fair(latest) if latest else None
    sharp = _wc_sharp(sb, market_id)
    research = _wc_research(away, home) if (away and home) else {}

    suggestion = None
    if fair:
        sug_side, score, confirmed = None, 0, True
        if sharp:
            sug_side, score, _n, confirmed = sharp
        # Confirmed by Kalshi → real gated pick. Unconfirmed (PMM-only move,
        # Kalshi silent) → demote to a lean (gate fails), like _xsharp_ml.
        gate = score >= SHARP_SCORE_MIN and confirmed
        if sug_side is None:        # always-give-a-pick: lean the favorite
            sug_side = min(("home", "draw", "away"),
                           key=lambda s: -(fair[s]["fair_prob"] or 0))
        f = fair[sug_side]
        edge_pp = round(score * _X_PP_SCALE, 2) if gate else 0.0
        units, conf, _k = _kelly_units(f["fair_prob"], f["fair_american"], edge_pp, gate)
        # Global sizing policy (June 2026): top tier 3u (5u disabled), and
        # size-up past 1u must be earned by real sharp steam (SHARP_FOR_3U).
        if units > 3:
            units, conf = 3, "medium"
        if units > 1 and score < SHARP_FOR_3U:
            units, conf = 1, "low"
        entry_cents = latest.get(("pmm", "ml", sug_side, None))
        entry_american = (_prob_to_american(entry_cents / 100.0)
                          if entry_cents else f["fair_american"])
        label = {"home": home, "away": away, "draw": "Draw"}[sug_side]
        if gate:
            reasons = [f"Exchange steam toward {label} (sharp {score}/10; "
                       "confirmed by Kalshi)."]
        elif score >= SHARP_SCORE_MIN and not confirmed:
            reasons = [f"PMM steam toward {label} (sharp {score}/10) but Kalshi "
                       "hasn't confirmed — 1u lean until both feeds agree."]
        else:
            reasons = ["No exchange movement signal yet — 1u lean on the favorite; "
                       "use the research read (Copy for Claude) to decide."]
        suggestion = {
            "side": sug_side, "side_label": label, "market_type": "moneyline",
            "fair_prob": f["fair_prob"], "fair_american": f["fair_american"],
            "pmm_bid_american": entry_american,
            "units": units, "confidence": conf,
            "sharp_score": score, "gates_cleared": gate,
            "edge_pp": edge_pp, "reasons": reasons,
        }

    return {
        "ok": True, "market_id": market_id, "sport": "WORLDCUP",
        "event_name": m.get("event_name"), "event_start": m.get("event_start"),
        "away": away, "home": home, "fair": fair,
        "sharp": ({"side": sharp[0], "score": sharp[1], "n": sharp[2],
                   "confirmed": sharp[3]} if sharp else None),
        "research": research, "suggestion": suggestion,
    }


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
    Mariners'; both directions need to work. Normalize diacritics so
    'Montréal Canadiens' matches 'Montreal Canadiens', AND strip punctuation
    so 'St. Louis Cardinals' matches 'St Louis Cardinals' (the period was
    silently breaking every Cardinals game — substring containment failed
    across the '.'). Punctuation→space can't create false MLB matches (no two
    teams differ only by punctuation)."""
    if not (home and away and ev_home and ev_away):
        return False
    import unicodedata
    import re
    def _norm(s: str) -> str:
        # NFKD splits accented chars into base + combining mark (category
        # "Mn") which we drop; then punctuation → space, collapse whitespace.
        s = "".join(c for c in unicodedata.normalize("NFKD", s)
                    if unicodedata.category(c) != "Mn").lower()
        return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", s)).strip()
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
    # Strip accents before matching — The Odds API sends "Montréal
    # Canadiens" (é) while ESPN lists "Montreal Canadiens" (e), so a raw
    # lowercase substring test never matched and the whole ESPN block
    # (records, recent form, injuries) came back null for any accented team.
    import unicodedata
    def _na(s: str) -> str:
        return "".join(c for c in unicodedata.normalize("NFKD", s or "")
                       if unicodedata.category(c) != "Mn").lower()
    away_n, home_n = _na(away), _na(home)
    for g in events:
        comp = (g.get("competitions") or [{}])[0]
        cs = comp.get("competitors") or []
        if len(cs) != 2:
            continue
        h = next((c for c in cs if c.get("homeAway") == "home"), cs[0])
        a = next((c for c in cs if c.get("homeAway") == "away"), cs[1])
        h_name = _na((h.get("team") or {}).get("displayName") or "")
        a_name = _na((a.get("team") or {}).get("displayName") or "")
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


def _normalize_injury_records(records: list) -> list:
    """Shape ESPN injury records (from either the per-team or league-wide
    feed — same field names) into the dossier's flat injury dicts."""
    out = []
    for it in records[:25]:
        if not isinstance(it, dict):
            continue
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


# League-wide ESPN injuries feed. The per-team /teams/{id}/injuries route
# comes back EMPTY for NHL (works for NFL/NBA/MLB) — the league-wide
# endpoint carries the data ESPN omits from the per-team route. One call
# returns every team grouped by id, so we fetch once per sport, cache it,
# and filter to the team that needs it.
_INJURY_CACHE: dict[str, tuple[float, dict]] = {}
_INJURY_CACHE_TTL_SEC = 600


def _espn_league_injuries(sport: str, diag: dict | None = None) -> dict[str, list]:
    pair = _ESPN_PATH.get(sport)
    if not pair:
        return {}
    cached = _INJURY_CACHE.get(sport)
    if cached and (time.time() - cached[0]) < _INJURY_CACHE_TTL_SEC:
        return cached[1]
    grp, lg = pair
    url = f"https://site.api.espn.com/apis/site/v2/sports/{grp}/{lg}/injuries"
    status = None
    data = None
    body_snip = None
    try:
        r = requests.get(url, timeout=HTTP_TIMEOUT,
                         headers={"User-Agent": "Mozilla/5.0"})
        status = r.status_code
        if status == 200:
            data = r.json()
        else:
            body_snip = (r.text or "")[:200]
    except Exception as e:
        log.warning("ESPN league injuries %s failed: %s", sport, e)
        body_snip = f"exc: {str(e)[:160]}"
    out: dict[str, list] = {}
    for grp_obj in ((data or {}).get("injuries") or []):
        if not isinstance(grp_obj, dict):
            continue
        team = grp_obj.get("team") or {}
        tid = str(grp_obj.get("id") or team.get("id") or "")
        recs = grp_obj.get("injuries")
        if tid and isinstance(recs, list):
            out[tid] = _normalize_injury_records(recs)
    if status == 200:  # only cache a successful fetch, so a blip retries
        _INJURY_CACHE[sport] = (time.time(), out)
    if diag is not None:
        diag["league_status"] = status
        diag["league_teams"] = len(out)
        if body_snip:
            diag["league_body"] = body_snip
    return out


def _espn_team_injuries(sport: str, team_id: str | None, diag: dict | None = None) -> list:
    if not team_id:
        return []
    pair = _ESPN_PATH.get(sport)
    if not pair:
        return []
    grp, lg = pair
    url = (f"https://site.web.api.espn.com/apis/site/v2/sports/"
           f"{grp}/{lg}/teams/{team_id}/injuries")
    status = None
    data = None
    body_snip = None
    try:
        r = requests.get(url, timeout=HTTP_TIMEOUT,
                         headers={"User-Agent": "Mozilla/5.0"})
        status = r.status_code
        if status == 200:
            data = r.json()
        else:
            body_snip = (r.text or "")[:200]
    except Exception as e:
        log.warning("ESPN injuries %s %s failed: %s", sport, team_id, e)
        body_snip = f"exc: {str(e)[:160]}"
    if diag is not None:
        diag["status"] = status
        diag["keys"] = list((data or {}).keys())[:8]
        if body_snip:
            diag["body"] = body_snip
    # ESPN's injuries endpoint shape varies by host/sport: sometimes a
    # flat list under "items", sometimes under "injuries", and sometimes
    # grouped per team (each group is {team..., injuries:[...]}). Reading
    # only "items" left the list empty for every sport. Pull whichever
    # key exists and flatten team-grouped shapes down to injury records.
    raw: list = []
    records: list = []
    if data:
        raw = data.get("injuries")
        if raw is None:
            raw = data.get("items") or []
        for el in (raw or []):
            if not isinstance(el, dict):
                continue
            if el.get("athlete"):
                records.append(el)                      # direct injury record
            elif isinstance(el.get("injuries"), list):
                records.extend(x for x in el["injuries"] if isinstance(x, dict))
            else:
                records.append(el)
    out = _normalize_injury_records(records)
    if diag is not None:
        diag["raw_len"] = len(raw or [])
        diag["records"] = len(records)
        diag["source"] = "team" if out else None
    # NHL's per-team route returns nothing — fall back to the league-wide
    # feed (one cached call) and slice out this team's injuries.
    if not out:
        league = _espn_league_injuries(sport, diag=diag)
        out = league.get(str(team_id), [])
        if diag is not None and out:
            diag["source"] = "league"
    return out


def _espn_score_int(raw) -> int | None:
    # ESPN's team-SCHEDULE endpoint returns competitor score as an object
    # ({"value": 5.0, "displayValue": "5"}), not the plain string the
    # scoreboard endpoint gives. Handle dict / str / number uniformly.
    if isinstance(raw, dict):
        raw = raw.get("value", raw.get("displayValue"))
    try:
        return int(float(raw))
    except (ValueError, TypeError):
        return None


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
        me_score = _espn_score_int(me.get("score"))
        opp_score = _espn_score_int(opp.get("score"))
        if me_score is None or opp_score is None:
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
        batters: set = set()
        pitchers: set = set()
        for p in ((teams.get(side) or {}).get("players") or {}).values():
            nm = ((p.get("person") or {}).get("fullName") or "").lower()
            if not nm:
                continue
            pos = ((p.get("position") or {}).get("abbreviation") or "").upper()
            if pos == "P":
                pitchers.add(nm)        # pitchers never bat (DH era) — exclude
            if p.get("battingOrder"):   # only starters carry a batting order
                batters.add(nm)
        if batters:
            out[side] = {"batters": batters, "pitchers": pitchers}
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
    lineup. None when no lineup posted / nobody material is missing.

    Pitchers are excluded: they don't bat in the DH era, so a starter
    who isn't pitching today (normal rotation) must never count as a
    resting hitter — that was docking runs for the whole rotation."""
    lineup = _mlb_posted_lineup(game_pk)
    if not lineup:
        return None
    dock: dict = {}
    notes: dict = {}
    for side, tid in (("home", home_id), ("away", away_id)):
        present = lineup.get(side)
        miss = []
        if present:
            batters = present["batters"]
            pitchers = present["pitchers"]
            for full in _mlb_hitting_leaders(tid, season):
                ln = full.lower()
                in_lineup = any(ln in p or p in ln for p in batters)
                is_pitcher = any(ln in p or p in ln for p in pitchers)
                if not in_lineup and not is_pitcher:
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
LEAN_UNITS       = 0.5  # forced-lean (gate-not-cleared) stake — July 2026,
                        # leans back on the games list as muted half-unit chips
SHARP_SCORE_MIN  = 3   # Sharp signal threshold. Lowered 4→3 May 2026:
                       # at 4 the recency-weighted score left most of the
                       # slate as grey leans (you need fresh steam in the
                       # last ~hour to clear 4). A ~3¢ recent PIN move (or
                       # a bigger older one) is a real-enough signal to bet.
                       # Late-steam recency weighting is untouched — this
                       # only widens what counts as a "real pick" vs lean.
SPLITS_MIN_PP    = 10  # |money% − bets%| considered "material".
SHARP_WEIGHT     = 0.7
SPLITS_WEIGHT    = 0.3
# Full-game O/U auto-suggestions — BENCHED June 2026. The totals engine has no
# independent number (it just follows the exchange total), and lost -10.8u/30d
# through every tweak. Off until the run-total model (scripts/mlb_total_backtest
# → a calibrated handicapper_web projection) clears its backtest + a shadow
# period. Flip True to re-enable. ML/SPR and NRFI are unaffected.
TOTALS_SUGGESTIONS_ENABLED = False  # exchange-follow O/U engine stays OFF — it
                                    # lost -10.8u/30d and the total-side veto that
                                    # would guard it is SKIPPED in test mode (dead).
                                    # Don't flip this on without a working veto +
                                    # a totals engine that beats its backtest.

# TEST tier for O/U (June 2026) — instead of staying fully dark while the
# run-total model proves out, MLB totals run a VISIBLE "test only" tier driven
# by the MODEL (power.proj_total vs the live exchange line), NOT the old
# exchange-follow logic. Phase-1 backtest showed the projection has real signal
# (corr +0.27, 61% directional, beats baseline) but under-projects ~0.7 runs;
# rather than wait, we deploy it at a tiny 0.25u flagged `test_only` so 2 weeks
# of auto-paperlog + user clicks build the real prime window + calibration with
# skin in the game. MLB-only (the model is pitcher-aware + was validated on
# MLB); other sports' totals stay benched. Flip TOTALS_TEST_MODE off to go
# dark again, or set TOTALS_SUGGESTIONS_ENABLED True once it's proven for full
# size. A test total clears its gate only when the model's projection beats the
# line by ≥ TEST_TOTAL_MIN_DIFF runs (projection above line → over, below →
# under). NO external vetoes on this tier — park is already in proj_total via
# _park_factor, so the test measures the model alone (VSiN/park recorded for
# post-hoc slicing, never suppress a pick).
TOTALS_TEST_MODE       = True    # totals run the MODEL-DRIVEN tier (proj_total vs
                                 # line), NOT the discredited exchange-follow
                                 # engine (TOTALS_SUGGESTIONS_ENABLED, -10.8u/30d).
                                 # PROMOTED June 2026 (user call) from the 0.25u
                                 # test to REAL 1u picks after the paperlog showed
                                 # the model's edge scales with conviction: gap
                                 # ≥1.5 runs went 9-5 (+0.94u @0.25u) while the
                                 # marginal 0.5-1.0 gap (the old gate floor) lost.
                                 # So the gate moved 0.5→1.0 run and size 0.25u→1u.
                                 # NO veto here (the model IS the guard — park is
                                 # already inside proj_total via _park_factor; the
                                 # separate _total_conflict_reason can't fire on a
                                 # model-driven side anyway). MLB-only (the model
                                 # is pitcher-aware + MLB-validated); 1u cap (totals
                                 # excluded from SIZE_UP_MARKETS + flat sizing here).
TEST_TOTAL_UNITS       = 1       # promoted to real 1u (was 0.25u test stake)
TEST_TOTAL_MIN_DIFF    = 1.0     # model must beat the line by ≥ this many runs
                                 # (raised 0.5→1.0 — the 0.5-1.0 gap picks lost)
TEST_TOTAL_EDGE_PER_RUN = 2.0    # pp of (display-only) edge per run of gap
# Bias re-center: the Phase-1 backtest (scripts/mlb_total_backtest) measured
# the run-total projection running ~0.73 runs COLD (model mean 8.71 vs actual
# 9.44) — the league RPG anchor is low for the 2026 run environment, and the
# live proj_total shares that anchor family. Add this back before comparing to
# the line so the test tier isn't systematically biased toward unders. RE-TUNE
# from the 2-week test data (compare logged proj vs realized totals), or set 0
# to test the raw projection.
TEST_TOTAL_BIAS_RUNS   = 0.7

# Spread-only price filter — SYMMETRIC band, aligned to Polymarket whole
# cents. A SPR pick is only worth showing when its fair sits in the band
# (-150 ≤ fair ≤ +186, i.e. 60¢ down to 35¢):
#   • fair ≤ -150 (CHALK, > 60¢)    → a leveraged ML at a worse price/risk —
#     drop, let the ML candidate take its place.
#   • fair ≥ +186 (LONGSHOT, < 35¢) → a leveraged-dog runline (e.g. underdog
#     -1.5 at +192/+292) — an equally lame bet with no edge; drop it too.
#     This is the "nothing past the band shows" rule; only the chalk half had
#     ever been coded, so longshot runlines leaked through as forced leans
#     (the Reds -1.5 +292 bug). Doesn't apply to ML (a +155 ML dog is a legit
#     bet the user takes) or TOT (always ~±120). Bounds are whole cents:
#     -150 = 60¢, +186 = 35¢.
SPR_CHALK_FAIR_CAP    = -150
SPR_LONGSHOT_FAIR_CAP =  186

# ── Market-anchored spread model (June 2026 — "use the ML steam in the spread") ──
# The exchange is the sharpest signal for WHO WINS (ML), but spreads ride that
# same direction PLUS a margin dimension the winner-market doesn't price. So a
# spread = market direction + model margin: anchor a joint (home runs, away runs)
# distribution to the exchange ML win prob, let the run-environment model
# (proj_total) supply the magnitude, and read the runline cover prob off it.
# Runs are negative-binomial per team (MLB var/mean ≈ 2.1, overdispersed vs
# Poisson). SHADOW first: computed + logged to pickbot_paperlog + shown on
# Details, but NOT a user-facing card pick — validate ~2 weeks, then promote
# (the totals playbook). MLB-only (the run model is MLB).
SPREAD_MODEL_MODE        = True   # compute the market-anchored spread (shadow)
SPREAD_MODEL_MIN_EDGE_PP = 4.0    # model cover prob must beat the exchange spread
                                  # price by ≥ this (pp) to flag a shadow bet
SPREAD_RUN_VAR_MEAN      = 2.1    # MLB team-runs variance/mean (NB overdispersion)
SPREAD_RUN_NMAX          = 25     # run grid cap per team for the convolution

# When BOTH an ML and a SPR candidate exist on the same side, drop
# the ML if its fair is at or below this cap. The leveraged SPR is
# the cleaner expression of a chalky directional bet at this price.
# ML alone (no SPR alternative) still stands — see the ML/SPR
# exclusion block in `_suggest_picks`.
ML_CHALK_FAIR_CAP = -140

# ─── Total-side veto (the "Rockies under" fix, June 2026) ───
# The TOT side is chosen purely from PIN line movement, so the bot will
# happily follow PIN's total down into Coors and bet an under in the most
# hitter-friendly park in baseball — the power model watches it happen but
# (by design) only nudges SIZING, never the side. Over 118 graded picks
# MLB unders went 13-17 (-0.17u) vs overs 10-6 (+1.86u), and EVERY Rockies
# under lost (4-for-4, -5.84u). These two vetoes let the OUR-NUMBER read
# DEMOTE a total pick to a forced lean (so it stops being a real
# recommendation / one-tap chip) when it disagrees with the side:
#   • Park veto (MLB) — independent of the model, from the static park-
#     factor table. PF ≥ 104 (Coors 112, Fenway/GABP 104) kills unders;
#     PF ≤ 96 (Petco/Oracle/T-Mobile) kills overs.
#   • Model veto — our projected total disagrees by ≥ 1 run, but only when
#     the model feeds sizing for the sport (we trust the number).
_PARK_UNDER_VETO       = 104.0   # park factor ≥ this → don't recommend an under
_PARK_OVER_VETO        = 96.0    # park factor ≤ this → don't recommend an over
_TOTAL_MODEL_VETO_DIFF = 1.0     # runs of model-vs-line disagreement to veto the opposite side

# ─── Timing window (the proven-edge fix, June 2026) ───
# Picks made 1.5-2h before first pitch carried the entire edge (38 picks,
# 68.4%, +27.06u) while last-hour picks were mush (51 picks, 52.9%,
# +3.72u — and totals in that window were outright negative). The recency
# weighting over-trusts last-15-min PIN twitches (retail pile-on, lineup-
# reaction overshoots) as if they were clean sharp steam. We can't easily
# retune that without global blast radius, so instead we CAP sizing on
# last-hour picks: a < 1h-out pick never sizes above 1u no matter what
# Kelly says. Picks keep their `timing_window` tag for the card badge +
# future analysis (it lands in signal_blob when logged).
LATE_WINDOW_MIN = 60                   # < 1h to first pitch → cap units at 1u (last-hour picks underperform)
_PRIME_WINDOW   = (60, 180)            # 1-3h out = the betting window (size-up + glow). Extended 2h->3h June 2026
                                       # on conviction (sharp money moves early — proven 3.5h on BOS); the paperlog
                                       # dataset confirms or kills it after 2 weeks.
_PRIME_CORE     = (90, 120)            # 90-120 = the PROVEN hammer (68.4%, +27u over 38) — tracked via prime_core.
EVAL_WINDOW_MAX = 180                  # page evaluator/chips reach the prime edge (3h). PAPERLOG goes to 5h (app.py)
                                       # for DATA only — not surfaced on the page. (legacy: was 150 'early' test)
                                       # window (capped 1u, NOT prime sizing) — hypothesis: sharp money moves the
                                       # line before the 2h mark, so picking earlier captures CLV. Measured via
                                       # signal_blob.timing_window=='early' + clv_pp before any promotion to sizing.


# Runtime-tunable prime window — now MULTI-ZONE. The weekly auto-tuner
# (scripts/tune_prime_window.py) finds the winning minute-bands in the
# paperlog and writes them as a list of [lo,hi] segments to the
# `pickbot_tuning.zones` column; the dossier reads them here. Live data
# showed the edge is BIMODAL (30-90 and 150-210 win, 90-120 is a losing
# hole between them), which a single contiguous span can't express.
# _PRIME_ZONES is the fallback when the table is empty/unreachable.
_PRIME_ZONES = [(60, 180)]   # fallback (old single-span behaviour)
# Per-bet-type prime zones (June 2026): the markets don't share a hot zone,
# so the tuner writes a separate zone list per market_type to
# `pickbot_tuning.zones_by_market`. A market absent from that map inherits
# the pooled `zones` (the `_pooled` key here). Cache holds both.
_PRIME_WINDOW_CACHE: dict = {"zones": None, "by_market": None, "at": 0.0}
_PRIME_WINDOW_TTL = 300.0   # seconds
_TIMED_MARKETS = ("moneyline", "spread")   # totals benched — see tuner TIMED_MARKETS


def _load_prime_tuning(sb) -> tuple[list[tuple[int, int]], dict]:
    """Load BOTH the pooled prime zones and the per-market map from
    `pickbot_tuning`, cached 5 min. Returns (pooled_zones, by_market) where
    by_market is {market_type: [(lo,hi),…]} (markets without their own
    tuned zones are simply absent → callers fall back to pooled).
    Silent-fail to the _PRIME_ZONES constant."""
    now = time.time()
    c = _PRIME_WINDOW_CACHE
    if c["zones"] is not None and (now - c["at"]) < _PRIME_WINDOW_TTL:
        return c["zones"], c["by_market"]
    zones = _PRIME_ZONES
    by_market: dict = {}
    try:
        rows = (sb.table("pickbot_tuning")
                .select("prime_lo,prime_hi,zones,zones_by_market")
                .eq("id", 1)
                .limit(1)
                .execute().data) or []
        if rows:
            parsed = _parse_zones(rows[0].get("zones"))
            if parsed:
                zones = parsed
            else:
                lo, hi = rows[0].get("prime_lo"), rows[0].get("prime_hi")
                if lo is not None and hi is not None and 0 <= lo < hi:
                    zones = [(int(lo), int(hi))]
            raw_bm = rows[0].get("zones_by_market")
            if isinstance(raw_bm, str):
                try:
                    raw_bm = json.loads(raw_bm)
                except (TypeError, ValueError):
                    raw_bm = None
            if isinstance(raw_bm, dict):
                for mt, segs in raw_bm.items():
                    p = _parse_zones(segs)
                    if p:
                        by_market[mt] = p
    except Exception:
        pass
    c["zones"], c["by_market"], c["at"] = zones, by_market, now
    return zones, by_market


def _load_prime_zones(sb) -> list[tuple[int, int]]:
    """Back-compat: the pooled prime zones (drives the games-list row glow
    as the UNION of all market zones — see _prime_zones_union)."""
    return _load_prime_tuning(sb)[0]


def _load_prime_zones_by_market(sb) -> dict:
    """Per-market prime zones with a `_pooled` fallback key. Passed into
    _suggest_picks so each candidate is timed against ITS market's zones."""
    pooled, by_market = _load_prime_tuning(sb)
    return {**by_market, "_pooled": pooled}


def _market_zones(by_market: dict | None, market_type: str) -> list[tuple[int, int]]:
    """Resolve the prime zones for one market_type from a by-market map,
    falling back to the pooled set then the _PRIME_ZONES constant."""
    if not by_market:
        return _PRIME_ZONES
    return by_market.get(market_type) or by_market.get("_pooled") or _PRIME_ZONES


def _prime_zones_union(sb) -> list[tuple[int, int]]:
    """Merged union of every market's zones (+ pooled) — drives the coarse
    games-list ROW glow, which is per-game (one kickoff time) and so lights
    when the game is prime for ANY bet type. Per-bet-type sizing/badges are
    still resolved precisely server-side via _market_zones."""
    pooled, by_market = _load_prime_tuning(sb)
    segs = list(pooled)
    for mt, z in by_market.items():
        # NRFI and the test O/U tier are TRACKED but their sizing isn't
        # zone-gated yet (flat 0.5u / 0.25u), so they must not light the
        # (size-up implying) row glow. Skip them until their consumption is
        # wired. ML/SPR drive the glow via the pooled zones above.
        if mt in ("nrfi", "total"):
            continue
        segs.extend(z)
    if not segs:
        return _PRIME_ZONES
    segs.sort()
    merged = [list(segs[0])]
    for lo, hi in segs[1:]:
        if lo <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], hi)
        else:
            merged.append([lo, hi])
    return [(lo, hi) for lo, hi in merged]


# Side markets that carry a prime label on the games-list row. NRFI excluded
# (sizing not zone-gated yet → must not imply a size-up window). TOT included —
# model-driven totals are real 1u picks now (June 2026); they have no tuned zones
# of their own yet, so they fall back to the pooled zone for timing/labels.
_PRIME_LABEL_MARKETS = (("moneyline", "ML"), ("spread", "SPR"), ("total", "TOT"))


def _prime_zones_by_market_resolved(sb) -> dict:
    """Per-side-market RESOLVED prime zones for the games-list row badge —
    each side market's OWN zones, falling back to pooled. Keyed by display
    label (ML/SPR/TOT). Lets the row badge name WHICH markets are prime at a
    kickoff once markets specialize; while they share the pooled zone (the
    hold) all three are identical and the frontend collapses to a bare PRIME."""
    pooled, by_market = _load_prime_tuning(sb)
    out = {}
    for mt, label in _PRIME_LABEL_MARKETS:
        out[label] = [list(z) for z in _market_zones({**by_market, "_pooled": pooled}, mt)]
    return out


def _parse_zones(raw) -> list[tuple[int, int]] | None:
    """Coerce the jsonb zones value into a clean, sorted list of (lo,hi)."""
    if not raw:
        return None
    try:
        if isinstance(raw, str):
            raw = json.loads(raw)
        out = []
        for seg in raw:
            lo, hi = int(seg[0]), int(seg[1])
            if 0 <= lo < hi:
                out.append((lo, hi))
        return sorted(out) or None
    except (TypeError, ValueError, KeyError, IndexError):
        return None


def _timing_window(starts_in_min, zones: list[tuple[int, int]] | None = None) -> str | None:
    """Classify a pick by minutes before first pitch against the (tuned or
    fallback) prime ZONES:
      prime  — inside ANY zone: green glow + size-up.
      late   — before the earliest zone: capped to 1u (cold pre-game).
      far    — after the latest zone: capped to 1u.
      gap    — between two zones (e.g. the 90-120 hole): capped to 1u. We
               still bet it if the sharp gate clears, just never size up.
    Only `prime` sizes past 1u. The 90-120 hammer sub-bucket is preserved
    via `prime_core`; raw starts_in_min is logged for finer buckets."""
    if starts_in_min is None:
        return None
    zones = zones or _PRIME_ZONES
    for lo, hi in zones:
        if lo <= starts_in_min <= hi:
            return "prime"
    lo_min = min(z[0] for z in zones)
    hi_max = max(z[1] for z in zones)
    if starts_in_min < lo_min:
        return "late"
    if starts_in_min > hi_max:
        return "far"
    return "gap"


def _is_prime_core(starts_in_min) -> bool | None:
    """True when the pick is in the 90-120 HAMMER sub-bucket of prime.
    Kept distinct from `timing_window` so the next review can still
    compare the 60-90 half against the proven 90-120 core."""
    if starts_in_min is None:
        return None
    return _PRIME_CORE[0] <= starts_in_min <= _PRIME_CORE[1]


# ─────────────── Fight IQ — the UFC model (Phase 3 wiring) ───────────────
# Reads the latest `ufc_model` snapshot (weekly compute in kahla-scanner —
# _lib/ufc_model.py is the engine of record) and prices UFC dossiers:
# winner P (FightElo + layers), the DISTANCE prop, and the rounds ladder
# vs PMM's actual posted lines. REFERENCE-ONLY: never feeds Kelly sizing.
# Its one power is VETO duty (the total-side-veto pattern): a gated UFC ML
# steam pick whose side the model rates <= (1 - UFC_MODEL_VETO_P) demotes
# to a forced lean. Backtest basis: walk-forward 2022+ the model's 70+
# bucket won 81.2%, so a >=65% read against the steam is a real red flag.
# Layer constants are MIRRORED from kahla-scanner/_lib/ufc_model.py — keep
# in sync (Flask can't import the kahla-scanner subproject).
UFC_MODEL_VETO_P    = 0.65   # model >= this on the OPPOSITE side → veto
UFC_MODEL_MIN_N     = 5      # both fighters need >= N rated UFC bouts
UFC_DUR_EDGE_MIN_PP = 4.0    # duration-family shadow-log edge threshold
_UFC_AGE_KNEE, _UFC_AGE_PTS = 32.0, 12.0
_UFC_CHIN_PTS = 25.0
_UFC_LAYOFF_1, _UFC_LAYOFF_2 = 30.0, 60.0
_UFC_REACH_PTS, _UFC_REACH_CAP = 1.5, 9.0
_UFC_STANCE_PTS = 10.0
_UFC_STYLE_PTS, _UFC_STYLE_CAP = 12.0, 36.0
_UFC_SNAP_CACHE: dict = {}
_UFC_SNAP_TTL = 600


def _ufc_snapshot(sb) -> dict | None:
    """Latest ufc_model row (10-min module cache)."""
    now = time.time()
    if _UFC_SNAP_CACHE.get("t", 0) > now - _UFC_SNAP_TTL:
        return _UFC_SNAP_CACHE.get("data")
    data = None
    try:
        rows = (sb.table("ufc_model").select("params,ratings,computed_at")
                .order("computed_at", desc=True).limit(1).execute().data) or []
        data = rows[0] if rows else None
    except Exception as e:
        log.warning("ufc_model snapshot read failed: %s", e)
    _UFC_SNAP_CACHE.update({"t": now, "data": data})
    return data


def _ufc_name_toks(s: str) -> list[str]:
    return [t for t in re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).split()
            if len(t) >= 3]


def _ufc_fighter_lookup(sb, name: str) -> dict | None:
    """ufc_fighters row for a dossier fighter name — exact-insensitive
    first, then unique last-name-token match (the resolver's posture)."""
    toks = _ufc_name_toks(name)
    if not toks:
        return None
    try:
        rows = (sb.table("ufc_fighters")
                .select("id,name,dob,reach_in,stance,td_avg,td_def")
                .ilike("name", f"%{toks[-1]}%").limit(25).execute().data) or []
    except Exception:
        return None
    exact = [r for r in rows
             if _ufc_name_toks(r.get("name") or "") == toks]
    if exact:
        return exact[0]
    last = [r for r in rows
            if toks[-1] in _ufc_name_toks(r.get("name") or "")]
    return last[0] if len(last) == 1 else None


def _ufc_fighter_adj(rating: dict, frow: dict, when) -> tuple[float, list[str]]:
    """Leakage-free live layers for one fighter (Elo points + notes)."""
    pts, notes = 0.0, []
    dob = frow.get("dob")
    if dob and when:
        try:
            age = (when.date() - datetime.fromisoformat(str(dob)[:10]).date()).days / 365.25
            if age > _UFC_AGE_KNEE:
                d = _UFC_AGE_PTS * (age - _UFC_AGE_KNEE)
                pts -= d
                notes.append(f"age {age:.0f} (−{d:.0f})")
        except Exception:
            pass
    ko2 = rating.get("ko2") or 0
    if ko2:
        pts -= _UFC_CHIN_PTS * ko2
        notes.append(f"{ko2} KO loss{'es' if ko2 > 1 else ''} in last 2 (−{_UFC_CHIN_PTS * ko2:.0f})")
    last = rating.get("last")
    if last and when:
        try:
            gap = (when.date() - datetime.fromisoformat(last).date()).days
            if gap >= 730:
                pts -= _UFC_LAYOFF_2
                notes.append(f"{gap // 365}yr layoff (−{_UFC_LAYOFF_2:.0f})")
            elif gap >= 365:
                pts -= _UFC_LAYOFF_1
                notes.append(f"{gap // 30}mo layoff (−{_UFC_LAYOFF_1:.0f})")
        except Exception:
            pass
    return pts, notes


def _ufc_static_pair(fh: dict, fa: dict) -> float:
    """Reach/stance/style points, home-positive. Mirrors the engine."""
    pts = 0.0
    rh, ra = fh.get("reach_in"), fa.get("reach_in")
    if rh is not None and ra is not None:
        pts += max(-_UFC_REACH_CAP, min(_UFC_REACH_CAP,
                                        (float(rh) - float(ra)) * _UFC_REACH_PTS))
    sh = (fh.get("stance") or "").lower()
    sa = (fa.get("stance") or "").lower()
    if sh.startswith("south") and sa.startswith("orth"):
        pts += _UFC_STANCE_PTS
    elif sa.startswith("south") and sh.startswith("orth"):
        pts -= _UFC_STANCE_PTS

    def _n(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None
    th, dh = _n(fh.get("td_avg")), _n(fh.get("td_def"))
    ta, da = _n(fa.get("td_avg")), _n(fa.get("td_def"))
    if None not in (th, dh, ta, da):
        edge = th * (1.0 - da) - ta * (1.0 - dh)
        pts += max(-_UFC_STYLE_CAP, min(_UFC_STYLE_CAP, _UFC_STYLE_PTS * edge))
    return pts


def _ufc_model_block(sb, away: str, home: str, event_start, odds: dict,
                     pmm_data: dict | None) -> dict | None:
    """The Fight IQ dossier block for one bout: winner P + the duration
    family priced vs PMM's actual markets. None when the snapshot or
    either fighter can't be matched (silent — never breaks the dossier)."""
    snap = _ufc_snapshot(sb)
    if not snap:
        return None
    params = snap.get("params") or {}
    ratings = snap.get("ratings") or {}
    scale = params.get("scale") or 300.0
    beta = params.get("beta") or 0.5
    aggs = params.get("aggs") or {}
    fh = _ufc_fighter_lookup(sb, home)
    fa = _ufc_fighter_lookup(sb, away)
    if not fh or not fa:
        return {"matched": False, "reason": "fighter not in UFCStats spine"}
    rh, ra = ratings.get(fh["id"]), ratings.get(fa["id"])
    if not rh or not ra:
        return {"matched": False, "reason": "fighter not rated yet"}
    when = event_start if isinstance(event_start, datetime) else None
    if when is None and event_start:
        try:
            when = datetime.fromisoformat(str(event_start).replace("Z", "+00:00"))
        except Exception:
            when = None
    adj_h, notes_h = _ufc_fighter_adj(rh, fh, when)
    adj_a, notes_a = _ufc_fighter_adj(ra, fa, when)
    d = (rh["elo"] + adj_h) - (ra["elo"] + adj_a) + _ufc_static_pair(fh, fa)
    p_home = 1.0 / (1.0 + 10.0 ** (-d / scale))
    reliable = (rh.get("n", 0) >= UFC_MODEL_MIN_N
                and ra.get("n", 0) >= UFC_MODEL_MIN_N)

    # Duration family: P(finish) from overall rate + fighter finish-rate
    # deviation (weight class unknown at dossier level — documented v1
    # approximation), then distance + any PMM-posted rounds line.
    overall = (aggs.get("overall_finish") or 0.54)
    conds = aggs.get("finish_conds") or {}
    fr = [x for x in (rh.get("fin_rate"), ra.get("fin_rate")) if x is not None]
    p_fin = overall + beta * ((sum(fr) / len(fr) - overall) if fr else 0.0)
    p_fin = max(0.05, min(0.95, p_fin))
    duration: dict = {"p_finish": round(p_fin, 3),
                      "p_distance": round(1.0 - p_fin, 3)}
    # PMM distance market (sniffed by pmm_markets._classify_ufc_distance)
    for dm in ((pmm_data or {}).get("ufc_distance") or []):
        if dm.get("side") != "yes" or not dm.get("quote"):
            continue
        mid = dm["quote"].get("mid")
        if mid:
            duration["distance_pmm"] = {
                "slug": dm.get("slug"), "mid": round(float(mid), 3),
                "edge_pp": round(((1.0 - p_fin) - float(mid)) * 100, 1),
            }
        break
    # PMM rounds line — UFC "totals" on PMM ARE round-count O/U; the
    # dossier's total block carries them per line. Price the over side at
    # any line our timing curve covers (0.5/1.5/2.5; 5-round lines skip).
    tot = (odds.get("total") or {}).get("polymarket") or {}
    over_blk = tot.get("over") or {}
    line = over_blk.get("line")
    cond = conds.get(str(line)) if line is not None else None
    if cond is not None and over_blk.get("quote"):
        mid = over_blk["quote"].get("mid")
        p_over = 1.0 - p_fin * cond
        duration["rounds_pmm"] = {
            "line": line, "p_over": round(p_over, 3),
            "mid": (round(float(mid), 3) if mid else None),
            "edge_pp": (round((p_over - float(mid)) * 100, 1) if mid else None),
        }
    return {
        "matched": True, "reliable": reliable,
        "scale": scale,
        "home": {"name": fh.get("name"), "elo": rh["elo"], "n": rh.get("n"),
                 "adj": round(adj_h, 1), "notes": notes_h},
        "away": {"name": fa.get("name"), "elo": ra["elo"], "n": ra.get("n"),
                 "adj": round(adj_a, 1), "notes": notes_a},
        "p": {"home": round(p_home, 3), "away": round(1.0 - p_home, 3)},
        "fair_american": {"home": _prob_to_american(p_home),
                          "away": _prob_to_american(1.0 - p_home)},
        "duration": duration,
    }


def _total_conflict_reason(sport: str | None, home: str | None,
                           power: dict | None, side: str) -> str | None:
    """Reason the OUR-NUMBER read materially disagrees with a TOTAL side
    that PIN movement picked — else None. Used to veto an under/over down
    to a forced lean. See the constants above for the rationale."""
    if side not in ("over", "under"):
        return None
    # Park veto (MLB) — independent of the model, so it fires even on a v1
    # season-stat fallback or an unmatched team. This is the Rockies guard.
    if sport == "MLB" and home:
        pf = _park_factor(home)
        mascot = home.split()[-1] if home.split() else home
        if side == "under" and pf >= _PARK_UNDER_VETO:
            return f"{mascot} park factor {round(pf)} (hitter-friendly) — fading the under"
        if side == "over" and pf <= _PARK_OVER_VETO:
            return f"{mascot} park factor {round(pf)} (pitcher-friendly) — fading the over"
    # Model-lean veto — only when the model feeds sizing for this sport
    # (i.e. it cleared the backtest gate and we trust the projected total).
    if power and power.get("feeds_sizing"):
        lean = power.get("total_lean")
        diff = _to_float(power.get("total_diff"))
        if lean and lean != side and diff is not None and abs(diff) >= _TOTAL_MODEL_VETO_DIFF:
            pt = power.get("proj_total")
            return f"model projects {pt} ({lean} by {abs(diff):.1f}) — disagrees with the {side}"
    return None


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
# with calibrated numbers. Sizing snaps to the existing 1/3/5u tiers.
#
# 1u IS THE DEFAULT BET (rev. May 2026). Standard bankroll discipline:
# ~90% of plays are 1u, 3u/5u are the rare strong-conviction spots. So a
# pick that CLEARS the sharp gate is still a 1u low bet UNLESS the
# ¼-Kelly stake is genuinely large — only then does it step up to 3u
# (≥ KELLY_MED_PCT) or 5u (≥ KELLY_HIGH_PCT). A pick that does NOT clear
# the gate is also a 1u low (forced lean). The difference between a real
# 1u pick and a 1u lean is `gates_cleared` (the card heading + button
# color key off it), NOT the unit size. Before this rev every gated pick
# defaulted to 3u, so the bot never produced a 1u recommendation — wrong
# for unit-based betting where 1u is the workhorse.
KELLY_FRACTION       = 0.25     # quarter-Kelly — survives variance
EDGE_PER_SHARP_POINT = 0.40     # pp of edge per point of sharp_score (≤4pp at 10)
# ZEROED July 2026 — Circa money-vs-tickets divergence backtested at NO edge
# vs the exchange close on ML (−0.8 wins above close /96 at ≥8pp; +0.1/74 at
# ≥15pp) and negative on spreads (−3.5/40), so it no longer buys Kelly stake.
# splits_pp is still computed + recorded on every pick; restore a coefficient
# only if forward per-pick data contradicts the backtest. (SPLITS_WEIGHT in
# combined_score is untouched — it only tiebreaks side selection.)
EDGE_PER_SPLITS_PP   = 0.0      # pp of edge per aligned money−bets pp (was 0.10)
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
# MLB REMOVED July 2026 — the live verdict came in: on 92 settled real ML
# picks with a model read, model-DISAGREE picks (+24.5u, CLV 0.94) BEAT
# model-agree (+13.6u, CLV 0.11). The pitcher-aware layer did not rescue the
# noisy MLB team core for SIDES, so the confirmation nudge was sizing up the
# WORSE picks. The MLB model still drives TOTALS (proj_total gate: +11.2u/81)
# and the total-side veto — those paths don't read this set.
# FOOTBALL ADDED July 2026 — walk-forward on the full 2025 season
# (preseason excluded, warmup 60): NFL 66.5% vs 52.2% home baseline
# (Brier 0.2171, n=203; 70+ bucket won 77.0%), NCAAF 71.2% vs 57.2%
# (Brier 0.1935, n=517; 70+ bucket 80.4%) — both clear the same bar NBA
# earned in on. Live CLV (signal_blob.model.agree) audits them in-season;
# demote like MLB if model-agree underperforms.
MODEL_SIZING_SPORTS  = {"NBA", "NFL", "NCAAF"}
MODEL_EDGE_WEIGHT    = 0.25     # fraction of (model_prob − PIN_fair) credited
MODEL_EDGE_CAP_PP    = 1.5      # cap on the nudge — widen as CLV proves the model
EDGE_CAP_PP          = 6.0      # hard cap so a crude input can't blow up sizing
# Two ¼-Kelly stake thresholds gate the step-up off the default 1u. Set
# so the BULK of gated picks stay 1u: with edge_pp dominated by
# sharp_score·0.40, a typical sharp-3-to-6 read yields a ¼-Kelly stake
# of ~0.6-1.6% → 1u. 3u needs ~4pp of edge (sharp ≈7, or sharp ≈5 with
# aligned splits/model); 5u needs a near-cap multi-signal read. Raised
# from the old single 2.5% (which made 3u the floor) to make 3u/5u the
# exception, not the rule.
KELLY_MED_PCT        = 2.0      # ¼-Kelly stake ≥ this %BR → 3u medium
KELLY_HIGH_PCT       = 3.0      # ¼-Kelly stake ≥ this %BR → 5u high

# Size-up is MONEYLINE-ONLY (June 2026 — from a 365-pick live review).
# Sized-up (3u/5u) picks by market told a one-sided story:
#   ML  19-12 (61%)  +27.4u   ← the size-up signal is real here
#   TOT  9-11 (45%)   -7.2u   ← losing in BOTH eras
#   SPR  0-10 ( 0%)  -12.0u   ← never won a sized-up spread, ever
# The Kelly stake is predictive for moneyline but pure variance on
# spreads (a leveraged restatement of the ML bet — same edge, more
# variance) and totals (chosen off line movement alone; the total-side
# veto already flags the leaks). Same failure mode that killed the whale
# tier: a signal that's real for one market applied where it isn't. So
# only ML earns 3u/5u; SPR + TOT cap at 1u. Bankroll defense, NOT a model
# change — add markets back to SIZE_UP_MARKETS to re-enable, and re-check
# the per-market sized-up PnL before doing so.
SIZE_UP_MARKETS      = {"moneyline"}

# Size-up must be EARNED BY SHARP STEAM (June 2026 — 365-pick review).
# The bug: edge_pp drives Kelly sizing, but edge_pp pegs at its 6pp cap
# from VSiN splits + the crude power model even when sharp_score is weak
# — so sharp-light picks got sized to 5u (the Pirates 5u rode sharp=2.0,
# edge pegged at 6.0, and lost -5.15u). Sized-up (3u+) PnL by sharp_score:
#   sharp ≤3   4-5   -7.5u   (pure bleed — the sticky/decayed picks live here)
#   sharp 4-5  13-14 +1.6u   (breakeven, +1.9 CLV — real but thin)
#   sharp 6+   11-9  +14.1u  (the actual edge, +2.5 CLV)
# So a pick can't size past 1u without genuine sharp: 3u needs sharp ≥
# SHARP_FOR_3U, 5u needs sharp ≥ SHARP_FOR_5U; otherwise it caps at 1u no
# matter what edge_pp/Kelly say. This also defuses the sticky-gate path —
# a sticky pick survives on a DECAYED score (≥ STICKY_GATE_EXIT=1.5, which
# is < SHARP_FOR_3U), so hysteresis keeps the chip stable for the UI but
# can never justify sizing up. Same whale-tier lesson: size on sharp
# steam, never on splits/model alone. Caps DOWN only.
SHARP_FOR_3U         = 4.0      # sharp_score floor to allow a 3u pick
SHARP_FOR_5U         = 6.0      # (unused — 5u tier disabled June 2026; top tier is 3u)

# Sticky gate (June 2026 — fixes the "pick exists for 5 minutes" symptom).
# After the June recency-weight trim, a genuine 5¢ PIN steam scores ~3.75
# only while <15min old (5×0.75), then decays to ~2.5 (5×0.50) — below the
# gate — so real picks flickered onto the page and vanished. Hysteresis:
# enter at SHARP_SCORE_MIN (3), then a (market, side) that already cleared
# today (pickbot_paperlog memory) STAYS a real pick while its score holds
# ≥ STICKY_GATE_EXIT. The total-side veto still overrides. Sticky picks
# re-size through Kelly (typically 1u) and carry sticky=True for review.
STICKY_GATE_EXIT  = 1.5   # exit bar — below this even a sticky pick demotes
STICKY_LOOKBACK_H = 12    # how far back the paperlog memory reaches


def _sticky_keys(sb, market_id: str) -> set:
    """(market_type, side) pairs that cleared the gate for this game within
    the lookback — read from pickbot_paperlog (gate-cleared rows only).
    Silent-fail → empty set (no hysteresis, never a broken dossier)."""
    if sb is None or not market_id:
        return set()
    try:
        since = (datetime.now(timezone.utc)
                 - timedelta(hours=STICKY_LOOKBACK_H)).isoformat()
        rows = (sb.table("pickbot_paperlog")
                .select("market_type,side")
                .eq("market_id", market_id)
                .eq("gates_cleared", True)
                .gte("logged_at", since)
                .limit(100).execute().data) or []
    except Exception:
        return set()
    return {(r.get("market_type"), r.get("side")) for r in rows
            if r.get("market_type") and r.get("side")}


def _kelly_units(fair_prob, fair_american, edge_pp,
                 gates_cleared, kelly_fraction: float | None = None) -> tuple[int, str, float]:
    """(units, confidence, kelly_pct). Kelly stake from the signal-derived
    edge, snapped to the 1/3/5u tiers.

    The Kelly FRACTION (aggression) is runtime-tunable — `kelly_fraction`
    overrides the KELLY_FRACTION constant when the sizing auto-tuner has
    proven (on new-scale CLV) that higher-edge picks earn more, so it can
    push from quarter-Kelly toward third-Kelly (more 3u/5u) — or back off.

    1u is the DEFAULT — both for forced leans (gate not cleared) AND for
    real picks whose Kelly stake is ordinary. A real pick only steps up
    to 3u (≥ KELLY_MED_PCT) or 5u (≥ KELLY_HIGH_PCT) on a genuinely large
    stake. Real-pick-vs-lean is conveyed by `gates_cleared`, not units."""
    frac = kelly_fraction if kelly_fraction is not None else KELLY_FRACTION
    if not gates_cleared:
        return 1, "low", 0.0
    # Gated pick but no usable edge estimate → standard 1u bet.
    if fair_prob is None or fair_american is None or not edge_pp or edge_pp <= 0:
        return 1, "low", 0.0
    true_p = min(max(fair_prob + edge_pp / 100.0, 0.01), 0.99)
    try:
        dec = _american_to_decimal(int(fair_american))
    except (TypeError, ValueError):
        return 1, "low", 0.0
    b = dec - 1.0
    if b <= 0:
        return 1, "low", 0.0
    f = true_p - (1.0 - true_p) / b      # full Kelly fraction
    kelly_pct = max(0.0, f) * frac * 100.0
    if kelly_pct >= KELLY_HIGH_PCT:
        return 5, "high", round(kelly_pct, 2)
    if kelly_pct >= KELLY_MED_PCT:
        return 3, "medium", round(kelly_pct, 2)
    return 1, "low", round(kelly_pct, 2)


# Runtime-tunable Kelly fraction (aggression dial), written by the weekly
# sizing auto-tuner to `pickbot_tuning.kelly_fraction`. NULL → use the
# KELLY_FRACTION constant (dormant). Bounded MIN/MAX in the tuner.
_KELLY_FRAC_CACHE: dict = {"frac": None, "at": 0.0}


def _load_kelly_fraction(sb) -> float:
    """Tuned Kelly fraction from `pickbot_tuning`, cached 5 min. Falls back
    to the KELLY_FRACTION constant when unset. Silent-fail."""
    now = time.time()
    c = _KELLY_FRAC_CACHE
    if c["frac"] is not None and (now - c["at"]) < _PRIME_WINDOW_TTL:
        return c["frac"]
    frac = KELLY_FRACTION
    try:
        rows = (sb.table("pickbot_tuning")
                .select("kelly_fraction").eq("id", 1).limit(1)
                .execute().data) or []
        if rows and rows[0].get("kelly_fraction") is not None:
            v = float(rows[0]["kelly_fraction"])
            if 0.05 <= v <= 0.6:
                frac = v
    except Exception:
        pass
    c["frac"], c["at"] = frac, now
    return frac


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


# ─────────────────────── NRFI / YRFI first-inning model ───────────────────────
#
# The Pick Bot's first DERIVATIVE market. NRFI = No Run First Inning
# (neither team scores in the 1st); YRFI = a run scores top or bottom of
# the 1st. MLB only. Web-only (like the power model) — the CLI dossier
# has no projection logic, so this isn't mirrored into
# kahla-scanner/scripts/handicapper.py.
#
# THE CORE INSIGHT (why this isn't a scaled game total):
#   1. The first inning ALWAYS faces the top of the order (hitters 1-2-3).
#      A team can be league-average overall but run a monster 1-2-3 — so
#      the offense input is the top-of-order on-base, NOT team RPG.
#   2. It's TWO independent half-innings vs two different pitchers, so:
#         P(NRFI) = q_top · q_bot
#      top half:    AWAY top-of-order bats vs HOME starting pitcher
#      bottom half: HOME top-of-order bats vs AWAY starting pitcher
#
# Each q (scoreless-half prob) maps an "expected first-inning runs" index
# (xr) through a CALIBRATED LOGISTIC — deliberately NOT raw Poisson.
# Half-inning run-scoring is zero-inflated/bursty (lots of 1-2-3 innings
# then crooked numbers), so Poisson(λ≈0.53) overpredicts scoring (gives
# YRFI ≈ 63% vs the observed ~47-50%). We anchor the logistic so
# league-average inputs land at NRFI_Q_BASE (≈0.713 → NRFI ≈ 0.508),
# recentered on the observed 2026 base rate (see the constant); the slope
# NRFI_Q_SLOPE is a provisional guess the backtest (nrfi_backtest.py) tunes.
#
#   xr = NRFI_XR0 · off_ratio · pitch_ratio · park_ratio · wx_ratio
#     off_ratio   = top_order_obp / NRFI_LG_OBP      (top-of-order on-base)
#     pitch_ratio = opposing_sp_ra9 / NRFI_LG_RA9    (the starter)
#     park_ratio  = park_factor / 100
#     wx_ratio    = small temp-only nudge (wind needs park orientation we
#                   don't encode → surfaced as reference, not sized; same
#                   stance as the power model's weather layer)
#
# OUR-NUMBER-ONLY: there's no NRFI line in our odds feed, so the card
# shows the model's fair %/American + a lean RELATIVE to the league
# baseline (our number materially off ~50/50 is where the shopping value
# is). The user shops a book/Polymarket price beating our fair.
NRFI_XR0       = 0.53     # league-avg expected runs in a first half-inning
NRFI_LG_OBP    = 0.315    # league-average on-base percentage
NRFI_LG_RA9    = 4.30     # league-average runs allowed / 9 (matches _starter_runs scale)
NRFI_Q_BASE    = 0.713    # scoreless-half prob at league-avg inputs (→ NRFI ≈ 0.508).
                          # Recentered May 2026: a 796-game 2026 backtest put the
                          # observed NRFI base rate at 48.6% (offense up vs the
                          # historical ~52.5%) and the model mean at 50.3% — biased
                          # +1.7pp high at 0.725. 0.713 drops model mean onto ~48.6%.
                          # The market prices this season's environment, so we match it.
NRFI_Q_SLOPE   = 2.0      # logistic sensitivity to xr — backtest-calibrated
NRFI_TOP_BOOST = 1.10     # top-of-order OBP ≈ 10% above team OBP (fallback only)
NRFI_LEAN_PP   = 4.0      # |our NRFI% − baseline NRFI%| ≥ this → model lean (context only)
NRFI_EDGE_MIN_PP = 3.0    # our fair − PMM maker entry ≥ this (pp) → light the BET button
# EDGE CLAMP (July 2026, 247 graded paperlog NRFI rows): the edge is REAL only
# in the 3-6pp band — YRFI@3-6pp went +21.5u/151 while 6pp+ was flat on both
# sides (yes +1.5/40, no −1.2/17). A claimed edge past 6pp means the model
# disagrees with the market too much — and at that distance the MARKET is
# right (same inverse pattern as the whale tier). Past the max → no bet.
NRFI_EDGE_MAX_PP = 6.0


def _nrfi_half_scoreless(xr: float) -> float:
    """Map expected first-half-inning runs (xr) to P(scoreless half).
    Calibrated logistic anchored so xr == NRFI_XR0 → NRFI_Q_BASE."""
    k0 = math.log(NRFI_Q_BASE / (1.0 - NRFI_Q_BASE)) + NRFI_Q_SLOPE * NRFI_XR0
    z = k0 - NRFI_Q_SLOPE * max(0.0, xr)
    # Clamp to keep a single scoreless prob sane (0.40–0.92 covers the
    # realistic per-half range; the product still spans ~0.16–0.85 NRFI).
    return min(0.92, max(0.40, 1.0 / (1.0 + math.exp(-z))))


def _nrfi_team_obp(team_id, season: int) -> float | None:
    """Team season OBP from MLB Stats API. None on any failure."""
    if not team_id:
        return None
    data = _http_get(f"https://statsapi.mlb.com/api/v1/teams/{team_id}/stats",
                     params={"stats": "season", "group": "hitting", "season": season})
    if not data:
        return None
    try:
        sp = ((data.get("stats") or [{}])[0].get("splits") or [{}])
        s = (sp[0].get("stat") or {}) if sp else {}
        return _to_float(s.get("obp"))
    except Exception:
        return None


def _nrfi_lineup_top_obp(box: dict | None, side: str) -> float | None:
    """Average season OBP of the posted top-3 batting-order spots for one
    side, read from the boxscore's per-player seasonStats. None when the
    lineup isn't posted yet (~3-4h pre-game) or OBP is missing — caller
    falls back to team OBP × NRFI_TOP_BOOST.

    battingOrder is a string: '100'/'200'/'300' = the 1/2/3 hole starters
    ('101' etc. = subs in that slot, ignored)."""
    if not box:
        return None
    players = ((box.get("teams") or {}).get(side) or {}).get("players") or {}
    top: dict[int, float] = {}
    for p in players.values():
        bo = p.get("battingOrder")
        if not bo:
            continue
        try:
            slot = int(bo) // 100
        except (TypeError, ValueError):
            continue
        if slot not in (1, 2, 3) or int(bo) % 100 != 0:
            continue  # only the starting 1/2/3 hitters
        obp = _to_float((((p.get("seasonStats") or {}).get("batting") or {}).get("obp")))
        if obp is not None:
            top[slot] = obp
    vals = list(top.values())
    if not vals:
        return None
    return sum(vals) / len(vals)


def _nrfi_model(sport: str, pitchers: dict | None, away: str | None,
                home: str | None, game_pk, weather: dict | None,
                pmm_nrfi: list | None = None) -> dict | None:
    """First-inning NRFI/YRFI model. MLB only; None otherwise or when we
    can't build a usable input set. Silent-fail throughout — never raises
    into the dossier. See the section header for the math + rationale.

    `pmm_nrfi` is the Polymarket first-inning market entries (from
    pmm_markets.lookup) when PMM lists one. We compute the model fair,
    then — if PMM has a price — the EDGE vs PMM's maker entry (bid) per
    side and flag the +EV side as the bet. No PMM market → model fair +
    lean only, no bet flag (the determination NEEDS a price to shop)."""
    if sport != "MLB" or not (away and home):
        return None
    season = datetime.now(timezone.utc).year
    away_p = (pitchers or {}).get("away") or None
    home_p = (pitchers or {}).get("home") or None

    # Opposing starter run-prevention (RA9 talent blend). Missing/​unannounced
    # starter falls back to the league average so we still produce a number.
    home_sp = _starter_runs(home_p, NRFI_LG_RA9)
    away_sp = _starter_runs(away_p, NRFI_LG_RA9)
    home_sp_used = home_sp if home_sp is not None else NRFI_LG_RA9
    away_sp_used = away_sp if away_sp is not None else NRFI_LG_RA9

    # Top-of-order on-base. Prefer the posted lineup's 1/2/3 hitters
    # (the structural edge); fall back to team OBP × boost before the
    # lineup posts. If we can't get OBP at all, bail — without an offense
    # input the model is just a pitcher number and not worth showing.
    box = (_http_get(f"https://statsapi.mlb.com/api/v1/game/{game_pk}/boxscore")
           if game_pk else None)
    away_id = (pitchers or {}).get("away_team_id")
    home_id = (pitchers or {}).get("home_team_id")

    def _obp_for(side: str, team_id) -> tuple[float | None, str]:
        lo = _nrfi_lineup_top_obp(box, side)
        if lo is not None:
            return lo, "lineup"
        team = _nrfi_team_obp(team_id, season)
        if team is not None:
            return team * NRFI_TOP_BOOST, "team"
        return None, "none"

    away_obp, away_src = _obp_for("away", away_id)
    home_obp, home_src = _obp_for("home", home_id)
    if away_obp is None or home_obp is None:
        return None

    park = _park_factor(home) / 100.0

    # Weather: temp-only nudge (warm air carries; cold suppresses). Wind
    # needs park orientation we don't encode, so it's reference-only —
    # consistent with the power model's stance. Domes / no forecast → 1.0.
    wx = 1.0
    temp_f = None
    if weather and not weather.get("dome"):
        temp_f = _to_float(weather.get("temp_f"))
        if temp_f is not None:
            wx = min(1.10, max(0.94, 1.0 + (temp_f - 70.0) * 0.002))

    # Expected first-half-inning runs per side.
    xr_top = NRFI_XR0 * (away_obp / NRFI_LG_OBP) * (home_sp_used / NRFI_LG_RA9) * park * wx
    xr_bot = NRFI_XR0 * (home_obp / NRFI_LG_OBP) * (away_sp_used / NRFI_LG_RA9) * park * wx
    q_top = _nrfi_half_scoreless(xr_top)   # away scoreless in top 1st
    q_bot = _nrfi_half_scoreless(xr_bot)   # home scoreless in bottom 1st
    p_nrfi = q_top * q_bot
    p_yrfi = 1.0 - p_nrfi

    baseline_nrfi = NRFI_Q_BASE * NRFI_Q_BASE
    diff_pp = (p_nrfi - baseline_nrfi) * 100.0
    if diff_pp >= NRFI_LEAN_PP:
        lean = "no"          # model NRFI well above baseline → NRFI value
    elif diff_pp <= -NRFI_LEAN_PP:
        lean = "yes"         # model YRFI well above baseline → YRFI value
    else:
        lean = None

    # ── Polymarket edge + bet determination ──
    # Maker-only entry: we rest a limit on the PMM BID for the side we
    # want and fill there. So edge = our_fair_prob − bid_prob for the
    # side. The +EV side with the largest edge ≥ NRFI_EDGE_MIN_PP is THE
    # bet (button lights up); below threshold (or no PMM market) → pass.
    our_prob = {"no": p_nrfi, "yes": p_yrfi}
    pmm_block: dict = {}
    for e in (pmm_nrfi or []):
        side = e.get("side")
        q = e.get("quote") or {}
        if side in ("no", "yes") and (q.get("bid") is not None or q.get("ask") is not None):
            # Keep the better (higher) bid if PMM somehow returns two.
            prev = pmm_block.get(side)
            if prev is None or (q.get("bid") or 0) > (prev.get("bid") or 0):
                pmm_block[side] = {
                    "bid": q.get("bid"), "ask": q.get("ask"), "mid": q.get("mid"),
                    "bid_american": q.get("bid_american"),
                    "ask_american": q.get("ask_american"),
                    "mid_american": q.get("mid_american"),
                    "slug": e.get("slug"),
                }
    bet_side = None
    bet_edge_pp = None
    entry_price = None
    for side in ("no", "yes"):
        blk = pmm_block.get(side)
        if not blk or blk.get("bid") is None:
            continue
        edge = (our_prob[side] - blk["bid"]) * 100.0   # pp, vs maker entry
        blk["edge_pp"] = round(edge, 1)
        if (NRFI_EDGE_MIN_PP <= edge <= NRFI_EDGE_MAX_PP
                and (bet_edge_pp is None or edge > bet_edge_pp)):
            bet_side = side
            bet_edge_pp = round(edge, 1)
            entry_price = blk.get("bid_american")
    pmm_matched = bool(pmm_block)
    # Gate on the PMM edge — the user's call: NRFI only "lights up" when
    # there's a real price to beat, not on the model-vs-baseline lean.
    gates_cleared = bet_side is not None

    # Plain-English reasons (mirror the suggestion-card bullet style).
    reasons: list[str] = []
    _amer = lambda a: ("+" + str(a)) if (a is not None and a > 0) else (str(a) if a is not None else "?")
    if pmm_matched and bet_side:
        side_lbl = "NRFI" if bet_side == "no" else "YRFI"
        reasons.append(
            f"BET {side_lbl} — model fair "
            f"{round(our_prob[bet_side]*100)}% vs Polymarket bid "
            f"{_amer(pmm_block[bet_side].get('bid_american'))} "
            f"→ +{bet_edge_pp}pp edge (rest a maker limit at the bid)")
    elif pmm_matched:
        _edges = [b.get("edge_pp") for b in pmm_block.values()
                  if b.get("edge_pp") is not None]
        if _edges and max(_edges) > NRFI_EDGE_MAX_PP:
            reasons.append(
                f"Model claims +{max(_edges)}pp vs Polymarket — past the "
                f"{NRFI_EDGE_MAX_PP}pp trust clamp (edges that large graded "
                "flat: the market is right, not the model). Pass.")
        else:
            reasons.append(
                "No edge vs Polymarket — model fair within "
                f"{NRFI_EDGE_MIN_PP}pp of PMM's price on both sides. Pass.")
    else:
        reasons.append(
            "No Polymarket 1st-inning market found — can't price an edge. "
            "Model fair shown for reference; shop a book price beating it.")
    sp_bits = []
    if home_p and home_p.get("name"):
        sp_bits.append(f"{home_p['name']} {home_sp_used:.2f} RA9")
    if away_p and away_p.get("name"):
        sp_bits.append(f"{away_p['name']} {away_sp_used:.2f} RA9")
    if sp_bits:
        reasons.append("Starters (1st-inning run prevention): " + " · ".join(sp_bits))
    reasons.append(
        f"Top-of-order OBP — {away}: {away_obp:.3f} ({away_src}), "
        f"{home}: {home_obp:.3f} ({home_src})")
    if abs(park - 1.0) >= 0.02:
        reasons.append(f"Park run factor {round(park*100)} ({'hitter' if park > 1 else 'pitcher'}-friendly)")
    if temp_f is not None and abs(wx - 1.0) >= 0.01:
        reasons.append(f"Temp {round(temp_f)}°F ({'warm — runs up' if wx > 1 else 'cold — runs down'})")
    reasons.append(
        f"Model NRFI {round(p_nrfi*100)}% vs ~{round(baseline_nrfi*100)}% league baseline → "
        + ("lean NRFI" if lean == "no" else "lean YRFI" if lean == "yes"
           else "no edge vs baseline"))

    return {
        "p_nrfi":              round(p_nrfi, 4),
        "p_yrfi":              round(p_yrfi, 4),
        "nrfi_fair_american":  _prob_to_american(p_nrfi),
        "yrfi_fair_american":  _prob_to_american(p_yrfi),
        "q_top":               round(q_top, 4),
        "q_bot":               round(q_bot, 4),
        "xr_top":              round(xr_top, 3),
        "xr_bot":              round(xr_bot, 3),
        "lean":                lean,             # model vs baseline: 'no'/'yes'/None (context)
        "diff_vs_baseline_pp": round(diff_pp, 1),
        "baseline_nrfi":       round(baseline_nrfi, 4),
        # Polymarket-driven bet determination (the actionable signal).
        "polymarket":          pmm_block or None,   # {no:{...}, yes:{...}} with edge_pp per side
        "pmm_matched":         pmm_matched,
        "bet_side":            bet_side,            # 'no'(NRFI)/'yes'(YRFI)/None
        "bet_edge_pp":         bet_edge_pp,
        "entry_price":         entry_price,         # PMM bid American for the bet side (maker entry)
        "gates_cleared":       gates_cleared,       # true only when a +EV PMM side exists
        "inputs": {
            "away_obp": round(away_obp, 3), "home_obp": round(home_obp, 3),
            "away_obp_src": away_src, "home_obp_src": home_src,
            "home_sp_ra9": round(home_sp_used, 2), "away_sp_ra9": round(away_sp_used, 2),
            "home_sp_known": home_sp is not None, "away_sp_known": away_sp is not None,
            "park_factor": round(park * 100), "temp_f": temp_f, "wx_mult": round(wx, 3),
        },
        "reasons":             reasons,
    }


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


# ───────────── VSiN sharp-money splits (Circa handle vs blended tickets) ────
# The axis is HANDLE (money) vs BETS (tickets), NOT Circa-vs-DK. Circa doesn't
# limit bettors, so real sharp money lands there → CIRCA HANDLE is the sharp
# signal. DK limits everyone (sharps can't get down), so DK handle is NOT sharp
# — we use DK only for its BET% (a bigger public-ticket sample). Public read =
# BLENDED (Circa+DK) bet%. Signal per side = circa_handle% − blended_bet%:
# big positive = money beyond the tickets (sharp) → follow; big negative =
# tickets with no money behind them (square) → fade. NO Circa handle ⇒ no
# sharp read at all (we never substitute DK handle — chasing DK "smart" money
# is nonsense). Both books' raw handle+bets are still captured for validation.
_VSIN_MT = {"moneyline": "ml", "spread": "spread", "total": "total"}
_VSIN_OPP_HANDLE_MIN = 60   # opposite side holds ≥ this % of the (Circa) money
_VSIN_SHARP_GAP_MIN  = 8    # Circa money exceeds blended tickets by ≥ this (sharp concentration, not chalk)
_VSIN_OPP = {"away": "home", "home": "away", "over": "under", "under": "over"}


def _vsin_for_game(sport: str, away: str, home: str) -> dict:
    """Match this game to VSiN's slate for BOTH books. Late-imports app.py's
    scraper (cached 15 min there). Returns {matched, books:{circa:ev,...},
    slate:{circa:[...],...}} — `slate` is the full list of games VSiN returned
    per book (for the no-match diagnostic: lets us see WHAT VSiN had so a miss
    self-explains as a name mismatch vs VSiN not carrying the game)."""
    out = {"matched": False, "books": {}, "slate": {}}
    try:
        from app import _fetch_vsin_splits as _vf  # late import (circular-safe)
    except Exception:
        return out
    for book in ("circa", "draftkings"):
        try:
            res = _vf(sport, book) or {}
        except Exception:
            continue
        evs = res.get("events") or []
        out["slate"][book] = [f"{e.get('away_team','?')} @ {e.get('home_team','?')}"
                              for e in evs]
        for e in evs:
            if _team_match(home, away, e.get("home_team", ""), e.get("away_team", "")):
                out["books"][book] = e
                out["matched"] = True
                break
    return out


def _vsin_cell(vsin: dict | None, book: str, vmt: str, side: str, field: str):
    """One value (handle|bets) for a book/market/side, or None."""
    ev = ((vsin or {}).get("books") or {}).get(book)
    if not ev:
        return None
    blk = ev.get(vmt) or {}
    key = side if vmt != "total" else ("over" if side == "over" else "under")
    return blk.get(f"{key}_{field}")


def _vsin_circa_handle(vsin, vmt, side):
    """Circa handle% (the SHARP money) for a market/side. Circa only — DK
    handle is never sharp (limited book), so it's never substituted."""
    return _vsin_cell(vsin, "circa", vmt, side, "handle")


def _vsin_blended_bets(vsin, vmt, side):
    """Public ticket% = mean of Circa + DK bet% for a market/side (whichever
    books report it). None if neither does."""
    vals = [v for v in (_vsin_cell(vsin, "circa", vmt, side, "bets"),
                        _vsin_cell(vsin, "draftkings", vmt, side, "bets"))
            if v is not None]
    return (sum(float(v) for v in vals) / len(vals)) if vals else None


def _vsin_split_pp(vsin: dict | None, mt: str, side: str) -> float:
    """Circa handle% − blended (Circa+DK) tickets% on (mt, side). Positive =
    sharp money beyond the public ticket share on this side (the confirmation
    signal). Works for ml/spread/total — VSiN exposes per-market handle+bets,
    unlike Action (which was ML-only). 0.0 when no Circa data for the side."""
    vmt = _VSIN_MT.get(mt)
    if not vmt or not vsin or not vsin.get("matched"):
        return 0.0
    if side not in ("away", "home", "over", "under"):
        return 0.0
    h = _vsin_circa_handle(vsin, vmt, side)
    b = _vsin_blended_bets(vsin, vmt, side)
    if h is None or b is None:
        return 0.0
    return float(h) - float(b)


def _vsin_sharp_veto(vsin: dict | None, mt: str, side: str):
    """If Circa sharp money (handle) is concentrated on the OPPOSITE side
    BEYOND the public ticket share, return (reason, read); else (None, read).
    Circa handle only — no DK-handle fallback. `read` is captured regardless."""
    vmt = _VSIN_MT.get(mt)
    if not vmt or not vsin or not vsin.get("matched"):
        return None, None
    opp = _VSIN_OPP.get(side)
    our_h = _vsin_circa_handle(vsin, vmt, side)
    opp_h = _vsin_circa_handle(vsin, vmt, opp)
    our_b = _vsin_blended_bets(vsin, vmt, side)
    opp_b = _vsin_blended_bets(vsin, vmt, opp)
    if opp_h is None and our_h is None and our_b is None and opp_b is None:
        return None, None
    read = {"sharp_book": "circa",
            "side_handle": our_h, "side_bets": (round(our_b) if our_b is not None else None),
            "opp_handle": opp_h, "opp_bets": (round(opp_b) if opp_b is not None else None)}
    if opp_h is None:                       # no Circa handle = no sharp read = no veto
        return None, read
    gap = (opp_h - opp_b) if opp_b is not None else None
    if (opp_h >= _VSIN_OPP_HANDLE_MIN and gap is not None
            and gap >= _VSIN_SHARP_GAP_MIN):
        return (f"Circa sharp money {opp_h}% on the other side "
                f"(vs {round(opp_b)}% blended tickets) — would be betting into it"), read
    return None, read


# Circa MOVEMENT window: how far back the handle trajectory reads. Snapshots
# only accrue ~24h pre-game (the vsin-snapshot cron's window), so 24h = the
# full history for a game; `d3h` isolates the late move.
_VSIN_MOVE_WINDOW_H = 24


def _vsin_movement(sb, market_id: str) -> dict | None:
    """Circa handle TRAJECTORY per (market, side) from vsin_snapshots — the
    'when does sharp money hit Circa' curve the 15-min cron has been logging
    since June 24. Per side: the earliest + latest Circa handle% in the window,
    the full-window delta, and the last-3h delta (late money). Positive delta =
    money ARRIVING on that side. Returns
      {"ml": {"home": {"open":41,"now":68,"delta":27,"d3h":9,"n":12}, ...}, ...}
    keyed by the vsin market codes (ml/spread/total), or None when no Circa
    snapshots exist for this market. Recorded per pick (signal_blob
    circa_move_pp) for the validation review — NOT a sizing input yet."""
    if not market_id:
        return None
    try:
        since = (datetime.now(timezone.utc)
                 - timedelta(hours=_VSIN_MOVE_WINDOW_H)).isoformat()
        rows = (sb.table("vsin_snapshots")
                .select("market_type,side,handle_pct,captured_at")
                .eq("market_id", market_id).eq("book", "circa")
                .gte("captured_at", since)
                .order("captured_at").limit(2000).execute().data) or []
    except Exception:
        return None
    series: dict[tuple, list] = {}
    for r in rows:
        h = r.get("handle_pct")
        if h is None:
            continue
        series.setdefault((r.get("market_type"), r.get("side")), []).append(
            (r.get("captured_at") or "", int(h)))
    if not series:
        return None
    cut3h = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    out: dict = {}
    for (mt, side), pts in series.items():
        if not mt or not side:
            continue
        first_h, last_h = pts[0][1], pts[-1][1]
        recent = [h for (ts, h) in pts if ts >= cut3h]
        d3h = (pts[-1][1] - recent[0]) if len(recent) >= 2 else None
        out.setdefault(mt, {})[side] = {
            "open": first_h, "now": last_h,
            "delta": last_h - first_h,
            "d3h": d3h, "n": len(pts),
        }
    return out or None


def _circa_move_pp(movement: dict | None, mt: str, side: str):
    """Full-window Circa handle delta for a candidate's (market_type, side).
    None when no trajectory exists (unmatched game / Circa silent)."""
    vmt = _VSIN_MT.get(mt)
    if not movement or not vmt:
        return None
    cell = (movement.get(vmt) or {}).get(side)
    return cell.get("delta") if cell else None


def _vsin_to_ml_splits(vsin: dict | None) -> dict:
    """ML-shaped splits dict (the dossier reason bullet + _splits_signal_pp
    scoring), REPLACING Action Network. money% = CIRCA HANDLE (sharp money,
    Circa only — never DK handle), bets% = BLENDED Circa+DK ticket%. No Circa
    handle ⇒ no splits available (we do NOT fall back to DK as 'smart')."""
    base = {"away_money": None, "home_money": None,
            "away_bets": None, "home_bets": None,
            "sharp_diff": None, "book": None,
            "sources": [], "sources_tried": ["vsin-circa", "vsin-draftkings"],
            "per_source": {}}
    if not vsin:
        return base
    for book in ("circa", "draftkings"):
        ev = (vsin.get("books") or {}).get(book)
        ml = (ev or {}).get("ml") or {}
        slate = (vsin.get("slate") or {}).get(book) or []
        base["per_source"][f"vsin-{book}"] = {
            "matched": (ml.get("away_handle") is not None or ml.get("home_handle") is not None),
            # Full slate size VSiN returned (NOT 0/1) — "0 games" was misleading
            # on a name-mismatch where VSiN actually had a full board.
            "events_returned": len(slate),
            # On a match show the matched game; on a MISS show VSiN's actual
            # slate so we can eyeball the name mismatch (or see the game's absent).
            "sample_games": ([f"{ev.get('away_team','?')} @ {ev.get('home_team','?')}"]
                             if ev else slate[:8]),
        }
    # Sharp money = Circa handle ONLY. No Circa ⇒ no read.
    a_h = _vsin_circa_handle(vsin, "ml", "away")
    h_h = _vsin_circa_handle(vsin, "ml", "home")
    if a_h is None and h_h is None:
        return base
    a_b = _vsin_blended_bets(vsin, "ml", "away")
    h_b = _vsin_blended_bets(vsin, "ml", "home")
    a_b = round(a_b) if a_b is not None else None
    h_b = round(h_b) if h_b is not None else None
    base.update({
        "away_money": a_h, "home_money": h_h,   # Circa handle (sharp)
        "away_bets": a_b, "home_bets": h_b,     # blended Circa+DK tickets (public)
        "book": "circa", "sources": ["vsin-circa"],
        "sharp_diff": (None if h_h is None or h_b is None else h_h - h_b),
    })
    return base


# ───────────── Market-anchored spread model (see SPREAD_MODEL_* above) ─────────────
def _nb_run_pmf(mean: float, nmax: int = SPREAD_RUN_NMAX,
                ratio: float = SPREAD_RUN_VAR_MEAN) -> list[float]:
    """Negative-binomial pmf P(team scores 0..nmax runs) for the given mean and
    variance/mean overdispersion (var = mean·ratio). ratio→1 is the Poisson
    limit. Normalized over the grid."""
    mean = max(mean, 0.05)
    if ratio <= 1.0001:
        pmf = [math.exp(-mean) * mean ** k / math.factorial(k) for k in range(nmax + 1)]
    else:
        r = mean / (ratio - 1.0)             # var = mean + mean^2/r = mean·ratio
        p = r / (r + mean)
        pmf = [math.exp(math.lgamma(k + r) - math.lgamma(r) - math.lgamma(k + 1)
                        + r * math.log(p) + k * math.log(1 - p))
               for k in range(nmax + 1)]
    s = sum(pmf)
    return [x / s for x in pmf] if s else pmf


def _spread_winprob(lh: float, la: float) -> float:
    """P(home wins) from two independent NB run distributions; ties split 50/50
    (extra innings ≈ coinflip)."""
    ph, pa = _nb_run_pmf(lh), _nb_run_pmf(la)
    win = tie = 0.0
    for h, phh in enumerate(ph):
        for a, paa in enumerate(pa):
            j = phh * paa
            if h > a:    win += j
            elif h == a: tie += j
    return win + 0.5 * tie


def _spread_cover_prob(p_home_win: float, total: float, home_line: float):
    """Market-anchored runline cover prob. Solve λ_home+λ_away = total so that
    P(home wins) == p_home_win (direction from the exchange ML), then read the
    cover prob at `home_line` off the joint (margin shape from the run total).
    Returns {home, away, lambda_home, lambda_away}."""
    total = max(float(total), 1.0)
    lo, hi = 0.1, total - 0.1
    for _ in range(40):                       # bisection — winprob ↑ in λ_home
        mid = (lo + hi) / 2.0
        if _spread_winprob(mid, total - mid) < p_home_win:
            lo = mid
        else:
            hi = mid
    lh = (lo + hi) / 2.0
    la = total - lh
    ph, pa = _nb_run_pmf(lh), _nb_run_pmf(la)
    thr = -float(home_line)                   # home covers iff (h-a) > thr
    home_cover = sum(ph[h] * pa[a]
                     for h in range(len(ph)) for a in range(len(pa))
                     if (h - a) > thr)
    return {"home": home_cover, "away": 1.0 - home_cover,
            "lambda_home": round(lh, 2), "lambda_away": round(la, 2)}


def _spread_model_block(odds: dict, power: dict | None, sport: str | None) -> dict | None:
    """SHADOW market-anchored spread read for one game (MLB). Direction = the
    exchange ML devigged win prob; magnitude = the model's proj_total. Returns
    the per-side cover prob, the offered runline, the edge vs the exchange's own
    spread price, and the +EV side (if any clears SPREAD_MODEL_MIN_EDGE_PP).
    None when inputs are missing. Never raises (caller wraps too)."""
    if sport != "MLB" or not power:
        return None
    ml = (odds.get("moneyline") or {}).get("exch_current") or {}
    p_home = ((ml.get("home") or {}).get("fair_prob"))
    proj_total = power.get("proj_total")
    spr = (odds.get("spread") or {}).get("exch_current") or {}
    home_blk = spr.get("home") or {}
    home_line = home_blk.get("line")
    if p_home is None or proj_total is None or home_line is None:
        return None
    try:
        cover = _spread_cover_prob(float(p_home), float(proj_total), float(home_line))
    except Exception:
        return None
    # Exchange's OWN devigged spread prob per side — the price we're calling
    # mispriced. Edge = model cover prob − exchange spread prob.
    mkt = {"home": (spr.get("home") or {}).get("fair_prob"),
           "away": (spr.get("away") or {}).get("fair_prob")}
    pmm = (odds.get("spread") or {}).get("polymarket") or {}
    out = {"home_line": float(home_line), "away_line": -float(home_line),
           "p_home_win": round(float(p_home), 4), "proj_total": round(float(proj_total), 2),
           "lambda_home": cover["lambda_home"], "lambda_away": cover["lambda_away"]}
    best_side, best_edge = None, None
    for side in ("home", "away"):
        cp = cover[side]
        out[side + "_cover"] = round(cp, 4)
        out[side + "_fair_american"] = _prob_to_american(cp)
        edge = round((cp - float(mkt[side])) * 100, 2) if mkt.get(side) is not None else None
        out[side + "_edge_pp"] = edge
        # Maker entry = the PMM bid for that side (else the model fair).
        bid = (((pmm.get(side) or {}).get("quote") or {}).get("bid_american"))
        out[side + "_entry"] = bid if bid is not None else _prob_to_american(cp)
        if edge is not None and edge >= SPREAD_MODEL_MIN_EDGE_PP and (best_edge is None or edge > best_edge):
            best_side, best_edge = side, edge
    if best_side:
        out["bet_side"] = best_side
        out["bet_edge_pp"] = best_edge
        out["line"] = out[best_side + "_line"]
        out["entry_price"] = out[best_side + "_entry"]
        out["fair_american"] = out[best_side + "_fair_american"]
    return out


def _suggest_picks(odds: dict, splits: dict | None = None,
                   power: dict | None = None, *,
                   sport: str | None = None, home: str | None = None,
                   starts_in_min=None,
                   sticky_keys: set | None = None,
                   prime_zones: list[tuple[int, int]] | None = None,
                   prime_zones_by_market: dict | None = None,
                   vsin: dict | None = None,
                   vsin_movement: dict | None = None,
                   ufc_model: dict | None = None,
                   kelly_fraction: float | None = None) -> list[dict]:
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
    aligned splits + power-rating confirmation), snapped to tiers —
    1u is the DEFAULT, 3u/5u are rare strong-conviction step-ups:
      gate not cleared (sharp < 3)       → 1u low (forced lean)
      gate cleared, ¼-Kelly < 2.0%BR     → 1u low (standard pick)
      gate cleared, 2.0%BR ≤ ¼K < 3.0%BR → 3u medium
      gate cleared, ¼-Kelly ≥ 3.0%BR     → 5u high
    Whale (10u) stays disabled. See _kelly_units / EDGE_* constants.
    """
    # Per-bet-type prime zones. Prefer the explicit by-market map; fall back
    # to the legacy single prime_zones list (wrapped as the pooled default)
    # so older callers still work.
    _bm = prime_zones_by_market or (
        {"_pooled": prime_zones} if prime_zones else None)

    candidates: list[dict] = []
    for mt in ("moneyline", "spread", "total"):
        # Full-game O/U: the old exchange-follow engine is benched (-10.8u/30d).
        # MLB totals now run a model-driven TEST tier (test_total) — the side +
        # gate come from power.proj_total vs the live line, sized 0.25u, flagged
        # test_only (see TOTALS_TEST_MODE). Non-MLB totals stay fully dark until
        # TOTALS_SUGGESTIONS_ENABLED. ML/SPR/NRFI unaffected.
        test_total = (mt == "total" and sport == "MLB" and TOTALS_TEST_MODE)
        if mt == "total" and not (TOTALS_SUGGESTIONS_ENABLED or test_total):
            continue
        # Model side + gap for the test tier: project_total − live exchange line.
        model_total_side = None
        model_total_diff = None
        if test_total:
            _xc = (odds.get("total") or {}).get("exch_current") or {}
            _pt = (power or {}).get("proj_total")
            _tl = ((_xc.get("over") or {}).get("line")
                   or (_xc.get("under") or {}).get("line"))
            if _pt is not None and _tl is not None:
                try:
                    # Re-center the projection (backtest-measured cold bias)
                    # before comparing to the line.
                    model_total_diff = (float(_pt) + TEST_TOTAL_BIAS_RUNS) - float(_tl)
                    model_total_side = ("over" if model_total_diff > 0.01
                                        else "under" if model_total_diff < -0.01
                                        else None)
                except (TypeError, ValueError):
                    model_total_diff = model_total_side = None
        blk = odds.get(mt) or {}
        mv = blk.get("movement") or {}
        # PRIMARY = exchange sharp score (PMM cents + Kalshi confirm). The
        # PIN recency-weighted score is retired with its feed (cutover,
        # June 2026); `x_side`/`x_score` are now the headline signal.
        sharp_side  = mv.get("x_side")
        sharp_score = mv.get("x_score") or 0
        x_confirmed = mv.get("x_confirmed")
        sides = ("over", "under") if mt == "total" else ("away", "home")
        for side in sides:
            # Fair anchor: exchange devigged mid (Kalshi/PMM) is primary;
            # pin_current is a fallback only while its feed is still warm.
            fair_src = ((blk.get("exch_current") or {}).get(side)
                        or (blk.get("pin_current") or {}).get(side) or {})
            fair_prob     = fair_src.get("fair_prob")
            fair_american = fair_src.get("fair_american")
            # Need at minimum a fair line to call this a Polymarket target.
            if fair_prob is None or fair_american is None:
                continue
            pin = fair_src   # downstream reference fields read from here

            # TEST totals: only the model's side is a candidate. No live line /
            # no model opinion → no test pick for this game.
            if test_total and (model_total_side is None or side != model_total_side):
                continue

            score_for_side = sharp_score if side == sharp_side else 0
            if test_total:
                # Decouple from exchange movement — the model drives this tier.
                score_for_side = 0
            # Splits confirmation = Circa handle − blended tickets on the sharp
            # side. Now per-market (ml/spread/total) via VSiN, not ML-only —
            # so a total/spread move that Circa money confirms gets the bonus,
            # not just ML. Still gated on exchange movement (confirmation, not
            # a standalone trigger).
            splits_pp = _vsin_split_pp(vsin, mt, sharp_side) if score_for_side > 0 else 0.0
            cs = (SHARP_WEIGHT * (score_for_side / 10.0)
                  + SPLITS_WEIGHT * min(max(0.0, splits_pp) / 30.0, 1.0))
            gates_cleared = score_for_side >= SHARP_SCORE_MIN
            if test_total:
                # Model-driven gate: clears when the projection beats the line
                # by ≥ TEST_TOTAL_MIN_DIFF runs. combined_score scales with the
                # gap so the test total orders sensibly among candidates.
                gap = abs(model_total_diff or 0.0)
                gates_cleared = gap >= TEST_TOTAL_MIN_DIFF
                cs = min(gap / 3.0, 1.0)
            # PMM+Kalshi confirmation gate (ML-only — the only market both
            # exchanges quote). A moneyline move PMM shows but Kalshi
            # doesn't confirm is mostly noise: on the live disagreement set
            # the unconfirmed side won only ~3-of-9. So an unconfirmed ML
            # signal stays visible but DEMOTES to a forced lean (1u) rather
            # than a real pick. SPR/TOT have no second venue → x_confirmed
            # is True for them, so this never fires there. TRI-STATE:
            # x_confirmed is None when Kalshi has NO rows for this game at
            # all (single-venue — UFC/NCAAF, or a matcher miss) — that's
            # structural absence, not evidence, so `is False` (never
            # `not x_confirmed`) is load-bearing here.
            unconfirmed_ml = False
            if (mt == "moneyline" and gates_cleared
                    and score_for_side > 0 and x_confirmed is False):
                gates_cleared = False
                unconfirmed_ml = True
            # Sticky gate (hysteresis — the "5-minute pick" fix, June 2026).
            # The recency-weighted score DECAYS as a move ages, so a real
            # steam cleared the gate only while <15min fresh, then the pick
            # demoted back to a lean and vanished from the page. Schmitt
            # trigger: ENTER at SHARP_SCORE_MIN; once this (market, side)
            # has cleared (paperlog memory, threaded in via sticky_keys),
            # it STAYS a real pick while the score holds ≥ STICKY_GATE_EXIT.
            sticky = False
            if (not gates_cleared and not unconfirmed_ml and sticky_keys
                    and (mt, side) in sticky_keys
                    and score_for_side >= STICKY_GATE_EXIT):
                gates_cleared, sticky = True, True

            # Total-side veto — the OUR-NUMBER read can DEMOTE a total pick
            # to a forced lean when it disagrees with the side movement
            # chose (hitter-park unders, model-projected total the other
            # way). Stops the bot recommending Rockies/Coors unders. ML/SPR
            # are untouched — the veto is totals-only.
            conflict_reason = None
            # NO veto on the test tier. The point of the 0.25u O/U test is to
            # measure THE MODEL ALONE, so nothing outside the model may suppress
            # a pick. The park veto is redundant — _power_rating_v2 already
            # scales proj_total by _park_factor, so a park-aware projection that
            # STILL lands under the line in Coors is the model's real call;
            # vetoing it would override my own park layer AND suppress the exact
            # park-case picks the test needs to validate. The model-lean veto is
            # moot too (the side already IS the model's side). VSiN is recorded
            # below but not allowed to veto, so the 2-week sample is the pure
            # model — we slice by park / VSiN agreement afterward.
            if mt == "total" and not test_total:
                conflict_reason = _total_conflict_reason(sport, home, power, side)
                if conflict_reason:
                    gates_cleared = False

            # Fight IQ winner veto (UFC ML only — the total-side-veto
            # pattern applied to fights). The model is REFERENCE-ONLY for
            # sizing, but a >= UFC_MODEL_VETO_P read AGAINST a steam pick
            # demotes it to a forced lean — built for sentiment fights
            # (the McGregor archetype: retail floods a name, PMM drifts,
            # the model screams age+layoff+chin the other way). Recorded
            # on every UFC ML candidate either way for the paperlog.
            ufc_p_side = None
            ufc_model_vetoed_pick = False
            if (sport == "UFC" and mt == "moneyline" and ufc_model
                    and ufc_model.get("matched")):
                ufc_p_side = (ufc_model.get("p") or {}).get(side)
                if (gates_cleared and ufc_model.get("reliable")
                        and ufc_p_side is not None
                        and ufc_p_side <= 1.0 - UFC_MODEL_VETO_P):
                    gates_cleared = False
                    ufc_model_vetoed_pick = True
                    conflict_reason = (f"Fight IQ has the other corner "
                                       f"{round((1 - ufc_p_side) * 100)}% "
                                       f"— model-vs-steam veto")

            # VSiN sharp-money veto (Circa handle) — ALL markets EXCEPT the test
            # totals tier. If the sharp money is piled on the opposite side,
            # betting this side is fading into it → demote to a forced lean.
            # Captured (vsin_read) on every candidate for forward validation
            # regardless of whether it fired (test totals: recorded, not vetoed).
            vsin_reason, vsin_read = _vsin_sharp_veto(vsin, mt, side)
            # WARN-ONLY (July 2026) — the veto no longer demotes. Backtested
            # 9 days of vsin_snapshots (134 MLB games) against game outcomes
            # vs the exchange close: betting the side OPPOSITE concentrated
            # Circa money (the veto's exact protection) performed dead-even
            # with the close (33-34, +0.0 wins above close over 67 games) —
            # the veto protected against nothing while killing ~half the ML
            # slate (a veto-triggering side exists on 54% of MLs). The read
            # is kept as a card badge + recorded (signal_blob.vsin_veto) so
            # real-pick results keep measuring it; re-enable only if that
            # forward data contradicts the backtest. `vsin_vetoed_pick` is
            # retained for the (now-dormant) shadow-log path.
            vsin_vetoed_pick = False

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
            if test_total:
                # Edge from the model gap (display only — test stake is flat).
                edge_pp = round(min(abs(model_total_diff or 0.0)
                                    * TEST_TOTAL_EDGE_PER_RUN, EDGE_CAP_PP), 2)

            # PMM-projected fair: if a Polymarket market exists for
            # this side AND the push-rate projection is applicable
            # (delta ≤ 0.5 between PIN and PMM lines), prefer the
            # projected fair at PMM's line as the limit-order target.
            # When projection ISN'T applicable (multi-point gap, missing
            # push rate), keep PIN's line + PIN fair as a coherent pair
            # so we never display "PMM line + PIN fair" together
            # (inconsistent and misleading).
            pmm_block = (blk.get("polymarket") or {}).get(side)
            pmm_line = (pmm_block or {}).get("line")
            pmm_quote = (pmm_block or {}).get("quote") or {}
            # LINE-CONSISTENCY GUARD (post-cutover, June 2026). exch_current's
            # devigged fair is at the at-the-money line; the PMM quote block can
            # be attached at a DIFFERENT line (best_line_for was keyed off the
            # now-dead PIN line, so post-cutover it can land on the wrong line).
            # Using that quote's bid as the entry while the fair sits at another
            # line produced the "Yankees +1.5 @ -285 entry on a -1.5/-100 fair"
            # phantom — incoherent, AND it dodged the SPR chalk filter (which
            # guards on the -100 fair). For SPR/TOT, only trust the PMM quote
            # when its line matches the exchange-fair line; otherwise drop it so
            # the entry falls back to the exchange fair at ITS own line. (ML has
            # no line, so it's always coherent.) The White Sox over-8.5 case is
            # untouched — there the PMM line already equals the fair line.
            if mt != "moneyline":
                _fl = pin.get("line")
                if not (pmm_line is not None and _fl is not None
                        and abs(float(pmm_line) - float(_fl)) < 0.001):
                    pmm_block, pmm_line, pmm_quote = None, None, {}
            # Post-cutover: the EXCHANGE devigged mid (exch_current) IS the
            # Polymarket fair at the at-the-money line — no PIN push-rate
            # projection. Target line + fair come straight from fair_src;
            # the PMM quote (re-anchored to that line in _attach_pmm_to_odds)
            # supplies the maker bid for entry. use_pmm now means "PMM quotes
            # the target line" (consistency guard for the displayed pmm_line).
            target_fair_american = fair_american
            target_line = pin.get("line")
            use_pmm = (pmm_line is not None and target_line is not None
                       and abs(pmm_line - target_line) < 0.001)

            candidates.append({
                "market_type":    mt,
                "side":           side,
                "sharp_score":    score_for_side,
                "splits_pp":      round(splits_pp, 1),
                "edge_pp":        edge_pp,
                "model_edge_pp":  round(model_edge, 2),
                # TEST O/U tier — model-driven (proj_total vs line), 0.25u.
                "test_only":      bool(test_total),
                "model_total_diff": (round(model_total_diff, 2)
                                     if test_total and model_total_diff is not None
                                     else None),
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
                "pmm_synthetic":  bool((pmm_block or {}).get("synthetic")),
                "uses_pmm_projection": use_pmm,
                "combined_score": round(cs, 4),
                "gates_cleared":  gates_cleared,
                "sticky":         sticky,
                # Exchange-score shadow (cutover review): does the
                # PMM+Kalshi read agree with the PIN read on this side?
                "x_score":        mv.get("x_score"),
                "x_side":         mv.get("x_side"),
                "x_agree":        ((mv.get("x_side") == side)
                                   if mv.get("x_side") else None),
                # Tri-state on purpose: True agree / False quoted-but-silent
                # / null no-Kalshi-read — the paperlog review needs to tell
                # PMM-only sports apart from genuinely unconfirmed moves.
                "x_confirmed":    (None if x_confirmed is None
                                   else bool(x_confirmed)),
                "unconfirmed_ml": unconfirmed_ml,
                "fair_source":    fair_src.get("source"),
                "conflict_reason": conflict_reason,
                # VSiN sharp-money read (Circa/DK handle vs bets) — captured on
                # every candidate; `vsin_veto` true when it demoted this side.
                "vsin":           vsin_read,
                "vsin_veto":      bool(vsin_reason),
                "vsin_vetoed_pick": vsin_vetoed_pick,
                # Fight IQ (UFC ML): the model's P for THIS side + whether
                # the winner veto fired — the paperlog's veto counterfactual.
                "ufc_model_p":    ufc_p_side,
                "ufc_model_agree": ((ufc_p_side >= 0.5)
                                    if ufc_p_side is not None else None),
                "ufc_model_vetoed_pick": ufc_model_vetoed_pick,
                # Circa handle TRAJECTORY on this side (full-window delta from
                # vsin_snapshots) — positive = money arriving. Recorded for the
                # review; NOT a scoring/sizing input yet.
                "circa_move_pp":  _circa_move_pp(vsin_movement, mt, side),
                # Per-bet-type timing: each market is classified against ITS
                # own tuned zones (markets without their own fall back to
                # pooled). _bm is built once below from prime_zones_by_market
                # or the legacy single prime_zones list. Totals have no tuned
                # zones yet → pooled. (Model-driven totals are now REAL 1u picks,
                # so they DO carry a timing_window and obey the prime-only games
                # list, same as ML/SPR — the old null-for-test-tier special-case
                # is gone now that they aren't a flat 0.25u test.)
                "timing_window":  _timing_window(
                                      starts_in_min, _market_zones(_bm, mt)),
                "prime_core":     _is_prime_core(starts_in_min),
            })

    if not candidates:
        return []

    # Drop out-of-band SPR — band -150 ≤ fair ≤ +186 (60¢–35¢, Poly whole
    # cents). Too chalky (≤ -150) is a worse-EV leveraged ML; too longshot
    # (≥ +186) is a leveraged-dog runline with no edge (the Reds -1.5 +292
    # lean). Either
    # way the SPR isn't worth showing — the ML candidate (if any) takes its
    # slot, and a chalk-flat game with only out-of-band SPR falls back to a
    # sane ML/TOT lean instead of a longshot runline.
    candidates = [
        c for c in candidates
        if not (c["market_type"] == "spread"
                and c.get("fair_american") is not None
                and (c["fair_american"] <= SPR_CHALK_FAIR_CAP
                     or c["fair_american"] >= SPR_LONGSHOT_FAIR_CAP))
    ]
    if not candidates:
        return []

    # Best candidate per market_type (with Kelly sizing applied).
    # Sizing is now quarter-Kelly off the signal-derived edge_pp, snapped
    # to the 1/3/5u tiers, replacing the old fixed "sharp≥5 AND splits≥5"
    # thresholds. 1u is the default for a real pick; Kelly only promotes
    # to 3u/5u on a large stake. The sharp gate (gates_cleared) decides
    # real-pick (blue "Bot Suggests") vs forced lean (grey) — independent
    # of unit size, so a real pick can be 1u. Whale (10u) stays disabled — live results showed
    # it was a FADE indicator (23% over 35 picks). The CLV column will
    # let Stage-4 recalibrate the edge coefficients from realized data.
    by_market: dict[str, dict] = {}
    for c in candidates:
        units, conf, kelly_pct = _kelly_units(
            c.get("fair_prob"), c.get("fair_american"),
            c.get("edge_pp"), c.get("gates_cleared"), kelly_fraction)
        # TOP TIER IS NOW 3U (June 2026, user call) — 5u disabled. The 5u
        # tier carried too much variance for the edge it captured; the
        # user wants the ceiling at 3u. Clamp here so nothing downstream
        # emits 5u; the sharp gate below still requires real steam for 3u.
        if units > 3:
            units, conf = 3, "medium"
        # SIZE-UP IS MONEYLINE-ONLY (June 2026 — 365-pick live review).
        # Sized-up SPR went 0-10 (-12.0u) and TOT 9-11 (-7.2u) while ML
        # went 19-12 (+27.4u): the Kelly signal is real for moneyline and
        # pure variance on spreads/totals. So anything outside
        # SIZE_UP_MARKETS caps at 1u. See the constant for the data + how
        # to re-enable. (Runs before the prime-window cap below; once 1u
        # here that cap's `units > 1` is a no-op.)
        if c.get("market_type") not in SIZE_UP_MARKETS and units > 1:
            units, conf = 1, "low"
            c["size_capped"] = True
        # SHARP-GATED SIZING — size-up to 3u must be earned by sharp steam,
        # not by edge_pp pegging at its cap from splits/model. See
        # SHARP_FOR_3U for the data. Caps DOWN only; a decayed sticky score
        # (< SHARP_FOR_3U) can never reach 3u here. (5u is already gone.)
        sharp_for_size = c.get("sharp_score") or 0
        if units > 1 and sharp_for_size < SHARP_FOR_3U:
            units, conf = 1, "low"
            c["size_capped"] = True
        # Model-driven totals — flat TEST_TOTAL_UNITS (now 1u, promoted June
        # 2026 from the 0.25u test). Sizing is flat (the model has no Kelly/
        # sharp signal to size on), so this overrides the caps above and lands
        # at a clean 1u. Still capped at 1u (totals are excluded from
        # SIZE_UP_MARKETS), and prime-only display still gates the card chip.
        if c.get("test_only"):
            units, conf = TEST_TOTAL_UNITS, "low"
        # Sizing concentrated in the PRIME window — only picks made 90-120
        # min before first pitch can size past 1u. That 38-pick window
        # carried +27u at 68.4%; 60-90 was modest (+3.9u), the last hour
        # mush. So everything OUTSIDE prime with a known kickoff time is
        # capped at 1u (caps DOWN only; unknown kickoff → no cap). `late`
        # picks (<60m) keep the amber badge flag too.
        tw = c.get("timing_window")
        if tw is not None and tw != "prime" and units > 1:
            units, conf = 1, "low"
            c["size_capped"] = True
            if tw == "late":
                c["late_capped"] = True
        # Forced leans are HALF-UNIT (July 2026, user call — leans returned to
        # the games list as muted 0.5u chips after the June declutter hid
        # them). Runs last so no cap/override can lift a lean back to 1u.
        # Schema allows 0.5 (migration 009); the paperlog still skips leans.
        if not c.get("gates_cleared"):
            units, conf = LEAN_UNITS, "low"
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

    # VSiN-vetoed picks that didn't survive selection ride along as extra
    # entries (gates_cleared=False, so they render as leans on the dossier —
    # the "why won't you pick this" answer — and NEVER as games-list chips).
    # The paperlog shadow-logs them (signal_blob.vsin_vetoed_pick) so the
    # veto finally gets a measurable counterfactual. At most one vetoed side
    # per market (the veto needs ≥60% opposite handle, which only one side
    # can hold); keep the best per market_type.
    already = {id(c) for c in chosen}
    best_veto: dict[str, dict] = {}
    for c in candidates:
        if not c.get("vsin_vetoed_pick") or id(c) in already:
            continue
        cur = best_veto.get(c["market_type"])
        if not cur or c["combined_score"] > cur["combined_score"]:
            best_veto[c["market_type"]] = c
    chosen.extend(best_veto.values())
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

    # VSiN sharp-money splits (Circa + DK handle/bets) — the SOLE splits
    # source now (Action Network is retired for picks: unreliable, and it
    # contradicted Circa on live games). `vsin` (full per-market) feeds the
    # sharp-money veto; `splits` (ML-shaped, Circa-preferred) feeds the reason
    # bullet + _splits_signal_pp, and is surfaced read-only on the dossier.
    vsin = _vsin_for_game(sport, away, home) if (away and home) else None
    splits = _vsin_to_ml_splits(vsin)

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
        # Instrument the lookup outcome to Vercel runtime logs so a miss is
        # diagnosable from the server side (no UI round-trip needed): which
        # tag/window was used, how many events PMM returned, and their titles.
        try:
            _samples = " | ".join(
                (pmm_diag.get("sample_event_titles") or [])[:5])
            print(f"[pmm] {sport} {away}@{home} matched={bool(pmm_data)} "
                  f"err={pmm_error!r} tag={pmm_diag.get('tag')!r} "
                  f"win={pmm_diag.get('window_min')}..{pmm_diag.get('window_max')} "
                  f"events={pmm_diag.get('events_returned')} "
                  f"matched_title={pmm_diag.get('matched_title')!r} "
                  f"samples=[{_samples}]")
        except Exception:
            pass

    # Exchange CURRENT fair anchor (Kalshi mid / PMM fallback, devigged) —
    # attached BEFORE the PMM quotes so the PMM line selection anchors on
    # the exchange at-the-money line (not the dead PIN line). This is the
    # cutover fix for "why is the total on 6.5 when PMM's main line is 7.5":
    # PIN froze at 6.5, so anchoring PMM on it picked the wrong line.
    _attach_exch_current(sb, market["id"], odds)

    # Attach PMM market info onto each odds[market_type] block so the UI can
    # render the PMM line + bid/ask next to the exchange fair, and supply
    # the maker bid (entry price) at the at-the-money line. Skipped silently
    # when no PMM data.
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
            _inj_dbg_h: dict = {}
            _inj_dbg_a: dict = {}
            home_t["injuries"] = _espn_team_injuries(sport, home_t.get("id"), diag=_inj_dbg_h)
            away_t["injuries"] = _espn_team_injuries(sport, away_t.get("id"), diag=_inj_dbg_a)
            home_t["recent"]   = _espn_team_recent(sport, home_t.get("id"))
            away_t["recent"]   = _espn_team_recent(sport, away_t.get("id"))
            comp = (m["event"].get("competitions") or [{}])[0]
            espn_block = {
                "venue":      ((comp.get("venue") or {}).get("fullName")),
                "broadcasts": [b.get("names") for b in comp.get("broadcasts") or [] if b.get("names")],
                "home":       home_t,
                "away":       away_t,
                "inj_debug":  {"home": _inj_dbg_h, "away": _inj_dbg_a},
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

    # NRFI / YRFI first-inning model (MLB only) — an OUR-NUMBER prop
    # priced from the opposing starters + top-of-order on-base + park +
    # temp. No line in our feed, so it's surfaced as a standalone card
    # with a lean-vs-baseline; the user shops a book/PMM price. Silent-fail.
    nrfi = None
    if sport == "MLB":
        try:
            nrfi = _nrfi_model(sport, (mlb_extra or {}).get("probable_pitchers"),
                               away, home, _pp.get("game_pk"), weather,
                               pmm_nrfi=(pmm_data or {}).get("nrfi"))
        except Exception as e:
            log.warning("nrfi model failed: %s", e)
            nrfi = None

    # Fight IQ — the UFC model (winner P + duration family). Reference +
    # veto duty only; never feeds sizing. Silent-fail.
    ufc_model = None
    if sport == "UFC":
        try:
            ufc_model = _ufc_model_block(sb, away, home, event_start, odds,
                                         pmm_data)
        except Exception as e:
            log.warning("ufc model failed: %s", e)
            ufc_model = None

    # Exchange-based sharp score — side-by-side with PIN during the
    # Odds-API cutover (deadline June 25). Attached BEFORE _suggest_picks
    # so every suggestion carries x_score/x_side/x_agree for the
    # agreement review.
    _attach_xsharp(sb, market["id"], odds)
    # (exch_current already attached above, before _attach_pmm_to_odds, so
    # the PMM line anchors on the exchange ATM line.)

    # Circa handle trajectory from vsin_snapshots (the movement curve the
    # 15-min cron logs) — recorded on every candidate for the review + shown
    # on the Details market panels. Silent-fail.
    try:
        vsin_movement = _vsin_movement(sb, market["id"])
    except Exception:
        vsin_movement = None

    suggestions = _suggest_picks(odds, splits, power_rating,
                                 sport=sport, home=home,
                                 starts_in_min=starts_in_min,
                                 sticky_keys=_sticky_keys(sb, market["id"]),
                                 prime_zones_by_market=_load_prime_zones_by_market(sb),
                                 vsin=vsin,
                                 vsin_movement=vsin_movement,
                                 ufc_model=ufc_model,
                                 kelly_fraction=_load_kelly_fraction(sb))
    # Keep the singular `suggestion` field as an alias for the top pick
    # so any caller still expecting it doesn't break. New code should
    # use `suggestions` (list) so multi-pick games render correctly.
    suggestion = suggestions[0] if suggestions else None

    # SHADOW market-anchored spread read (direction from the exchange ML, margin
    # from the run model) — see _spread_model_block. Logged to the paperlog +
    # shown on Details for validation; NOT yet a card pick. Silent-fail.
    spread_model = None
    if SPREAD_MODEL_MODE:
        try:
            spread_model = _spread_model_block(odds, power_rating, sport)
        except Exception as e:
            log.warning("spread model failed: %s", e)

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

    # POST-CUTOVER (June 2026): PIN/Odds-API ingest is RETIRED — book_snapshots
    # is frozen and odds_ingest_runs is dead, so pin_latest_captured / cron_last_run
    # are stale by design (the alarming "cron 19h ago"). The LIVE freshness signal
    # is now the exchange logger (pm_snapshots). exch_latest_captured = the most
    # recent exchange cent row for this game; the UI shows this as the real
    # "data Nm ago" instead of the dead PIN/cron heartbeat.
    exch_latest_captured = None
    try:
        r = (sb.table("pm_snapshots").select("captured_at")
             .eq("market_id", market["id"])
             .order("captured_at", desc=True).limit(1).execute().data) or []
        if r:
            exch_latest_captured = r[0].get("captured_at")
    except Exception:
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
        "vsin":            vsin,
        "vsin_movement":   vsin_movement,
        "espn":            {
            "home":       espn_block.get("home"),
            "away":       espn_block.get("away"),
            "broadcasts": espn_block.get("broadcasts"),
            "inj_debug":  espn_block.get("inj_debug"),
        } if espn_block else None,
        "mlb":             mlb_extra or None,
        "team_compare":    team_compare,
        "weather":         weather,
        "power_rating":    power_rating,
        "nrfi":            nrfi,
        "spread_model":    spread_model,
        "ufc_model":       ufc_model,
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
            "pin_latest_captured":  pin_latest_captured,
            "cron_last_run":        cron_last_run,
            "exch_latest_captured": exch_latest_captured,   # the live signal post-cutover
            "source":               "live" if live_used else "cached",
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
