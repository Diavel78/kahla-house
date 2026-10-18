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
POLY_TAKER_C, POLY_MAKER_C = 5.0, 1.25
KALSHI_TAKER_C, KALSHI_MAKER_C = 7.0, 1.75


def _pq(price_c) -> float:
    p = max(0.0, min(1.0, float(price_c) / 100.0))
    return p * (1.0 - p)


def fee_per_contract_c(venue: str, role: str, price_c, fee: dict | None = None) -> float:
    """Fee in cents per contract for buying at `price_c`. role = 'take' |
    'rest'. `fee` = the Kalshi series fee dict from `series_fee` ({maker:
    bool, mult: float}); None = defaults (maker charged, mult 1 — the
    CONSERVATIVE read when the series could not be read)."""
    if price_c is None:
        return 0.0
    v = (venue or "").upper()
    if v.startswith("POLY"):
        return round((POLY_TAKER_C if role == "take" else POLY_MAKER_C) * _pq(price_c), 4)
    mult = float((fee or {}).get("mult", 1.0) or 1.0)
    if role == "take":
        return round(KALSHI_TAKER_C * mult * _pq(price_c), 4)
    maker = (fee or {}).get("maker", True)
    return round(KALSHI_MAKER_C * mult * _pq(price_c), 4) if maker else 0.0


def order_fee_c(venue: str, role: str, price_c, qty: int, fee: dict | None = None) -> float:
    """The whole order's fee in cents. Kalshi rounds the ORDER's fee up to
    the next cent (a 5-lot pays a whole cent on a 0.4¢ fee); Polymarket
    charges per share to the hundredth of a cent."""
    per = fee_per_contract_c(venue, role, price_c, fee)
    tot = per * max(0, int(qty or 0))
    if (venue or "").upper().startswith("KALSHI"):
        return float(math.ceil(tot - 1e-9))
    return round(tot, 2)


def all_in_c(venue: str, role: str, price_c, qty: int, fee: dict | None = None):
    """Per-contract all-in cost in cents: price + the order's fee spread
    over its contracts. None when there is no price."""
    if price_c is None or not qty:
        return None
    return round(float(price_c) + order_fee_c(venue, role, price_c, qty, fee) / int(qty), 3)


def compare_rest(poly_touch_c, kalshi_touch_c, qty: int, kalshi_fee: dict | None,
                 current: str | None = None, min_gain_c: float = 0.0):
    """Pure. WHERE A RESTING BET SITS. Each venue's seat = our-side touch
    (the price we would join) + its maker fee. Lower all-in wins; a tie, or
    a gain under `min_gain_c`, keeps the bet where it is (`current`) so the
    box never churns a venue switch on noise. Returns (venue, detail)."""
    opts = {}
    a = all_in_c("POLYMARKET", "rest", poly_touch_c, qty)
    if a is not None:
        opts["POLYMARKET"] = a
    b = all_in_c("KALSHI", "rest", kalshi_touch_c, qty, kalshi_fee)
    if b is not None:
        opts["KALSHI"] = b
    if not opts:
        return None, {"why": "no_quote"}
    best = min(opts, key=opts.get)
    det = {"all_in": opts, "best": best}
    if current and current in opts and best != current:
        if opts[current] - opts[best] < float(min_gain_c) - 1e-9:
            det["why"] = "gain_under_threshold"
            return current, det
        det["why"] = "switch"
        return best, det
    det["why"] = "stay" if current == best else "best"
    return best, det


def compare_take(poly_ask_c, kalshi_ask_c, qty: int, kalshi_fee: dict | None,
                 resting_c=None, cap_c=None):
    """Pure. THE TAKE AT THE DROP across venues: each ask + its taker fee;
    the cheaper all-in wins. With `resting_c` and `cap_c` the winner must
    sit within cap_c of the resting price on its RAW ask (Rob's 1¢ rule
    is on the line, the fee is the cost of crossing). Returns (venue or
    None, detail)."""
    opts, raw = {}, {}
    a = all_in_c("POLYMARKET", "take", poly_ask_c, qty)
    if a is not None:
        opts["POLYMARKET"], raw["POLYMARKET"] = a, float(poly_ask_c)
    b = all_in_c("KALSHI", "take", kalshi_ask_c, qty, kalshi_fee)
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


def markets(series: str, status: str = "open", max_pages: int = 8, ttl_s: float = 30.0) -> dict:
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
    out = {"ok": False, "maker": True, "mult": 1.0, "fee_type": None, "raw": {}, "error": err}
    if sr:
        feeish = {k: v for k, v in sr.items() if re.search(r"fee|maker|taker|mult", str(k), re.I)}
        ft = str(sr.get("fee_type") or "")
        out.update(ok=True, fee_type=ft or None, raw=feeish,
                   maker=("maker" in ft.lower()) if ft else bool(feeish.get("maker_fee") or feeish.get("maker_fee_multiplier")),
                   mult=float(sr.get("fee_multiplier") or feeish.get("taker_fee_multiplier") or 1.0) or 1.0)
        _CACHE[key] = (time.time(), out)
    return out


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


def discover_series(sport: str, mt: str) -> list:
    """Candidate series for (sport, ml|spread|total): the named guesses
    first, then every catalog ticker carrying the sport's tag and a
    market-type word in its ticker or title."""
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
    return cands


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
    series fee terms. Walks the candidate series for the market type."""
    mt = item.get("market_type")
    tried, found = [], None
    for series in discover_series(sport, mt):
        res = markets(series)
        tried.append({"series": series, "n": len(res.get("markets") or []), "err": res.get("error")})
        if not res.get("markets"):
            continue
        ev = pick_event(res["markets"], away, home, start_iso)
        if not ev:
            continue
        r = rung_for(item, ev[1], away, home)
        found = {"series": series, "event_ticker": ev[0], "n_event_markets": len(ev[1]), **r}
        if r.get("ok"):
            break
    if not found:
        return {"ok": False, "why": "no series lists this game", "tried": tried}
    if found.get("ok"):
        m = found.pop("market")
        found["quote"] = our_side_quote(m, found["buy_yes"])
        found["book"] = book_c(m)
        found["fee"] = series_fee(found["series"])
        found["raw_sample"] = {k: m.get(k) for k in
                               ("ticker", "title", "subtitle", "yes_sub_title", "no_sub_title",
                                "floor_strike", "cap_strike", "strike_type", "close_time",
                                "expected_expiration_time", "status") if k in m}
    found["tried"] = tried
    return found
