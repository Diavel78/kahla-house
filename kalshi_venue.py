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
    import base64
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    key = serialization.load_pem_private_key(pem.replace("\\n", "\n").encode(), password=None)
    sig = key.sign(msg.encode(),
                   padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
                   hashes.SHA256())
    return base64.b64encode(sig).decode()


def signed_request(method: str, path: str, body: dict | None = None, params: dict | None = None,
                   timeout: float = 12.0) -> dict:
    """One signed call. Returns {ok, status, data|error, base, body_sent}.
    401/403 short-circuit (the key is the problem, not the host); a 4xx
    with a JSON error body is returned as data too so the caller can read
    Kalshi's own reason (insufficient_balance, invalid field…)."""
    from urllib.parse import urlparse
    kid, pem, src = _creds()
    if not (kid and pem):
        return {"ok": False, "configured": False, "error": f"no creds ({src})"}
    method = method.upper()
    last = None
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
                              headers=headers, timeout=timeout)
            try:
                data = r.json() if (r.text or "").strip() else {}
            except ValueError:
                data = {"raw": (r.text or "")[:300]}
            out = {"ok": 200 <= r.status_code < 300, "configured": True, "status": r.status_code,
                   "data": data, "base": base, "creds": src}
            if body is not None:
                out["body_sent"] = body
            if out["ok"] or r.status_code in (400, 401, 403, 404, 409, 422):
                if not out["ok"]:
                    out["error"] = f"HTTP {r.status_code}: {str(data)[:300]}"
                return out
            last = f"HTTP {r.status_code} @ {base}: {(r.text or '')[:160]}"
        except Exception as e:  # noqa: BLE001
            last = f"{type(e).__name__} @ {base}: {str(e)[:120]}"
    return {"ok": False, "configured": True, "error": last}


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


def cancel_order(order_id: str) -> dict:
    return signed_request("DELETE", f"/portfolio/orders/{order_id}")


# THE CREATE SCHEMA IS UNSETTLED ACROSS SOURCES (Oct 18 2026): the current
# docs show dollar-string prices + `time_in_force`; the legacy body carries
# integer cents + `expiration_ts`; community reports say the integer body
# 400s now. `order_bodies` builds every shape we know, newest first;
# `create_order` sends them in turn and stops at the first answer that is
# not a schema rejection (a 400 naming a field). The probe records WHICH
# shape the venue took — that result is the schema from then on
# (`CREATE_SCHEMA` pins it once known).
CREATE_SCHEMA: str | None = None


def order_bodies(ticker: str, buy_yes: bool, price_c, count: int, tif: str = "good_till_canceled",
                 post_only: bool = True, client_order_id: str | None = None,
                 expiration_time: str | None = None, action: str = "buy") -> list[tuple[str, dict]]:
    import uuid
    cid = client_order_id or f"kh-{uuid.uuid4().hex[:20]}"
    side = "yes" if buy_yes else "no"
    pc = int(round(float(price_c)))
    pc = max(1, min(99, pc))
    dollars = f"{pc / 100.0:.4f}"
    v2 = {"ticker": ticker, "client_order_id": cid, "action": action, "side": side, "type": "limit",
          "count_fp": f"{int(count)}.00", "time_in_force": tif, "post_only": bool(post_only),
          ("yes_price_dollars" if buy_yes else "no_price_dollars"): dollars}
    if expiration_time and tif == "good_till_canceled":
        v2["expiration_time"] = expiration_time
    v2b = dict(v2)
    v2b.pop("count_fp")
    v2b["count"] = int(count)
    legacy = {"ticker": ticker, "client_order_id": cid, "action": action, "side": side, "type": "limit",
              "count": int(count), "post_only": bool(post_only),
              ("yes_price" if buy_yes else "no_price"): pc}
    if tif == "immediate_or_cancel":
        legacy["time_in_force"] = tif
    elif expiration_time:
        try:
            legacy["expiration_ts"] = int(datetime.fromisoformat(expiration_time.replace("Z", "+00:00")).timestamp())
        except Exception:  # noqa: BLE001
            pass
    return [("v2_dollars_fp", v2), ("v2_dollars_count", v2b), ("legacy_cents", legacy)]


_SCHEMA_REJECT = re.compile(r"unknown|unrecogni|invalid (?:field|param|request|body)|required|missing|"
                            r"deprecated|count_fp|price_dollars|yes_price|expiration|time_in_force|schema|parse",
                            re.I)


def create_order(ticker: str, buy_yes: bool, price_c, count: int, tif: str = "good_till_canceled",
                 post_only: bool = True, client_order_id: str | None = None,
                 expiration_time: str | None = None, action: str = "buy") -> dict:
    """Place ONE order. post-only GTC by default (a resting hand bet);
    tif='immediate_or_cancel' + post_only=False is the take. Returns the
    venue's answer plus `schema` (which body shape it accepted) and
    `order` (the order object when created) / `order_id`."""
    global CREATE_SCHEMA
    bodies = order_bodies(ticker, buy_yes, price_c, count, tif, post_only, client_order_id, expiration_time, action)
    if CREATE_SCHEMA:
        bodies.sort(key=lambda b: 0 if b[0] == CREATE_SCHEMA else 1)
    attempts = []
    for name, body in bodies:
        res = signed_request("POST", "/portfolio/orders", body=body)
        res["schema"] = name
        attempts.append({"schema": name, "status": res.get("status"), "error": res.get("error")})
        d = res.get("data") or {}
        if res.get("ok"):
            o = d.get("order") or d
            res["order"] = o
            res["order_id"] = o.get("order_id") or o.get("id")
            res["order_status"] = o.get("status")
            CREATE_SCHEMA = name
            break
        msg = str(d)[:400]
        if res.get("status") == 400 and _SCHEMA_REJECT.search(msg):
            continue                         # the venue rejected the SHAPE — try the next one
        break                                # a real answer (insufficient funds, 401, 403, 404…)
    res["attempts"] = attempts
    return res


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
                     "order_id": cr.get("order_id"), "order_status": cr.get("order_status"),
                     "error": cr.get("error"), "attempts": cr.get("attempts"),
                     "data": (cr.get("data") if not cr.get("ok") else None), "body_sent": cr.get("body_sent")}
    step("create", **{k: v for k, v in out["create"].items() if k not in ("data", "body_sent")})
    if not cr.get("ok"):
        st = cr.get("status")
        msg = str(cr.get("data") or cr.get("error") or "").lower()
        if st in (401, 403):
            out["verdict"] = "write_denied"           # the key cannot trade
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
    cx = cancel_order(oid) if oid else {"ok": False, "error": "no order id"}
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
