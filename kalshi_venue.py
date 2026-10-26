"""KALSHI AS A HAND-BET VENUE — the venue layer (Oct 18 2026).

Rob: "change my betting machine to automatically calculate the fees and
figure out where to keep these bets while they're resting, move them back
and forth… when we have to take a bet… would it be better to take on
Kalshi versus Polymarket based on what the take line is at the 95% drop?"

This module is the Kalshi half of that: public market reads (paged),
game/market matching by TEAM NAME (no code tables — RULE 0.001: Kalshi
lists everything we bet; a miss here is a reader bug, never "Kalshi
doesn't have it"), the venue's own fee schedule read per SERIES (never a
table — the maker fee is charged on some series and not others, and the
list moves), and the pure all-in math both venues share.

Pure functions carry no I/O so cellar/selftest.py can prove them.
Spec: docs/dual-venue-hand-bets-spec.md.
"""
from __future__ import annotations

import logging
import math
import re
import time
import unicodedata
from datetime import datetime, timezone

import requests as _http

log = logging.getLogger("kalshi_venue")

BASES = [
    "https://api.elections.kalshi.com/trade-api/v2",
    "https://external-api.kalshi.com/trade-api/v2",
    "https://trading-api.kalshi.com/trade-api/v2",
]
_HDRS = {"Accept": "application/json", "User-Agent": "kahla-house/1.0"}

# Per-game series by sport. ML series are the ones the pm-snapshot logger
# has read for months (MLB/NBA/NHL verified; NFL/NCAAF believed). Spread and
# total series are CANDIDATES — the catalog discovery below finds the real
# ticker when a guess is wrong, and the coverage probe prints what it found.
SERIES_ML = {"MLB": "KXMLBGAME", "NBA": "KXNBAGAME", "NHL": "KXNHLGAME",
             "NFL": "KXNFLGAME", "NCAAF": "KXNCAAFGAME"}
SERIES_LINE = {
    "MLB": {"total": ["KXMLBTOTAL"], "spread": ["KXMLBSPREAD"]},
    "NBA": {"total": ["KXNBATOTAL"], "spread": ["KXNBASPREAD"]},
    "NHL": {"total": ["KXNHLTOTAL"], "spread": ["KXNHLSPREAD"]},
    "NFL": {"total": ["KXNFLTOTAL"], "spread": ["KXNFLSPREAD"]},
    "NCAAF": {"total": ["KXNCAAFTOTAL"], "spread": ["KXNCAAFSPREAD"]},
}
_SPORT_TAGS = {"MLB": ("MLB",), "NBA": ("NBA",), "NHL": ("NHL",),
               "NFL": ("NFL",), "NCAAF": ("NCAAF", "CFB", "COLLEGEFOOTBALL")}

# ───────────────────────────── fees (pure) ─────────────────────────────
# Polymarket US: taker 5¢·p(1−p), maker 1.25¢·p(1−p) charged at fill
# (both calibrated on Rob's own tickets — app._pmm_*_fee_cents).
# Kalshi: taker 7¢·p(1−p) per contract, the ORDER's fee rounded UP to the
# cent; maker 1.75¢·p(1−p) ONLY on series whose fee type carries maker
# fees, else $0. The multiplier M is per series (default 1). Read it off
# the series object (`series_fee`), never from a table.
# ── THE FEES, READ OFF BOTH VENUES' OWN SCHEDULES Oct 18 2026 (Rob: "read
# all the fees… since it's what determines the venue") ──────────────────
# POLYMARKET US (docs.polymarket.us/fees, effective exchange-wide Oct 7 2026):
#   taker  = 0.0695 × C × p(1−p)       (the code carried 0.05 — a stale number)
#   maker  = $0 for us. (The −0.0125 × C × p(1−p) rebate ends for Tier 1 at
#            1:00 PM ET Mon Oct 19 2026; Rob: ignore it — the compare prices
#            the Polymarket maker side at exactly zero, always.)
#   rounding: banker's to the cent per fill, cumulative across an order.
# KALSHI (kalshi.com/docs/kalshi-fee-schedule.pdf via the series `fee_type`):
#   taker  = 0.07 × C × p(1−p) × fee_multiplier
#   maker  = taker × 0.25 on `quadratic_with_maker_fees` series (0.0175 —
#            the July 12 2026 fill receipt: 3 × 0.3808¢ at 32¢), × 0.5 on
#            `quadratic_with_combo_maker_fees`, $0 on plain `quadratic`;
#            `flat` is the Specific Trading Fees table (read conservatively).
#   ⚠ an EVENT may carry `fee_type_override` / `fee_multiplier_override`
#   that beat the series (GET /events/{ticker}) — `event_fee` reads it.
#   rounding: the trade fee rounds UP to $0.000001 per fill, then a rounding
#   fee aligns the balance to the member's cent — ≈ the order's fee ceil'd
#   to the cent, which is what `order_fee_c` does.
POLY_TAKER_C = 6.95
POLY_MAKER_REBATE_C = 1.25                     # credited, until the tier cutoff
POLY_MAKER_TIER1_FROM = datetime(2026, 10, 19, 17, 0, tzinfo=timezone.utc)   # 1 PM ET Oct 19 2026
KALSHI_TAKER_C = 7.0
KALSHI_MAKER_RATIO = {"quadratic": 0.0, "quadratic_with_maker_fees": 0.25,
                      "quadratic_with_combo_maker_fees": 0.5, "flat": 0.25}
KALSHI_MAKER_C = KALSHI_TAKER_C * 0.25       # 1.75 — the common maker-fee series
POLY_MAKER_C = 0.0                           # legacy name: Polymarket charges makers nothing


def poly_maker_c(at=None) -> float:
    """Polymarket's maker coefficient in cents: ZERO (Rob, Oct 18 2026:
    "ignore the rebate on Polymarket, that shit's gone — assume Polymarket
    maker is zero and Kalshi is whatever it is"). Tier 1 gets no rebate
    from the Oct 19 cutoff and the pennies before it bought nothing but a
    compare that leaned on a dying number. `at` is accepted for the callers
    and ignored."""
    return 0.0


def kalshi_maker_ratio(fee: dict | None) -> float:
    """Maker fee as a share of the taker fee for a Kalshi fee-terms dict."""
    if not fee:
        return 0.25                              # unreadable → the common charged case
    if fee.get("maker_ratio") is not None:
        return float(fee["maker_ratio"])
    ft = str(fee.get("fee_type") or "").lower()
    if ft in KALSHI_MAKER_RATIO:
        return KALSHI_MAKER_RATIO[ft]
    return 0.25 if fee.get("maker", True) else 0.0


def _pq(price_c) -> float:
    p = max(0.0, min(1.0, float(price_c) / 100.0))
    return p * (1.0 - p)


def fee_per_contract_c(venue: str, role: str, price_c, fee: dict | None = None, at=None) -> float:
    """Fee in cents per contract for buying at `price_c`; NEGATIVE = a
    rebate paid to us. role = 'take' | 'rest'. `fee` = the Kalshi fee-terms
    dict (`series_fee` / `event_fee`: fee_type, mult, maker_ratio); None =
    the conservative read (maker charged at 0.25, mult 1). `at` = the time
    the fee applies (Polymarket's maker rebate ends Oct 19 2026 1 PM ET)."""
    if price_c is None:
        return 0.0
    v = (venue or "").upper()
    if v.startswith("POLY"):
        coef = POLY_TAKER_C if role == "take" else poly_maker_c(at)
        return round(coef * _pq(price_c), 4)
    mult = float((fee or {}).get("mult", 1.0) or 1.0)
    if role == "take":
        return round(KALSHI_TAKER_C * mult * _pq(price_c), 4)
    return round(KALSHI_TAKER_C * mult * kalshi_maker_ratio(fee) * _pq(price_c), 4)


def order_fee_c(venue: str, role: str, price_c, qty: int, fee: dict | None = None, at=None) -> float:
    """The whole order's fee in cents (negative = rebate). Kalshi's fee
    rounds UP to the next cent on the order; Polymarket's rounds to the
    nearest cent (banker's) per fill."""
    per = fee_per_contract_c(venue, role, price_c, fee, at)
    tot = per * max(0, int(qty or 0))
    if (venue or "").upper().startswith("KALSHI"):
        return float(math.ceil(tot - 1e-9)) if tot > 0 else 0.0
    return round(tot, 2)


def all_in_c(venue: str, role: str, price_c, qty: int, fee: dict | None = None, at=None):
    """Per-contract all-in cost in cents: price + the order's fee (or minus
    its rebate) spread over its contracts. None when there is no price."""
    if price_c is None or not qty:
        return None
    return round(float(price_c) + order_fee_c(venue, role, price_c, qty, fee, at) / int(qty), 3)


def compare_rest(poly_touch_c, kalshi_touch_c, qty: int, kalshi_fee: dict | None,
                 current: str | None = None, min_gain_c: float = 0.0, at=None):
    """Pure. WHERE A RESTING BET SITS. Each venue's seat = our-side touch
    (the price we would join) + its maker fee. Lower all-in wins; a tie, or
    a gain under `min_gain_c`, keeps the bet where it is (`current`) so the
    box never churns a venue switch on noise. Returns (venue, detail)."""
    opts = {}
    a = all_in_c("POLYMARKET", "rest", poly_touch_c, qty, None, at)
    if a is not None:
        opts["POLYMARKET"] = a
    b = all_in_c("KALSHI", "rest", kalshi_touch_c, qty, kalshi_fee, at)
    if b is not None:
        opts["KALSHI"] = b
    if not opts:
        return None, {"why": "no_quote"}
    best = min(opts, key=opts.get)
    det = {"all_in": opts, "best": best}
    if current and current in opts and best != current:
        gain = opts[current] - opts[best]
        if gain <= 1e-9:
            # A TIE STAYS PUT (Oct 18 2026, the COL@PHI under: 19 switches in
            # 4.5h — 53 vs 53 read "best = POLYMARKET" because min() takes the
            # first key, and the bet walked home every other tick).
            det["best"] = current
            det["why"] = "tie"
            return current, det
        if gain < float(min_gain_c) - 1e-9:
            det["why"] = "gain_under_threshold"
            return current, det
        det["why"] = "switch"
        return best, det
    det["why"] = "stay" if current == best else "best"
    return best, det


def compare_take(poly_ask_c, kalshi_ask_c, qty: int, kalshi_fee: dict | None,
                 resting_c=None, cap_c=None, at=None):
    """Pure. THE TAKE AT THE DROP across venues: each ask + its taker fee;
    the cheaper all-in wins. With `resting_c` and `cap_c` the winner must
    sit within cap_c of the resting price on its RAW ask (Rob's 1¢ rule
    is on the line, the fee is the cost of crossing). Returns (venue or
    None, detail)."""
    opts, raw = {}, {}
    a = all_in_c("POLYMARKET", "take", poly_ask_c, qty, None, at)
    if a is not None:
        opts["POLYMARKET"], raw["POLYMARKET"] = a, float(poly_ask_c)
    b = all_in_c("KALSHI", "take", kalshi_ask_c, qty, kalshi_fee, at)
    if b is not None:
        opts["KALSHI"], raw["KALSHI"] = b, float(kalshi_ask_c)
    if not opts:
        return None, {"why": "no_ask"}
    order = sorted(opts, key=opts.get)
    det = {"all_in": opts, "ask": raw}
    for v in order:
        if resting_c is not None and cap_c is not None and raw[v] - float(resting_c) > float(cap_c) + 1e-9:
            continue
        det["why"] = "cheapest_within_cap" if resting_c is not None else "cheapest"
        return v, det
    det["why"] = "every_ask_past_cap"
    return None, det


# ───────────────────────────── reads (I/O) ─────────────────────────────
_CACHE: dict = {}


def _get(path: str, params: dict | None = None, timeout: float = 10.0):
    last = None
    for base in BASES:
        try:
            r = _http.get(base + path, params=params or {}, headers=_HDRS, timeout=timeout)
            if r.status_code == 200:
                return r.json() or {}, None
            last = f"HTTP {r.status_code} @ {base}: {(r.text or '')[:140]}"
            if r.status_code == 404:
                return None, last
        except Exception as e:  # noqa: BLE001
            last = f"{type(e).__name__} @ {base}: {str(e)[:120]}"
    return None, last


def markets(series: str, status: str = "open", max_pages: int = 25, ttl_s: float = 30.0) -> dict:
    """EVERY open market of a series — paged on the cursor (a college
    Saturday's spread ladders run past one 200-row page). {ok, markets:
    [raw dicts], pages, error}. Short cache; failures are not cached."""
    key = f"m:{series}:{status}"
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < ttl_s:
        return hit[1]
    out, cursor, pages, err = [], None, 0, None
    while pages < max_pages:
        params = {"series_ticker": series, "status": status, "limit": 200}
        if cursor:
            params["cursor"] = cursor
        data, err = _get("/markets", params)
        if data is None:
            break
        rows = data.get("markets") or []
        out.extend(rows)
        pages += 1
        cursor = data.get("cursor")
        if not cursor or not rows:
            break
    res = {"ok": pages > 0 and err is None or bool(out), "series": series,
           "markets": out, "pages": pages, "error": err}
    if res["ok"]:
        _CACHE[key] = (time.time(), res)
    return res


def events(series: str, status: str = "open", max_pages: int = 10, ttl_s: float = 60.0) -> dict:
    """EVERY open EVENT of a series (one row per game — ~60 rows on a
    college week, never the thousands of rungs `markets` pages through).
    {ok, events: [raw dicts], pages, error}. Short cache; failures not
    cached. Oct 19 2026: the series-wide markets walk capped at 8 × 200
    rows, and KXNCAAFSPREAD on a 56-game week runs past that — South
    Alabama's event sat beyond the last page, the walk fell through to
    the FIRST-HALF series, and the bet rested on the wrong market while
    Kalshi listed South Alabama −2.5 full game at 54¢ the whole time.
    The game's event is found HERE first; its own ladder is one read."""
    key = f"e:{series}:{status}"
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < ttl_s:
        return hit[1]
    out, cursor, pages, err = [], None, 0, None
    while pages < max_pages:
        params = {"series_ticker": series, "status": status, "limit": 200, "with_nested_markets": "false"}
        if cursor:
            params["cursor"] = cursor
        data, err = _get("/events", params)
        if data is None:
            break
        rows = data.get("events") or []
        out.extend(rows)
        pages += 1
        cursor = data.get("cursor")
        if not cursor or not rows:
            break
    res = {"ok": bool(out), "series": series, "events": out, "pages": pages, "error": err}
    if res["ok"]:
        _CACHE[key] = (time.time(), res)
    return res


def event_markets(event_ticker: str, status: str = "open", ttl_s: float = 30.0) -> dict:
    """Every open market of ONE event — the game's whole ladder, one page.
    {ok, markets, error}."""
    key = f"em:{event_ticker}:{status}"
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < ttl_s:
        return hit[1]
    data, err = _get("/markets", {"event_ticker": event_ticker, "status": status, "limit": 200})
    rows = (data or {}).get("markets") or [] if isinstance(data, dict) else []
    res = {"ok": bool(rows), "event_ticker": event_ticker, "markets": rows, "error": err}
    if res["ok"]:
        _CACHE[key] = (time.time(), res)
    return res



# ───────────────── live game data (milestones + live_data) ─────────────────
# Oct 25 2026, Rob: "How are the big players doing this?? How are they
# reacting fast?" — they price off the GAME, not the book. Kalshi publishes
# the game: `/milestones` lists every game (title, start, Sportradar
# source id) and `/live_data/batch` returns status + scores for up to 100
# milestones a call, public, no auth. The milestone is the GAME, not a
# Kalshi market — a Polymarket-only bet maps to the same milestone, so one
# feed drives the chase on both venues. Polymarket publishes no game state
# (its whole doc index read Oct 25 2026: teams/players/leagues only).
COMPETITION = {"NFL": "Pro Football", "NCAAF": "College Football",
               "NBA": "Pro Basketball (M)", "NHL": "Pro Hockey", "MLB": "Pro Baseball"}
MILESTONE_TYPE = {"NFL": "football_game", "NCAAF": "football_game", "NBA": "basketball_game",
                  "NHL": "hockey_match", "MLB": "baseball_game"}


def milestones(sport: str, min_start_iso: str | None = None, max_pages: int = 4,
               ttl_s: float = 300.0, competition: str | None = None) -> dict:
    """Game milestones for a sport (`category=Sports`, competition + type
    per the docs' examples), paged on the cursor. {ok, milestones, pages,
    error, params}. The competition name is the docs' example spelling —
    an empty answer with no error is read as "that filter spelled wrong",
    and the caller may retry with `competition=''` (type only)."""
    # Oct 25 2026 probe: the docs' competition spellings ("Pro Football",
    # "Pro Basketball (M)", "Pro Hockey") all returned ZERO rows; type-only
    # returned 359 football / 504 basketball / 62 hockey games and
    # `pick_milestone` matched 21 of 21 by team names + start. So the
    # default is NO competition filter; pass one explicitly to probe it.
    comp = competition or ""
    mtype = MILESTONE_TYPE.get(sport.upper())
    key = f"ms:{sport}:{comp}:{min_start_iso}"
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < ttl_s:
        return hit[1]
    out, cursor, pages, err = [], None, 0, None
    while pages < max_pages:
        params: dict = {"limit": 200, "category": "Sports"}
        if comp:
            params["competition"] = comp
        if mtype:
            params["type"] = mtype
        if min_start_iso:
            params["minimum_start_date"] = min_start_iso
        if cursor:
            params["cursor"] = cursor
        data, err = _get("/milestones", params)
        if data is None:
            break
        rows = data.get("milestones") or []
        out.extend(rows)
        pages += 1
        cursor = data.get("cursor")
        if not cursor or not rows:
            break
    res = {"ok": pages > 0 and err is None, "sport": sport, "milestones": out, "pages": pages,
           "error": err, "params": {"competition": comp, "type": mtype, "minimum_start_date": min_start_iso}}
    if res["ok"] and out:
        _CACHE[key] = (time.time(), res)
    return res


def live_data_batch(milestone_ids: list, include_player_stats: bool = False) -> dict:
    """`GET /live_data/batch` for up to 100 milestones — {ok, live_datas:
    {milestone_id: {type, details}}, error, ms}. Never cached: this IS the
    clock the chase reads. `details.live_data_delayed` is the venue's own
    staleness flag (set late, some feeds never set it — a missing flag is
    not freshness)."""
    ids = [str(x) for x in (milestone_ids or []) if x][:100]
    if not ids:
        return {"ok": False, "live_datas": {}, "error": "no ids", "ms": 0}
    t0 = time.monotonic()
    params = [("milestone_ids", i) for i in ids]
    if include_player_stats:
        params.append(("include_player_stats", "true"))
    data, err = _get("/live_data/batch", params)
    ms = round((time.monotonic() - t0) * 1000)
    rows = (data or {}).get("live_datas") or [] if isinstance(data, dict) else []
    by = {}
    for r in rows:
        if isinstance(r, dict) and r.get("milestone_id"):
            by[str(r["milestone_id"])] = {"type": r.get("type"), "details": r.get("details") or {}}
    return {"ok": data is not None, "live_datas": by, "error": err, "ms": ms}


def live_data(milestone_id: str, include_player_stats: bool = False) -> dict:
    """`GET /live_data/milestone/{id}` — one game. {ok, type, details, error, ms}."""
    t0 = time.monotonic()
    params = {"include_player_stats": "true"} if include_player_stats else None
    data, err = _get(f"/live_data/milestone/{milestone_id}", params)
    ms = round((time.monotonic() - t0) * 1000)
    ld = (data or {}).get("live_data") or {} if isinstance(data, dict) else {}
    return {"ok": data is not None, "type": ld.get("type"), "details": ld.get("details") or {},
            "error": err, "ms": ms}


def game_stats(milestone_id: str) -> dict:
    """`GET /live_data/milestone/{id}/game_stats` — play-by-play by period
    (Sportradar-backed; null for a milestone without a Sportradar id).
    {ok, pbp, error, ms}."""
    t0 = time.monotonic()
    data, err = _get(f"/live_data/milestone/{milestone_id}/game_stats")
    ms = round((time.monotonic() - t0) * 1000)
    return {"ok": data is not None, "pbp": (data or {}).get("pbp") if isinstance(data, dict) else None,
            "error": err, "ms": ms}


def _milestone_dt(m: dict):
    v = m.get("start_date")
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def pick_milestone(rows: list, away: str, home: str, start_iso: str | None,
                   max_gap_s: float = 12 * 3600) -> dict | None:
    """PURE. The milestone for this game: both teams named in its title
    (or any `related_event_tickers` text) and its start within `max_gap_s`
    of ours (a series replays one matchup; the nearest start wins). None
    when nothing names both teams inside the window."""
    try:
        want = datetime.fromisoformat(str(start_iso).replace("Z", "+00:00")).astimezone(timezone.utc)
    except (TypeError, ValueError):
        want = None
    best, best_gap = None, None
    for m in rows or []:
        if not isinstance(m, dict) or not m.get("id"):
            continue
        text = " ".join([str(m.get("title") or ""), str(m.get("notification_message") or ""),
                         " ".join(str(t) for t in (m.get("related_event_tickers") or []))])
        if not (team_mentioned(text, away) and team_mentioned(text, home)):
            continue
        gap = 0.0
        if want is not None:
            d = _milestone_dt(m)
            if d is None:
                gap = max_gap_s  # dateless row: eligible, but any dated row beats it
            else:
                gap = abs((d - want).total_seconds())
                if gap > max_gap_s:
                    continue
        if best is None or gap < best_gap:
            best, best_gap = m, gap
    return best


def _walk(obj, path="", out=None, depth=0):
    """Flatten a nested dict into {dotted.path: scalar} for the shape probe."""
    if out is None:
        out = {}
    if depth > 6:
        return out
    if isinstance(obj, dict):
        for k, v in obj.items():
            _walk(v, f"{path}.{k}" if path else str(k), out, depth + 1)
    elif isinstance(obj, list):
        out[path] = f"<list len {len(obj)}>"
        for i, v in enumerate(obj[:3]):
            _walk(v, f"{path}[{i}]", out, depth + 1)
    else:
        out[path] = obj
    return out


def game_state(details: dict) -> dict:
    """PURE read of a live_data `details` object into one shape {status,
    home_score, away_score, period, clock, last_play, last_ts, possession,
    delayed, raw_keys}. Field names MEASURED Oct 25 2026 on live games:
    football (provider statsperform) `away_points/home_points/quarter/clock/
    status(inprogress|closed)/last_play{description,occurence_ts}/
    last_updated_ts/situation{down,yfd,possession_team_id}`; NBA (txodds)
    `away_points/home_points/period/period_remaining_time/last_play(str)/
    possession(home|away)/score_last_updated_ts/live_data_delayed`; NHL
    `away_points/home_points/period/period_remaining_time/last_play(str)/
    play_side`. Anything missing is None, never a guess."""
    d = details if isinstance(details, dict) else {}
    flat = _walk(d)
    low = {k.lower(): (k, v) for k, v in flat.items()}

    def find(*cands):
        for c in cands:
            for lk, (k, v) in low.items():
                if lk == c or lk.endswith("." + c):
                    if v is not None and not (isinstance(v, str) and v.startswith("<list")):
                        return v
        return None

    def num(v):
        try:
            return None if v is None else float(v)
        except (TypeError, ValueError):
            return None

    lp = d.get("last_play")
    last_play = lp.get("description") if isinstance(lp, dict) else (lp if isinstance(lp, str) else None)
    last_ts = None
    if isinstance(lp, dict):
        last_ts = num(lp.get("occurence_ts"))
    for k in ("score_last_updated_ts", "last_event_created_ts", "last_updated_ts"):
        if last_ts is None:
            last_ts = num(d.get(k))
    return {
        "status": find("status", "game_status", "state"),
        "home_score": num(find("home_points", "home_score", "home.score", "home.points", "score.home")),
        "away_score": num(find("away_points", "away_score", "away.score", "away.points", "score.away")),
        "period": find("quarter", "period", "inning", "half"),
        "clock": find("clock", "period_remaining_time", "game_clock", "time_remaining"),
        "last_play": last_play,
        "last_ts": last_ts,
        "possession": find("possession", "situation.possession_team_id", "play_side"),
        "delayed": bool(find("live_data_delayed")),
        "raw_keys": sorted(flat.keys())[:80],
    }


def series_fee(series: str, ttl_s: float = 6 * 3600) -> dict:
    """The series' fee terms off GET /series/{ticker}: {maker: bool, mult:
    float, fee_type, raw_keys, ok}. Field names are read defensively
    (`fee_type` carrying 'maker', `fee_multiplier`, or any key naming
    maker/taker) and the raw fee-ish fields ride along so the first live
    probe can lock the names. Unreadable → maker=True, mult=1 (the
    conservative default) with ok=False."""
    key = f"s:{series}"
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < ttl_s:
        return hit[1]
    data, err = _get(f"/series/{series}")
    sr = (data or {}).get("series") if isinstance(data, dict) else None
    if not isinstance(sr, dict):
        sr = data if isinstance(data, dict) and data.get("ticker") else None
    out = {"ok": False, "maker": True, "maker_ratio": 0.25, "mult": 1.0, "fee_type": None, "raw": {}, "error": err}
    if sr:
        feeish = {k: v for k, v in sr.items() if re.search(r"fee|maker|taker|mult", str(k), re.I)}
        ft = str(sr.get("fee_type") or "").lower()
        ratio = KALSHI_MAKER_RATIO.get(ft, 0.25 if "maker" in ft else (0.0 if ft == "quadratic" else 0.25))
        out.update(ok=True, fee_type=ft or None, raw=feeish, maker=ratio > 0, maker_ratio=ratio,
                   mult=float(sr.get("fee_multiplier") or feeish.get("taker_fee_multiplier") or 1.0) or 1.0)
        _CACHE[key] = (time.time(), out)
    return out


def event_fee(event_ticker: str | None, series_terms: dict | None, ttl_s: float = 6 * 3600) -> dict:
    """The fee terms that ACTUALLY apply to an event's markets: the parent
    series' terms, beaten by the event's own `fee_type_override` /
    `fee_multiplier_override` when set (GET /events/{ticker}, docs: "When
    present, takes precedence over the series-level fee"). Cached 6h; an
    unreadable event keeps the series terms."""
    base = dict(series_terms or {"ok": False, "maker": True, "maker_ratio": 0.25, "mult": 1.0, "fee_type": None})
    if not event_ticker:
        return base
    key = f"ef:{event_ticker}"
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < ttl_s:
        return {**base, **hit[1]}
    data, err = _get(f"/events/{event_ticker}", {"exclude_markets": "true"})
    ev = (data or {}).get("event") if isinstance(data, dict) else None
    ov: dict = {}
    if isinstance(ev, dict):
        ft = ev.get("fee_type_override")
        fm = ev.get("fee_multiplier_override")
        if ft:
            ft = str(ft).lower()
            ratio = KALSHI_MAKER_RATIO.get(ft, 0.25 if "maker" in ft else 0.25)
            ov.update(fee_type=ft, maker=ratio > 0, maker_ratio=ratio, event_override=True)
        if fm is not None:
            try:
                ov["mult"] = float(fm) or 1.0
                ov["event_override"] = True
            except (TypeError, ValueError):
                pass
        _CACHE[key] = (time.time(), ov)
    elif err:
        ov["event_fee_error"] = err
    return {**base, **ov}


def catalog(ttl_s: float = 6 * 3600) -> list:
    """Kalshi's /series catalog (sports first, then everything) as
    [{ticker, title, category}]. Cached."""
    hit = _CACHE.get("catalog")
    if hit and time.time() - hit[0] < ttl_s:
        return hit[1]
    rows: list = []
    for cat in ("Sports", ""):
        params = {"limit": 500}
        if cat:
            params["category"] = cat
        data, _err = _get("/series", params)
        for sr in ((data or {}).get("series") or []):
            if sr.get("ticker"):
                rows.append({"ticker": str(sr["ticker"]).upper(),
                             "title": str(sr.get("title") or ""),
                             "category": str(sr.get("category") or "")})
        if rows:
            break
    if rows:
        _CACHE["catalog"] = (time.time(), rows)
    return rows


def full_game_series(sport: str, mt: str, ticker: str) -> bool:
    """PURE. Is this series the FULL-GAME main line for (sport, mt)? The
    venue names them `KX<SPORT>GAME` / `KX<SPORT>SPREAD` / `KX<SPORT>TOTAL`
    with nothing between the sport and the market word; a period, inning,
    team-total, playoff-series or futures series carries extra letters
    (`KXNCAAF1HSPREAD`, `KXMLBF5TOTAL`, `KXMLBSERIESSPREAD`, `KXMLBTEAMTOTAL`).
    Oct 19 2026: South Alabama −2.5 rested 10 contracts on the FIRST-HALF
    spread at 44¢ because the full-game ladder had no −2.5 rung and the
    walk fell through to the next series carrying 'SPREAD' — a different
    bet read as 8.5¢ cheaper. A wrong market is never a seat; no rung on
    the full-game series = no Kalshi seat, Polymarket takes the bet."""
    sport = (sport or "").upper()
    tags = _SPORT_TAGS.get(sport, (sport,))
    word = "GAME" if mt == "ml" else ("TOTAL" if mt == "total" else "SPREAD")
    return bool(re.fullmatch(r"KX(?:%s)%s" % ("|".join(re.escape(t) for t in tags), word), str(ticker or "").upper()))


def discover_series(sport: str, mt: str) -> list:
    """Candidate series for (sport, ml|spread|total): the named guesses
    first, then every catalog ticker carrying the sport's tag and a
    market-type word in its ticker or title — EVERY candidate filtered to
    the full-game main-line shape (`full_game_series`)."""
    sport = (sport or "").upper()
    if mt == "ml":
        cands = [SERIES_ML[sport]] if sport in SERIES_ML else []
        want = r"GAME|WINNER|MONEYLINE"
    else:
        cands = list((SERIES_LINE.get(sport) or {}).get(mt) or [])
        want = r"TOTAL|POINTS|RUNS|GOALS|OVER" if mt == "total" else r"SPREAD|MARGIN|WINS BY|COVER"
    tags = _SPORT_TAGS.get(sport, (sport,))
    for s in catalog():
        tk, title = s["ticker"], s["title"].upper()
        if any(t in tk for t in tags) and re.search(want, tk + " " + title):
            if "COMBO" in tk or "PARLAY" in tk or "MVE" in tk:
                continue
            if tk not in cands:
                cands.append(tk)
    return [tk for tk in cands if full_game_series(sport, mt, tk)]


# ─────────────────────── matching (pure helpers) ───────────────────────
def team_key(name: str) -> str:
    t = unicodedata.normalize("NFKD", str(name or "")).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9 ]", "", t.lower()).strip()


_STOP = {"the", "of", "at", "and", "st", "state", "university", "college"}


def _tokens(name: str) -> set:
    return {t for t in team_key(name).split() if t and t not in _STOP}


def team_mentioned(text: str, team: str) -> bool:
    """Loose team match: the whole key, or the team's distinctive tokens
    (both 'Nebraska' and 'Nebraska Cornhuskers' find 'Nebraska at Oregon').
    One-token names need the token itself; multi-token names need any
    non-stop token of 4+ letters."""
    tx = " " + team_key(text) + " "
    k = team_key(team)
    if k and f" {k} " in tx:
        return True
    toks = [t for t in _tokens(team) if len(t) >= 4]
    return any(f" {t} " in tx for t in toks)


def _num(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def _market_text(m: dict) -> str:
    return " ".join(str(m.get(k) or "") for k in
                    ("title", "subtitle", "yes_sub_title", "no_sub_title", "rules_primary"))


def event_matches(ev_markets: list, away: str, home: str) -> bool:
    """Both teams named somewhere in the event's market texts."""
    text = " ".join(_market_text(m) for m in ev_markets) + " " + " ".join(
        str(m.get("event_ticker") or "") for m in ev_markets)
    return team_mentioned(text, away) and team_mentioned(text, home)


def group_events(rows: list) -> dict:
    ev: dict = {}
    for m in rows or []:
        et = m.get("event_ticker") or ""
        if et:
            ev.setdefault(et, []).append(m)
    return ev


def _close_dt(m: dict):
    for k in ("expected_expiration_time", "close_time", "expiration_time"):
        v = m.get(k)
        if v:
            try:
                return datetime.fromisoformat(str(v).replace("Z", "+00:00")).astimezone(timezone.utc)
            except ValueError:
                continue
    return None


def pick_event(rows: list, away: str, home: str, start_iso: str | None):
    """The event (ticker, markets) for this game: both teams named; of
    several (a series), the one whose close is nearest after the start."""
    try:
        want = datetime.fromisoformat(str(start_iso).replace("Z", "+00:00")).astimezone(timezone.utc)
    except (TypeError, ValueError):
        want = None
    best, best_gap = None, None
    for et, ms in group_events(rows).items():
        if not event_matches(ms, away, home):
            continue
        gap = 0.0
        if want is not None:
            cds = [c for c in (_close_dt(m) for m in ms) if c]
            if cds:
                gap = min(abs((c - want).total_seconds()) for c in cds)
        if best is None or gap < best_gap:
            best, best_gap = (et, ms), gap
    return best


def _event_text(e: dict) -> str:
    return " ".join(str(e.get(k) or "") for k in ("title", "sub_title", "subtitle", "event_ticker"))


def _event_dt(e: dict):
    for k in ("strike_date", "expected_expiration_time", "close_time"):
        v = e.get(k)
        if v:
            try:
                return datetime.fromisoformat(str(v).replace("Z", "+00:00")).astimezone(timezone.utc)
            except ValueError:
                continue
    return None


def pick_event_row(events_rows: list, away: str, home: str, start_iso: str | None) -> dict | None:
    """PURE. The EVENT row (from `events`) for this game: both teams named
    in its title/sub-title ('South Alabama vs Marshall'); of several (a
    series, a rematch) the one whose strike date is nearest the start.
    None when no row names both teams."""
    try:
        want = datetime.fromisoformat(str(start_iso).replace("Z", "+00:00")).astimezone(timezone.utc)
    except (TypeError, ValueError):
        want = None
    best, best_gap = None, None
    for e in events_rows or []:
        if not isinstance(e, dict) or not e.get("event_ticker"):
            continue
        text = _event_text(e)
        if not (team_mentioned(text, away) and team_mentioned(text, home)):
            continue
        gap = 0.0
        if want is not None:
            d = _event_dt(e)
            if d is not None:
                gap = abs((d - want).total_seconds())
        if best is None or gap < best_gap:
            best, best_gap = e, gap
    return best


def classify(m: dict, away: str, home: str) -> dict | None:
    """One Kalshi market → {mt, team_side ('home'|'away'|None), line,
    yes_is: 'team'|'over'|'cover', text}. Heuristic over the title and
    sub-titles, strike fields first when present:
      total  : 'Over 45.5' / strike 45.5 → yes = over, line = strike
      spread : 'X wins by over 6.5' / 'X -6.5' → yes = that team covers,
               line = the YES team's own line (negative)
      ml     : the YES team named with no number → yes = team
    None when nothing readable."""
    text = _market_text(m)
    ys = str(m.get("yes_sub_title") or "")
    strike = _num(m.get("floor_strike"))
    if strike is None:
        strike = _num(m.get("cap_strike"))
    low = text.lower()
    team_side = None
    for side, nm in (("home", home), ("away", away)):
        if team_mentioned(ys or text, nm):
            team_side = side if team_side is None else team_side
    nums = [float(x) for x in re.findall(r"(?<![\w.])(\d{1,3}(?:\.5)?)(?![\w.])", text)]
    half = [x for x in nums if abs(x - round(x)) > 0.1]
    # SPREAD FIRST: 'wins by over 7.5' carries the word 'over' too
    if re.search(r"wins? by|spread|cover|margin|by (over|more than|at least)", low) and (strike is not None or half or nums):
        mag = strike if strike is not None else (half[0] if half else nums[0])
        # 'wins by over 6.5' = that team −6.5; Kalshi's suffix convention
        # (N = margin + 0.5) shows up as a whole number: 'by over 6' → −6.5
        if abs(mag - round(mag)) < 0.1 and re.search(r"by (over|more than)", low):
            mag = mag + 0.5
        return {"mt": "spread", "team_side": team_side, "line": -abs(mag), "yes_is": "cover", "text": text[:140]}
    if re.search(r"\b(over|under|total|o/u|combined)\b", low) and (strike is not None or half):
        line = strike if strike is not None else half[0]
        return {"mt": "total", "team_side": None, "line": line, "yes_is": "over", "text": text[:140]}
    if team_side and not half:
        return {"mt": "ml", "team_side": team_side, "line": None, "yes_is": "team", "text": text[:140]}
    return None


def book_c(m: dict) -> dict:
    """Top of book in cents off the market row's dollar strings."""
    def c(v):
        x = _num(v)
        return None if x is None else round(x * 100.0, 2)
    return {"yes_bid": c(m.get("yes_bid_dollars")), "yes_ask": c(m.get("yes_ask_dollars")),
            "no_bid": c(m.get("no_bid_dollars")), "no_ask": c(m.get("no_ask_dollars")),
            "last": c(m.get("last_price_dollars")),
            "volume": m.get("volume_fp") or m.get("volume_24h_fp")}


def our_side_quote(m: dict, buy_yes: bool) -> dict:
    """(bid, ask) in cents for the side we would BUY. Buying NO is the
    inverted YES book; Kalshi also quotes NO directly when present."""
    b = book_c(m)
    if buy_yes:
        return {"bid": b["yes_bid"], "ask": b["yes_ask"]}
    nb = b["no_bid"] if b["no_bid"] is not None else (100.0 - b["yes_ask"] if b["yes_ask"] is not None else None)
    na = b["no_ask"] if b["no_ask"] is not None else (100.0 - b["yes_bid"] if b["yes_bid"] is not None else None)
    return {"bid": nb, "ask": na}


def rung_for(item: dict, ev_markets: list, away: str, home: str) -> dict:
    """The Kalshi contract for a slip item {market_type ml|spread|total,
    side home|away|over|under, line}: exact rung, else the nearest MORE
    favorable (the slip's own rule — never a worse line). Returns
    {ok, ticker, buy_yes, line, why, ladder:[...]} or {ok:False, why}."""
    mt, side = item.get("market_type"), item.get("side")
    want = _num(item.get("line"))
    ladder = []
    for m in ev_markets:
        cl = classify(m, away, home)
        if not cl or cl["mt"] != mt:
            continue
        if mt == "ml":
            if cl["team_side"] == side:
                return {"ok": True, "ticker": m.get("ticker"), "buy_yes": True, "line": None,
                        "why": "exact", "market": m}
            continue
        if mt == "total":
            # YES = over at line; under = NO of the same contract
            ladder.append((cl["line"], m, side == "over"))
        else:
            # YES = team_side covers −|line|; the other side's +|line| is NO
            if cl["team_side"] is None:
                continue
            own = cl["line"] if cl["team_side"] == side else -cl["line"]
            ladder.append((own, m, cl["team_side"] == side))
    if mt == "ml":
        return {"ok": False, "why": "no ML market names that team",
                "ladder": [classify(m, away, home) for m in ev_markets][:6]}
    if want is None:
        return {"ok": False, "why": "item has no line"}
    if not ladder:
        return {"ok": False, "why": f"no {mt} markets read on the event",
                "ladder": [classify(m, away, home) for m in ev_markets][:6]}
    exact = [r for r in ladder if abs(r[0] - want) < 0.01]
    if exact:
        ln, m, by = exact[0]
        return {"ok": True, "ticker": m.get("ticker"), "buy_yes": by, "line": ln, "why": "exact", "market": m,
                "ladder": sorted({r[0] for r in ladder})}
    better = (lambda ln: ln > want) if (mt == "spread" or side == "under") else (lambda ln: ln < want)
    fav = sorted([r for r in ladder if better(r[0])], key=lambda r: abs(r[0] - want))
    if not fav:
        return {"ok": False, "why": f"no rung at {want:g} or better", "ladder": sorted({r[0] for r in ladder})}
    ln, m, by = fav[0]
    return {"ok": True, "ticker": m.get("ticker"), "buy_yes": by, "line": ln, "why": "favorable", "market": m,
            "ladder": sorted({r[0] for r in ladder})}


def resolve(sport: str, away: str, home: str, start_iso: str | None, item: dict) -> dict:
    """One slip item → its Kalshi contract and our-side quote, plus the
    series fee terms. Walks the candidate (full-game) series for the
    market type. EVENT FIRST: the series' events list (one row per game)
    names the game, then that event's own ladder is read — the series-
    wide markets walk is the fallback only when the events read answers
    nothing for the series (it pages and can truncate; see `events`)."""
    mt = item.get("market_type")
    tried, found = [], None
    for series in discover_series(sport, mt):
        ev = None
        src = "events"
        evs = events(series)
        row = pick_event_row(evs.get("events") or [], away, home, start_iso) if evs.get("events") else None
        if row is not None:
            em = event_markets(row["event_ticker"])
            if em.get("markets"):
                ev = (row["event_ticker"], em["markets"])
            tried.append({"series": series, "src": src, "events": len(evs.get("events") or []),
                          "event": row["event_ticker"], "n": len(em.get("markets") or []), "err": em.get("error")})
        if ev is None:
            src = "markets_walk"
            res = markets(series)
            tried.append({"series": series, "src": src, "events": len(evs.get("events") or []),
                          "events_err": evs.get("error"), "n": len(res.get("markets") or []),
                          "pages": res.get("pages"), "err": res.get("error")})
            if not res.get("markets"):
                continue
            ev = pick_event(res["markets"], away, home, start_iso)
            if not ev:
                continue
        r = rung_for(item, ev[1], away, home)
        found = {"series": series, "event_ticker": ev[0], "event_src": src, "n_event_markets": len(ev[1]), **r}
        if r.get("ok"):
            break
    if not found:
        return {"ok": False, "why": "no series lists this game", "tried": tried}
    if found.get("ok"):
        m = found.pop("market")
        found["quote"] = our_side_quote(m, found["buy_yes"])
        found["book"] = book_c(m)
        found["fee"] = event_fee(found.get("event_ticker"), series_fee(found["series"]))
        found["raw_sample"] = {k: m.get(k) for k in
                               ("ticker", "title", "subtitle", "yes_sub_title", "no_sub_title",
                                "floor_strike", "cap_strike", "strike_type", "close_time",
                                "expected_expiration_time", "status") if k in m}
    found["tried"] = tried
    return found


# ───────────────────────── authed (I/O, the write path) ─────────────────────────
# The trading API signs ts + METHOD + path (the path INCLUDING /trade-api/v2,
# never the query string) with RSA-PSS-SHA256 — the same scheme
# app._kalshi_sign/_kalshi_authed_get have used for the portfolio reads
# since July 2026. Creds come from app._kalshi_creds (env, else the box's
# kalshi_state row). EVERY write is behind the caller's flag — this module
# never decides to place anything; it places what it is told.

def _creds():
    try:
        import app as _app  # noqa: WPS433 — the site module owns the creds
        kid, pem, kid_src, pem_src = _app._kalshi_creds()
        return kid, pem, f"{kid_src}/{pem_src}"
    except Exception as e:  # noqa: BLE001
        return None, None, f"creds unavailable: {type(e).__name__}"


def _sign(pem: str, msg: str) -> str:
    """RSA → RSA-PSS-SHA256; Ed25519 (Kalshi's recommended key type) → raw
    Ed25519. The key type picks the algorithm, exactly as app._kalshi_sign."""
    import base64
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519, padding
    key = serialization.load_pem_private_key(pem.replace("\\n", "\n").encode(), password=None)
    if isinstance(key, ed25519.Ed25519PrivateKey):
        sig = key.sign(msg.encode())
    else:
        sig = key.sign(msg.encode(),
                       padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
                       hashes.SHA256())
    return base64.b64encode(sig).decode()


def signed_request(method: str, path: str, body: dict | None = None, params: dict | None = None,
                   timeout: float = 12.0) -> dict:
    """One signed call. Returns {ok, status, data|error, base, body_sent, tries}.
    ⚠ THE FALLBACK HOSTS ARE LEGACY (Oct 18 2026, the first live probe): the
    elections host answered the balance read 200, the create got an
    unlisted status there, the client fell through to external-api, and
    external-api's 401 "API has been moved" was returned as the verdict —
    the real answer was lost. Now: ANY HTTP reply from a host is final
    (the next host only on a connection failure), every host's reply is
    kept in `tries`, and a 4xx with a JSON error body is returned as data
    so the caller can read Kalshi's own reason."""
    from urllib.parse import urlparse
    kid, pem, src = _creds()
    if not (kid and pem):
        return {"ok": False, "configured": False, "error": f"no creds ({src})"}
    method = method.upper()
    tries: list = []
    for base in BASES:
        ts = str(int(time.time() * 1000))
        try:
            sig = _sign(pem, ts + method + urlparse(base).path + path)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "configured": True, "error": f"key load/sign: {type(e).__name__}: {str(e)[:120]}"}
        headers = {**_HDRS, "Content-Type": "application/json", "KALSHI-ACCESS-KEY": kid,
                   "KALSHI-ACCESS-TIMESTAMP": ts, "KALSHI-ACCESS-SIGNATURE": sig}
        try:
            r = _http.request(method, base + path, params=params or {}, json=body if body is not None else None,
                              headers=headers, timeout=timeout, allow_redirects=False)
        except Exception as e:  # noqa: BLE001
            tries.append({"base": base, "exc": f"{type(e).__name__}: {str(e)[:120]}"})
            continue                                   # connection failure → next host
        try:
            data = r.json() if (r.text or "").strip() else {}
        except ValueError:
            data = {"raw": (r.text or "")[:300]}
        tries.append({"base": base, "status": r.status_code, "body": str(data)[:300],
                      "location": r.headers.get("Location")})
        out = {"ok": 200 <= r.status_code < 300, "configured": True, "status": r.status_code,
               "data": data, "base": base, "creds": src, "tries": tries}
        if body is not None:
            out["body_sent"] = body
        if not out["ok"]:
            out["error"] = f"HTTP {r.status_code} @ {base}: {str(data)[:300]}"
        return out                                     # an HTTP answer is THE answer
    return {"ok": False, "configured": True, "error": "no host answered", "tries": tries}


def balance() -> dict:
    return signed_request("GET", "/portfolio/balance")


def balance_c(res: dict | None = None):
    """Cents, off either field shape the venue has used (`balance` int cents,
    `balance_dollars` string)."""
    res = res or balance()
    d = (res or {}).get("data") or {}
    if d.get("balance_dollars") is not None:
        try:
            return round(float(d["balance_dollars"]) * 100.0, 2)
        except (TypeError, ValueError):
            return None
    if d.get("balance") is not None:
        try:
            return float(d["balance"])
        except (TypeError, ValueError):
            return None
    return None


# ── EXCHANGE SHARDS (Oct 19 2026 — the CLE ML that answered 404
# `insufficient_shard_balance` / "Exchange user not found") ──────────────
# Kalshi runs several matching engines ("exchange indexes"). Per the venue's
# own doc (docs.kalshi.com/getting_started/exchange_sharding.md): shard 3 =
# Sports tagged Tennis / Baseball / Basketball, shard 1 = Exotics (combos),
# shard 2 = Crypto + Commodities, shard 0 = everything else (NFL, NCAAF, NHL
# — every bet this lane had placed before Oct 19 2026). Collateral lives
# PER SHARD: an API order on a shard with no balance is refused, and ONLY
# app/web orders auto-move money. The market's `exchange_index` is the
# authoritative routing field; the prefix table below is the fallback when
# a market read fails. A shard the account has never held money on reads
# "Exchange user not found" — the first transfer provisions it.
SHARD_PREFIX = (("KXMLB", 3), ("KXNBA", 3), ("KXWNBA", 3), ("KXNCAAB", 3), ("KXNCAAMB", 3),
                ("KXNCAAWB", 3), ("KXATP", 3), ("KXWTA", 3), ("KXTENNIS", 3))
TRANSFER_PATH = "/portfolio/intra_exchange_instance_transfer"


def shard_for_prefix(ticker: str | None) -> int:
    t = str(ticker or "").upper()
    for pre, sh in SHARD_PREFIX:
        if t.startswith(pre):
            return sh
    return 0


def market_shard(ticker: str, ttl_s: float = 24 * 3600) -> dict:
    """{shard, src: 'market'|'prefix', error} — the market's `exchange_index`
    off GET /markets/{ticker} (cached a day; a live market never migrates per
    the venue), else the prefix table."""
    key = f"shard:{ticker}"
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < ttl_s:
        return hit[1]
    data, err = _get(f"/markets/{ticker}")
    m = (data or {}).get("market") if isinstance(data, dict) else None
    if not isinstance(m, dict) and isinstance(data, dict) and data.get("ticker"):
        m = data
    out: dict
    if isinstance(m, dict) and m.get("exchange_index") is not None:
        try:
            out = {"shard": int(m["exchange_index"]), "src": "market", "error": None}
        except (TypeError, ValueError):
            out = {"shard": shard_for_prefix(ticker), "src": "prefix",
                   "error": f"bad exchange_index {m.get('exchange_index')!r}"}
    else:
        out = {"shard": shard_for_prefix(ticker), "src": "prefix",
               "error": err or ("no exchange_index on the market" if m else "market unreadable")}
    if out["src"] == "market":
        _CACHE[key] = (time.time(), out)
    return out


def shard_balances(res: dict | None = None) -> dict | None:
    """{shard: cents} off the balance read's `balance_breakdown` (dollar
    strings). None when the read failed or carries no breakdown — never
    'nothing anywhere'."""
    res = res or balance()
    if not res or not res.get("ok"):
        return None
    rows = ((res.get("data") or {}).get("balance_breakdown"))
    if not isinstance(rows, list):
        return None
    out = {}
    for r in rows:
        try:
            out[int(r.get("exchange_index"))] = round(float(r.get("balance") or 0.0) * 100.0, 2)
        except (TypeError, ValueError, AttributeError):
            continue
    return out


def shard_fund_plan(balances: dict | None, shard: int, need_c: float, round_c: float = 500.0,
                    keep_c: float = 0.0) -> dict:
    """PURE. Does `shard` hold `need_c` cents; if not, which shard moves how
    much? Moves round UP to `round_c` (a $5 grain — one transfer funds the
    next few bets too) when a donor can cover it, else the exact shortfall;
    a donor keeps `keep_c`. {ok, move_c, from_shard, have_c, short_c, why}."""
    if balances is None:
        return {"ok": False, "move_c": 0, "from_shard": None, "have_c": None, "short_c": None,
                "why": "balance breakdown unreadable"}
    have = float(balances.get(int(shard), 0.0) or 0.0)
    need_c = float(need_c)
    if have >= need_c:
        return {"ok": True, "move_c": 0, "from_shard": None, "have_c": have, "short_c": 0.0, "why": "funded"}
    short = need_c - have
    move = math.ceil(short / round_c) * round_c if round_c > 0 else math.ceil(short)
    donors = sorted(((float(b or 0.0), int(s)) for s, b in balances.items() if int(s) != int(shard)),
                    reverse=True)
    for b, s in donors:
        if b - keep_c >= move:
            return {"ok": True, "move_c": float(move), "from_shard": s, "have_c": have, "short_c": short,
                    "why": "move"}
    exact = float(math.ceil(short))
    for b, s in donors:
        if b - keep_c >= exact:
            return {"ok": True, "move_c": exact, "from_shard": s, "have_c": have, "short_c": short,
                    "why": "move_exact"}
    return {"ok": False, "move_c": 0, "from_shard": None, "have_c": have, "short_c": short,
            "why": f"no shard can cover the ${short / 100.0:.2f} shortfall on shard {int(shard)} "
                   f"(balances: " + ", ".join(f"{s}=${b / 100.0:.2f}" for b, s in donors) + ")"}


def shard_transfer(src_shard: int, dst_shard: int, cents: float, client_transfer_id: str | None = None) -> dict:
    """POST intra_exchange_instance_transfer — primary account, event
    contracts, `amount` in CENTICENTS (the venue's unit: $1 = 10,000).
    Async on the venue side; the caller re-reads the balance."""
    import uuid
    body = {"source": "event_contract", "destination": "event_contract",
            "amount": int(round(float(cents) * 100.0)),
            "source_exchange_shard": int(src_shard), "destination_exchange_shard": int(dst_shard),
            "client_transfer_id": client_transfer_id or str(uuid.uuid4())}
    res = signed_request("POST", TRANSFER_PATH, body=body)
    res["transfer_id"] = ((res.get("data") or {}).get("transfer_id")) if res.get("ok") else None
    return res


def ensure_shard_funded(ticker: str, need_c: float, wait_s: float = 8.0, round_c: float = 500.0) -> dict:
    """Before a Kalshi create: the market's shard holds `need_c` cents, or
    the shortfall (rounded up to $5) is moved there from the richest other
    shard and the shard is re-read until it shows. {ok, shard, shard_src,
    have_c, moved_c, from_shard, transfer, after_c, wait_s, error}."""
    ms = market_shard(ticker)
    out: dict = {"ok": False, "ticker": ticker, "shard": ms.get("shard"), "shard_src": ms.get("src"),
                 "need_c": float(need_c), "moved_c": 0.0}
    if ms.get("shard") is None:
        out["error"] = ms.get("error") or "shard unknown"
        return out
    shard = int(ms["shard"])
    bal = balance()
    bb = shard_balances(bal)
    if bb is None:
        out["error"] = f"balance breakdown unreadable: {bal.get('error') or bal.get('status')}"
        return out
    plan = shard_fund_plan(bb, shard, need_c, round_c=round_c)
    out.update(have_c=plan.get("have_c"), plan=plan, balances=bb)
    if not plan.get("ok"):
        out["error"] = plan.get("why")
        return out
    if not plan.get("move_c"):
        out["ok"] = True
        return out
    tr = shard_transfer(plan["from_shard"], shard, plan["move_c"])
    out.update(moved_c=float(plan["move_c"]), from_shard=plan["from_shard"],
               transfer={"ok": tr.get("ok"), "status": tr.get("status"), "transfer_id": tr.get("transfer_id"),
                         "error": tr.get("error"), "body": tr.get("body_sent")})
    if not tr.get("ok"):
        out["error"] = (f"transfer shard {plan['from_shard']}→{shard} ${plan['move_c'] / 100.0:.2f} failed: "
                        f"{tr.get('error')}")[:220]
        return out
    t0 = time.monotonic()
    after = None
    while True:
        time.sleep(1.0)                              # the balance bucket is 1 call/s
        after = (shard_balances(balance()) or {}).get(shard)
        if after is not None and after >= float(need_c):
            out.update(ok=True, after_c=after, wait_s=round(time.monotonic() - t0, 1))
            return out
        if time.monotonic() - t0 > wait_s:
            out.update(after_c=after, wait_s=round(time.monotonic() - t0, 1),
                       error=f"transfer sent (id {tr.get('transfer_id')}) but shard {shard} reads "
                             f"{after if after is not None else 'unreadable'}¢ after {wait_s:g}s — try again")
            return out


def my_orders(ticker: str | None = None, status: str | None = "resting") -> dict:
    p = {}
    if ticker:
        p["ticker"] = ticker
    if status:
        p["status"] = status
    return signed_request("GET", "/portfolio/orders", params=p)


def my_positions(ticker: str | None = None) -> dict:
    return signed_request("GET", "/portfolio/positions", params={"ticker": ticker} if ticker else {})


def my_fills(ticker: str | None = None) -> dict:
    return signed_request("GET", "/portfolio/fills", params={"ticker": ticker} if ticker else {})


def get_order(order_id: str) -> dict:
    return signed_request("GET", f"/portfolio/orders/{order_id}")


# THE V2 ORDER API (read off docs.kalshi.com/api-reference/orders/create-order-v2
# on Oct 18 2026, after the live probe got `410 deprecated_v1_order_endpoint`
# on the old /portfolio/orders path): POST /portfolio/events/orders. The
# side is the BOOK side of the YES leg — `bid` = buy YES, `ask` = sell YES
# — and everything is quoted from the YES side, so BUYING NO at p is an
# `ask` at 1 − p (the venue's own words: "Selling YES is economically
# equivalent to buying NO at 1 - price"). count = fixed-point contracts
# ("1.00"), price = fixed-point dollars ("0.6100"), `time_in_force` ∈
# fill_or_kill | good_till_canceled | immediate_or_cancel (GTC +
# `expiration_time` unix SECONDS = an expiring order; IOC may not carry
# one), `self_trade_prevention_type` is REQUIRED (taker_at_cross = the NEW
# order dies if it would hit our own resting order — the safe default for
# a hand bet; `maker` kills the resting one instead). 201 → {order_id,
# client_order_id, fill_count, remaining_count, average_fill_price?,
# average_fee_paid?, ts_ms} — NO status field; the orders LIST says
# whether it rests. Cancel = DELETE /portfolio/events/orders/{id} →
# {order_id, client_order_id, reduced_by}. Amend (Phase 3's chase) =
# POST /portfolio/events/orders/{id}/amend, count = filled + desired
# remaining (the TOTAL, the same semantics as Polymarket's modify).
ORDERS_PATH = "/portfolio/events/orders"
CREATE_SCHEMA = "v2_events"


def yes_price_c(buy_yes: bool, price_c) -> int:
    """Our-side cents → the YES-canonical cents the V2 body wants."""
    pc = max(1, min(99, int(round(float(price_c)))))
    return pc if buy_yes else 100 - pc


def order_body(ticker: str, buy_yes: bool, price_c, count: int, tif: str = "good_till_canceled",
               post_only: bool = True, client_order_id: str | None = None,
               expiration_time=None, stp: str = "taker_at_cross") -> dict:
    """The ONE create body (V2). `expiration_time` = unix seconds or an ISO
    string; only sent with good_till_canceled."""
    import uuid
    body = {"ticker": ticker,
            "client_order_id": client_order_id or f"kh-{uuid.uuid4().hex[:20]}",
            "side": "bid" if buy_yes else "ask",
            "count": f"{int(count)}.00",
            "price": f"{yes_price_c(buy_yes, price_c) / 100.0:.4f}",
            "time_in_force": tif,
            "self_trade_prevention_type": stp,
            "post_only": bool(post_only)}
    if expiration_time and tif == "good_till_canceled":
        if isinstance(expiration_time, str):
            try:
                expiration_time = int(datetime.fromisoformat(expiration_time.replace("Z", "+00:00")).timestamp())
            except Exception:  # noqa: BLE001
                expiration_time = None
        if expiration_time:
            body["expiration_time"] = int(expiration_time)
    return body


def order_bodies(ticker: str, buy_yes: bool, price_c, count: int, tif: str = "good_till_canceled",
                 post_only: bool = True, client_order_id: str | None = None,
                 expiration_time=None, action: str = "buy") -> list[tuple[str, dict]]:
    """Kept for the selftest + callers: one shape now that the venue has
    answered which it takes."""
    return [(CREATE_SCHEMA, order_body(ticker, buy_yes, price_c, count, tif, post_only, client_order_id,
                                       expiration_time))]


def create_order(ticker: str, buy_yes: bool, price_c, count: int, tif: str = "good_till_canceled",
                 post_only: bool = True, client_order_id: str | None = None,
                 expiration_time=None, action: str = "buy") -> dict:
    """Place ONE order on the V2 path. post-only GTC by default (a resting
    hand bet); tif='immediate_or_cancel' + post_only=False is the take.
    Returns the venue's answer plus `order_id`, `fill_count`,
    `remaining_count` and an inferred `order_status`
    (resting / filled / partial / none)."""
    body = order_body(ticker, buy_yes, price_c, count, tif, post_only, client_order_id, expiration_time)
    res = signed_request("POST", ORDERS_PATH, body=body)
    res["schema"] = CREATE_SCHEMA
    res["attempts"] = [{"schema": CREATE_SCHEMA, "status": res.get("status"), "error": res.get("error"),
                        "base": res.get("base"), "tries": res.get("tries"), "body": body}]
    d = res.get("data") or {}
    if res.get("ok"):
        o = d.get("order") if isinstance(d.get("order"), dict) else d
        res["order"] = o
        res["order_id"] = o.get("order_id") or o.get("id")
        res["fill_count"] = _num(o.get("fill_count"))
        res["remaining_count"] = _num(o.get("remaining_count"))
        fc, rc = res["fill_count"] or 0.0, res["remaining_count"]
        res["order_status"] = ("resting" if (rc or 0) > 0 and fc == 0 else
                               "partial" if (rc or 0) > 0 and fc > 0 else
                               "filled" if fc > 0 else "none")
    return res


def cancel_order(order_id: str, ticker: str | None = None) -> dict:
    """DELETE on the V2 path → {order_id, client_order_id, reduced_by}.
    The venue auto-routes the cancel to its exchange shard ONLY when
    `market_ticker` rides along ("an order_id alone cannot identify the
    exchange shard") — always pass the ticker when you have it."""
    params = {"market_ticker": ticker} if ticker else None
    return signed_request("DELETE", f"{ORDERS_PATH}/{order_id}", params=params)


def amend_body(ticker: str, buy_yes: bool, price_c, total_count: int) -> dict:
    """The V2 amend body. ⚠ `ticker` and `side` are REQUIRED (AmendOrderV2Request,
    Oct 18 2026: a price+count body answered 400 on every chase move the
    first live hour) — `side` is the YES leg's book side exactly as the
    create: bid = buy YES, ask = buy NO at 1 − p; price YES-canonical;
    `count` is the TOTAL (filled + desired remaining)."""
    return {"ticker": ticker,
            "side": "bid" if buy_yes else "ask",
            "price": f"{yes_price_c(buy_yes, price_c) / 100.0:.4f}",
            "count": f"{int(total_count)}.00"}


def amend_order(order_id: str, ticker: str, buy_yes: bool, price_c, total_count: int) -> dict:
    """POST .../{id}/amend — Phase 3's in-place chase. See `amend_body`."""
    return signed_request("POST", f"{ORDERS_PATH}/{order_id}/amend",
                          body=amend_body(ticker, buy_yes, price_c, total_count))


def order_resting(order_id: str, ticker: str | None = None) -> dict:
    """Is this order on the book right now? Reads the orders list (the
    truthful read, never the create's echo). {ok, resting, order, error}."""
    res = my_orders(ticker=ticker, status="resting")
    if not res.get("ok"):
        return {"ok": False, "resting": None, "error": res.get("error")}
    for o in ((res.get("data") or {}).get("orders") or []):
        if (o.get("order_id") or o.get("id")) == order_id:
            return {"ok": True, "resting": True, "order": o}
    return {"ok": True, "resting": False}


def _best_bid_c(m: dict, buy_yes: bool):
    b = book_c(m)
    v = b.get("yes_bid") if buy_yes else b.get("no_bid")
    return v


def probe(place: bool = False, ticker: str | None = None, contracts: int = 1, off_c: int = 10,
          cancel: bool = True, log_fn=None) -> dict:
    """THE PROOF (Rob, Oct 18 2026: "I'll put money in Kalshi if you can
    prove this thing works"). Steps, each recorded: creds → balance →
    resting orders read → pick a liquid market (or `ticker`) → book →
    [place=True] ONE post-only buy for `contracts` at (our-side best bid −
    off_c¢, floored at 1¢ — never near the touch, never a fill) → verify on
    the orders list → cancel → verify gone. An EMPTY account proves scope
    too: a key without trade permission answers 401/403 at the create; a
    funded-enough key rests the order; a broke key answers 400
    insufficient balance — all three are recorded as `verdict`."""
    out: dict = {"kalshi_probe": True, "at": datetime.now(timezone.utc).isoformat(), "place": bool(place),
                 "steps": []}

    def step(name, **kw):
        kw["step"] = name
        out["steps"].append(kw)
        if log_fn:
            try:
                log_fn(f"kalshi_probe {name}: {kw}")
            except Exception:  # noqa: BLE001
                pass

    kid, pem, src = _creds()
    out["creds"] = {"configured": bool(kid and pem), "src": src}
    if not (kid and pem):
        out["verdict"] = "no_creds"
        return out
    bal = balance()
    out["balance"] = {"ok": bal.get("ok"), "status": bal.get("status"), "cents": balance_c(bal),
                      "error": bal.get("error"), "base": bal.get("base")}
    step("balance", **out["balance"])
    if not bal.get("ok"):
        out["verdict"] = "read_denied" if bal.get("status") in (401, 403) else "read_failed"
        return out
    mo = my_orders(status="resting")
    out["resting_before"] = (len((mo.get("data") or {}).get("orders") or []) if mo.get("ok") else None)
    step("orders_read", ok=mo.get("ok"), status=mo.get("status"), n=out["resting_before"], error=mo.get("error"))
    # the market
    m = None
    if ticker:
        res, err = _get(f"/markets/{ticker}")
        m = (res or {}).get("market") if res else None
        step("market", ticker=ticker, found=bool(m), error=err)
    else:
        for series in ("KXNHLGAME", "KXNBAGAME", "KXNCAAFGAME", "KXNFLGAME", "KXMLBGAME"):
            res = markets(series)
            cands = [x for x in (res.get("markets") or []) if book_c(x).get("yes_bid") and book_c(x).get("yes_ask")
                     and 10 <= (book_c(x).get("yes_bid") or 0) <= 90]
            cands.sort(key=lambda x: -(float(x.get("volume_fp") or x.get("volume") or 0)))
            if cands:
                m = cands[0]
                step("market", series=series, ticker=m.get("ticker"), title=m.get("title"), book=book_c(m))
                break
        if m is None:
            step("market", found=False)
    if m is None:
        out["verdict"] = "no_market"
        return out
    out["market"] = {"ticker": m.get("ticker"), "title": m.get("title"), "book": book_c(m),
                     "close_time": m.get("close_time"), "status": m.get("status")}
    if not place:
        out["verdict"] = "read_only_ok"
        return out
    bid = _best_bid_c(m, True)
    px = max(1, int(round((bid or 20) - off_c)))
    out["order_plan"] = {"ticker": m.get("ticker"), "buy_yes": True, "price_c": px, "contracts": int(contracts),
                         "post_only": True, "tif": "good_till_canceled", "touch_bid_c": bid}
    cr = create_order(m.get("ticker"), True, px, int(contracts))
    out["create"] = {"ok": cr.get("ok"), "status": cr.get("status"), "schema": cr.get("schema"),
                     "base": cr.get("base"), "order_id": cr.get("order_id"), "order_status": cr.get("order_status"),
                     "fill_count": cr.get("fill_count"), "remaining_count": cr.get("remaining_count"),
                     "error": cr.get("error"), "attempts": cr.get("attempts"), "tries": cr.get("tries"),
                     "data": (cr.get("data") if not cr.get("ok") else None), "body_sent": cr.get("body_sent")}
    step("create", **{k: v for k, v in out["create"].items() if k not in ("data", "body_sent")})
    if not cr.get("ok"):
        st = cr.get("status")
        msg = str(cr.get("data") or cr.get("error") or "").lower()
        if st in (401, 403) and (cr.get("base") or "").startswith(BASES[0]):
            out["verdict"] = "write_denied"           # the PRIMARY host refused the key
        elif st in (401, 403):
            out["verdict"] = "legacy_host_answered"   # never a verdict about the key
        elif "balance" in msg or "insufficient" in msg or "funds" in msg:
            out["verdict"] = "write_scope_ok_no_funds"  # it tried to place: trade scope proven, account empty
        else:
            out["verdict"] = "create_rejected"
        return out
    oid = cr.get("order_id")
    time.sleep(1.0)
    v = order_resting(oid, m.get("ticker")) if oid else {"ok": False, "resting": None, "error": "no order id"}
    out["verify"] = v
    step("verify", ok=v.get("ok"), resting=v.get("resting"), error=v.get("error"))
    if not cancel:
        out["verdict"] = "placed_left_resting"
        return out
    cx = cancel_order(oid, m.get("ticker")) if oid else {"ok": False, "error": "no order id"}
    out["cancel"] = {"ok": cx.get("ok"), "status": cx.get("status"), "error": cx.get("error"),
                     "data": cx.get("data") if cx.get("ok") else (cx.get("data") or cx.get("error"))}
    step("cancel", ok=cx.get("ok"), status=cx.get("status"), error=cx.get("error"))
    time.sleep(1.0)
    v2 = order_resting(oid, m.get("ticker")) if oid else {"ok": False, "resting": None}
    out["verify_after_cancel"] = v2
    step("verify_after_cancel", ok=v2.get("ok"), resting=v2.get("resting"), error=v2.get("error"))
    if cr.get("ok") and v.get("resting") and cx.get("ok") and v2.get("resting") is False:
        out["verdict"] = "write_ok"                   # rested, verified, cancelled, verified gone
    elif cr.get("ok") and cx.get("ok"):
        out["verdict"] = "write_ok_unverified"        # placed + cancelled, but a list read failed
    else:
        out["verdict"] = "cancel_failed" if not cx.get("ok") else "unverified"
    return out


# ───────────────── the hand lane's reads (Phase 2, Oct 18 2026) ─────────────────
# Everything the box's handbets lane needs from Kalshi to carry a resting
# hand bet: the order book shaped like a Polymarket `_pmm_book` dict (so
# every pure hand-bet rule — seat, thin touch, chase, start drop, take —
# runs unchanged on either venue), the resting orders and positions
# parsed to one shape, and a small pace so the box never bursts the
# venue's 20 reads / 10 writes a second.
_PACE = {"at": 0.0}
_PACE_MIN_S = 0.06


def _pace():
    now = time.monotonic()
    wait = _PACE["at"] + _PACE_MIN_S - now
    if wait > 0:
        time.sleep(wait)
    _PACE["at"] = time.monotonic()


def orderbook(ticker: str, depth: int = 12) -> dict:
    """GET /markets/{ticker}/orderbook → {ok, yes:[(c, q)…], no:[(c, q)…],
    error}. The venue returns BIDS ONLY on each side (`orderbook_fp`:
    yes_dollars / no_dollars as [["0.1500","100.00"], …], best first) — a
    yes bid at X is the no ask at 100 − X with the same size, and vice
    versa. Cents + float sizes; never cached (this IS the tape)."""
    _pace()
    data, err = _get(f"/markets/{ticker}/orderbook", {"depth": int(depth)} if depth else None)
    if data is None:
        return {"ok": False, "yes": [], "no": [], "error": err}
    ob = data.get("orderbook_fp") or data.get("orderbook") or {}

    def lv(rows, dollars: bool):
        out = []
        for r in (rows or []):
            try:
                p, q = r[0], r[1]
                c = round(float(p) * (100.0 if dollars else 1.0), 2)
                q = float(q)
                if 0 < c < 100 and q > 0:
                    out.append((c, q))
            except (TypeError, ValueError, IndexError):
                continue
        return sorted(out, key=lambda x: -x[0])
    if "yes_dollars" in ob or "no_dollars" in ob:
        yes, no = lv(ob.get("yes_dollars"), True), lv(ob.get("no_dollars"), True)
    else:
        yes, no = lv(ob.get("yes"), False), lv(ob.get("no"), False)
    return {"ok": True, "yes": yes, "no": no, "error": None}


def poly_book(ob: dict) -> dict | None:
    """Pure. A Kalshi order book → the YES-canonical `_pmm_book` shape
    {bids, asks, best_bid, best_ask}: bids = the yes bids, asks = the no
    bids inverted (a no bid at X sells YES at 100 − X). A NO hand bet then
    reads it as a synthetic side exactly like a Polymarket NO."""
    if not ob or not ob.get("ok"):
        return None
    bids = sorted(((c, q) for c, q in ob.get("yes") or []), key=lambda x: -x[0])
    asks = sorted(((round(100.0 - c, 2), q) for c, q in ob.get("no") or []), key=lambda x: x[0])
    return {"bids": bids, "asks": asks,
            "best_bid": bids[0][0] if bids else None,
            "best_ask": asks[0][0] if asks else None}


def tape_row(book: dict, ts: float, levels: int = 6) -> tuple | None:
    """Pure. One touch-tape sample from a YES book, in the box's tape row
    shape: (ts, yes_bid_c, yes_bid_q, yes_ask_c, yes_ask_q, bid3, ask3,
    bid ladder, ask ladder) — what `_hand_start_plan`, `_hand_side_rows`
    and the move rule read."""
    if not book:
        return None
    bids, asks = book.get("bids") or [], book.get("asks") or []
    bb = bids[0] if bids else (None, 0.0)
    ba = asks[0] if asks else (None, 0.0)
    return (round(float(ts), 2), bb[0], round(bb[1], 1), ba[0], round(ba[1], 1),
            round(sum(q for _, q in bids[:3]), 1), round(sum(q for _, q in asks[:3]), 1),
            [(p, round(q, 1)) for p, q in bids[:levels]],
            [(p, round(q, 1)) for p, q in asks[:levels]])


def parse_order(o: dict) -> dict:
    """Pure. One venue order → {order_id, ticker, status, yes_c, remaining,
    filled, initial, side, action}. Prices arrive as dollar strings
    (`yes_price_dollars`) or legacy integer cents (`yes_price`); counts as
    `*_count_fp` strings or legacy integers."""
    def n(*keys):
        for k in keys:
            v = o.get(k)
            if v is not None:
                x = _num(v)
                if x is not None:
                    return x
        return None
    yes_c = n("yes_price_dollars")
    if yes_c is not None:
        yes_c = round(yes_c * 100.0, 2)
    else:
        yes_c = n("yes_price")
        if yes_c is None:
            nc = n("no_price_dollars")
            if nc is not None:
                yes_c = round(100.0 - nc * 100.0, 2)
    return {"order_id": o.get("order_id") or o.get("id"), "ticker": o.get("ticker"),
            "status": str(o.get("status") or "").lower(),
            "yes_c": yes_c,
            "remaining": n("remaining_count_fp", "remaining_count") or 0.0,
            "filled": n("fill_count_fp", "fill_count") or 0.0,
            "initial": n("initial_count_fp", "initial_count"),
            "side": o.get("side"), "action": o.get("action"),
            "client_order_id": o.get("client_order_id")}


def our_c(yes_c, buy_yes: bool):
    if yes_c is None:
        return None
    return round(float(yes_c) if buy_yes else 100.0 - float(yes_c), 2)


RESTING = ("resting", "pending", "partially_filled", "partial")


def resting_orders() -> dict:
    """{ok, by_id: {order_id: parsed}, error} — every resting order on the
    account in one read."""
    _pace()
    res = my_orders(ticker=None, status="resting")
    if not res.get("ok"):
        return {"ok": False, "by_id": {}, "error": res.get("error")}
    by_id = {}
    for o in ((res.get("data") or {}).get("orders") or []):
        p = parse_order(o)
        if p["order_id"]:
            by_id[p["order_id"]] = p
    return {"ok": True, "by_id": by_id, "error": None}


def parse_positions(res: dict) -> dict | None:
    """{ticker: net contracts} off a positions read (+ = YES held, − = NO
    held; `position_fp` string, legacy `position` int). None when the read
    failed — a failed read must never look like 'nothing held'."""
    if not res or not res.get("ok"):
        return None
    out = {}
    for mp in ((res.get("data") or {}).get("market_positions") or []):
        t = mp.get("ticker")
        v = _num(mp.get("position_fp"))
        if v is None:
            v = _num(mp.get("position"))
        if t and v is not None:
            out[t] = float(v)
    return out


def positions() -> dict | None:
    _pace()
    return parse_positions(my_positions())


def held_for(pos: dict | None, ticker: str, buy_yes: bool) -> float | None:
    """Contracts held on OUR side of a ticker; None when positions are
    unreadable."""
    if pos is None:
        return None
    net = float(pos.get(ticker) or 0.0)
    if buy_yes:
        return net if net > 0 else 0.0
    return -net if net < 0 else 0.0


# ───────────── settlements: THE VENUE IS THE GRADER ─────────────
# Rob, Oct 25 2026 (Denver @ Arizona O 42.5: Kalshi paid the over at 46
# points with 6:21 left and the sheet read OFF THE BOOK): "Settled only
# comes from Polymarket and Kalshi." Shape off docs.kalshi.com/api-reference/
# portfolio/get-settlements.md (fetched the same day): `market_result`
# yes|no|scalar, `yes_count_fp`/`no_count_fp` (contracts held at settlement,
# fixed-point strings), `*_total_cost_dollars`, `revenue` in CENTS (winners
# pay 100¢ a contract), `fee_cost` dollars, `settled_time`. Archived
# settlements fall off this feed — read it soon after the game, not weeks on.
def my_settlements(ticker: str | None = None, min_ts: int | None = None, limit: int = 200,
                   cursor: str | None = None) -> dict:
    p: dict = {"limit": int(limit)}
    if ticker:
        p["ticker"] = ticker
    if min_ts:
        p["min_ts"] = int(min_ts)
    if cursor:
        p["cursor"] = cursor
    return signed_request("GET", "/portfolio/settlements", params=p)


def parse_settlement(s: dict) -> dict | None:
    """One settlement row → {ticker, event_ticker, result, yes_q, no_q,
    yes_cost, no_cost, revenue_usd, fee_usd, settled_time}; None on a row
    with no ticker or result."""
    if not isinstance(s, dict) or not s.get("ticker") or not s.get("market_result"):
        return None
    rev = _num(s.get("revenue"))
    return {"ticker": s.get("ticker"), "event_ticker": s.get("event_ticker"),
            "result": str(s.get("market_result")).lower(),
            "yes_q": _num(s.get("yes_count_fp")) if s.get("yes_count_fp") is not None else _num(s.get("yes_count")),
            "no_q": _num(s.get("no_count_fp")) if s.get("no_count_fp") is not None else _num(s.get("no_count")),
            "yes_cost": _num(s.get("yes_total_cost_dollars")),
            "no_cost": _num(s.get("no_total_cost_dollars")),
            "revenue_usd": None if rev is None else round(rev / 100.0, 2),
            "fee_usd": _num(s.get("fee_cost")) or 0.0,
            "settled_time": s.get("settled_time")}


def settlements(min_ts: int | None = None, max_pages: int = 3) -> dict | None:
    """{ticker: parsed settlement} for the live settlement set (newest first
    from the venue; the first row per ticker wins). None when the read
    failed — a failed read must never look like 'nothing settled'."""
    out: dict = {}
    cursor = None
    for _ in range(max_pages):
        _pace()
        res = my_settlements(min_ts=min_ts, limit=200, cursor=cursor)
        if not res.get("ok"):
            return None
        data = res.get("data") or {}
        for s in (data.get("settlements") or []):
            ps = parse_settlement(s)
            if ps and ps["ticker"] not in out:
                out[ps["ticker"]] = ps
        cursor = data.get("cursor")
        if not cursor:
            break
    return out


def settle_verdict(ps: dict | None, buy_yes: bool) -> dict | None:
    """What the venue did to OUR side of a settled market (pure).
    {status won|lost, qty, cost_usd, revenue_usd, fee_usd, pnl_usd,
    result, settled_time}; None when the market settled scalar (not a
    binary we bet), the row is unreadable, or we held nothing on our side
    (a bet that never filled is not graded — it was cancelled or missed)."""
    if not ps or ps.get("result") not in ("yes", "no"):
        return None
    qty = ps.get("yes_q") if buy_yes else ps.get("no_q")
    if qty is None or float(qty) <= 0.0:
        return None
    cost = (ps.get("yes_cost") if buy_yes else ps.get("no_cost")) or 0.0
    won = (ps["result"] == "yes") == bool(buy_yes)
    rev = ps.get("revenue_usd")
    if rev is None:
        rev = round(float(qty), 2) if won else 0.0
    fee = float(ps.get("fee_usd") or 0.0)
    return {"status": "won" if won else "lost", "qty": round(float(qty), 2),
            "cost_usd": round(float(cost), 2), "revenue_usd": round(float(rev), 2), "fee_usd": round(fee, 2),
            "pnl_usd": round(float(rev) - float(cost) - fee, 2),
            "result": ps["result"], "settled_time": ps.get("settled_time")}


# ───────────── the socket book (pure; cellar/kalshifeed.py feeds it) ─────────────
# Kalshi's `orderbook_delta` channel: one `orderbook_snapshot` per market on
# subscribe ({market_ticker, yes: [[price, qty]…], no: [[…]]} — cents ints
# on the legacy shape, `yes_dollars`/`no_dollars` fixed-point strings on the
# new one), then `orderbook_delta` frames ({market_ticker, price, delta,
# side} / `price_dollars` + `delta_fp`): the signed change in resting size
# at one price on one side. Both sides are BIDS (a no bid at X = a yes ask
# at 100 − X), exactly like the REST order book.
def _ws_price_c(msg: dict, dollars_key: str, cents_key: str):
    if msg.get(dollars_key) is not None:
        v = _num(msg.get(dollars_key))
        return None if v is None else round(v * 100.0, 2)
    v = _num(msg.get(cents_key))
    return None if v is None else round(float(v), 2)


_WS_SNAP_KEYS = (("{s}_dollars_fp", True), ("{s}_dollars", True), ("{s}_fp", False), ("{s}", False))


def ws_snapshot(msg: dict) -> dict:
    """An `orderbook_snapshot` body → {"yes": {price_c: qty}, "no": {…},
    "keys": [...]}. THE WIRE IS `yes_dollars_fp` / `no_dollars_fp` (Oct 18
    2026, read off docs.kalshi.com/websockets/orderbook-updates.md: arrays
    of ["0.5300", "901189.00"], the key ABSENT when a side has no offers;
    `price_dollars` + `delta_fp` on the deltas). Until this fix the parser
    looked for `yes_dollars` / `yes`, found neither, built every book from
    an EMPTY base and let the deltas since subscribe pose as the ladder —
    the ARI +13.5 socket book read 51×4,500 / 50×100 against a venue
    showing 53×901k / 52×731k. `keys` names the body's level keys so a
    renamed wire shows up as an empty book WITH unknown keys, never a quiet
    zero again."""
    state = {"yes": {}, "no": {}, "keys": sorted(k for k in msg.keys()
                                                  if k not in ("market_ticker", "market_id"))}
    for side in ("yes", "no"):
        rows, dollars = None, False
        for pat, dol in _WS_SNAP_KEYS:
            k = pat.format(s=side)
            if msg.get(k) is not None:
                rows, dollars = msg.get(k), dol
                break
        for r in rows or []:
            try:
                if isinstance(r, dict):
                    pv = r.get("price_dollars", r.get("price"))
                    qv = r.get("count_fp", r.get("quantity_fp", r.get("count", r.get("quantity"))))
                    dol = "price_dollars" in r or dollars
                else:
                    pv, qv, dol = r[0], r[1], dollars
                pf = float(pv)
                if not dol and pf <= 1.0 and isinstance(pv, str) and "." in pv:
                    dol = True                       # a "0.53" where cents were expected
                c = round(pf * (100.0 if dol else 1.0), 2)
                q = float(qv)
            except (TypeError, ValueError, IndexError, KeyError):
                continue
            if 0 < c < 100 and q > 0:
                state[side][c] = q
    return state


def ws_snapshot_suspect(msg: dict, state: dict) -> bool:
    """Pure. A snapshot that parsed to NOTHING while its body carried level
    data under keys we did not read — the renamed-wire tripwire."""
    if (state.get("yes") or state.get("no")):
        return False
    known = {pat.format(s=s) for pat, _ in _WS_SNAP_KEYS for s in ("yes", "no")}
    for k, v in (msg or {}).items():
        if k in ("market_ticker", "market_id") or k in known:
            continue
        if isinstance(v, (list, dict)) and v:
            return True
    return False


def ws_delta(state: dict, msg: dict) -> dict:
    """Apply one `orderbook_delta` body in place; returns the state."""
    side = str(msg.get("side") or "").lower()
    if side not in ("yes", "no"):
        return state
    c = _ws_price_c(msg, "price_dollars", "price")
    if c is None:
        c = _ws_price_c(msg, "price_dollars_fp", "price_fp")
    d = _num(msg.get("delta_fp")) if msg.get("delta_fp") is not None else _num(msg.get("delta"))
    if c is None or d is None:
        return state
    book = state.setdefault(side, {})
    q = book.get(c, 0.0) + float(d)
    if q <= 1e-9:
        book.pop(c, None)
    else:
        book[c] = q
    return state


def ws_to_ob(state: dict) -> dict:
    """The socket state → the same {ok, yes, no} shape `orderbook()` returns."""
    yes = sorted(((c, q) for c, q in (state.get("yes") or {}).items() if q > 0), key=lambda x: -x[0])
    no = sorted(((c, q) for c, q in (state.get("no") or {}).items() if q > 0), key=lambda x: -x[0])
    return {"ok": True, "yes": yes, "no": no, "error": None}


def ws_seq_ok(last: dict, sid, seq) -> bool:
    """Pure. Track `seq` per `sid`; False on a gap (the venue says: check it
    to guarantee you received every message — a gap means resync)."""
    try:
        sid_i, seq_i = int(sid), int(seq)
    except (TypeError, ValueError):
        return True
    prev = last.get(sid_i)
    last[sid_i] = seq_i
    return prev is None or seq_i == prev + 1


def ws_auth_headers() -> list | None:
    """The signed handshake headers for wss://…/trade-api/ws/v2 (the REST
    scheme: ts + GET + the ws path), as websocket-client header lines."""
    kid, pem, _src = _creds()
    if not (kid and pem):
        return None
    ts = str(int(time.time() * 1000))
    sig = _sign(pem, ts + "GET" + "/trade-api/ws/v2")
    return [f"KALSHI-ACCESS-KEY: {kid}", f"KALSHI-ACCESS-SIGNATURE: {sig}",
            f"KALSHI-ACCESS-TIMESTAMP: {ts}"]
