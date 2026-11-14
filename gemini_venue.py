"""Gemini Predictions as the hand-bet second venue — READ SIDE (step 1).

Rob, Nov 8 2026: "IT REPLACES KALSHI… SAME CHASE". Gemini takes Kalshi's slot
in the bet-sheet design: same seat rule, same chase, same rules everywhere. This
module is the Gemini twin of the read half of `kalshi_venue.py`:

  * fees      — make $0 (Gemini's maker fee-free promo, Sep 2 → Dec 31 2026),
                1.75¢·p(1−p) after it; take 7¢·p(1−p). Both round UP per order.
  * resolve   — a slip item {market_type, side, line} → the Gemini contract by the
                SLIP'S RUNG RULE (exact line, else the nearest MORE favorable, never
                worse), off ONE events list per call (cached 30s).
  * book      — /v1/book/{symbol} → the YES-canonical `_pmm_book` shape, so the
                box's seat / thin-touch / chase code reads it like Polymarket's or
                Kalshi's (a NO bet reads it inverted, `synthetic`).

GAME MATCH = team NAMES in the event title + start within 12h ('Denver vs
Carolina', away first; college by school) — never team codes, which differ
between venues. Ticker `NFL-2611081800-DEN-CAR-S`: date code = UTC start, the two
codes in title order, kind M (moneyline) / S (spread) / T (total). Contract tails:
M `CAR` = CAR wins; S `CAR3` = CAR wins by more than 3.5 (CAR −3.5); T `O43` =
over 43.5. Prices are YES prices in dollars; NO is a native side on Gemini
(buy NO at q = sell YES at 1 − q).

Nothing here writes. Orders are step 2.
"""
from __future__ import annotations

import math
import re
import threading
import os
import time
import unicodedata
from datetime import datetime, timezone
from typing import Any, Optional

import gemini_pm as _g

TICK_C = 1.0                      # Gemini prices in whole cents
GEMINI_TAKER_C = 7.0              # 0.07·C·p(1−p) dollars = 7¢·p(1−p) a contract
GEMINI_MAKER_C = 1.75             # the schedule's maker rate, charged after the promo
MAKER_PROMO_END = datetime(2027, 1, 1, 5, 0, tzinfo=timezone.utc)   # Dec 31 2026 end of day ET


# ───────────────────────── fees ─────────────────────────

def _pq(price_c) -> float:
    p = max(0.0, min(1.0, float(price_c) / 100.0))
    return p * (1.0 - p)


def maker_c(at=None) -> float:
    """The maker coefficient in force: 0 during the fee-free promo."""
    t = at or datetime.now(timezone.utc)
    return 0.0 if t < MAKER_PROMO_END else GEMINI_MAKER_C


def fee_per_contract_c(role: str, price_c, at=None) -> float:
    if price_c is None:
        return 0.0
    coef = GEMINI_TAKER_C if role == "take" else maker_c(at)
    return round(coef * _pq(price_c), 4)


def order_fee_c(role: str, price_c, qty, at=None) -> float:
    """The whole order's fee in cents, rounded UP to the cent (venue rule)."""
    tot = fee_per_contract_c(role, price_c, at) * max(0.0, float(qty or 0))
    return float(math.ceil(tot - 1e-9)) if tot > 0 else 0.0


def all_in_c(role: str, price_c, qty, at=None):
    if price_c is None or not qty:
        return None
    return round(float(price_c) + order_fee_c(role, price_c, qty, at) / float(qty), 3)


# ───────────────────────── the event list ─────────────────────────

_EV_CACHE: dict = {"at": 0.0, "evs": None, "err": None}
_EV_LOCK = threading.Lock()


def _fetch_events() -> tuple[list, Optional[str]]:
    """THE FULL CATALOG WALK — discovery (new games, changed mappings). Since
    the targeted refresh below it runs at most every `FULL_EVERY_S` on the box."""
    try:
        evs = _g.events_all("sports", "active")
    except Exception as e:
        with _EV_LOCK:
            _EV_CACHE["err"] = f"{type(e).__name__}: {e}"[:200]
            stale = _EV_CACHE["evs"]
        return (stale or []), _EV_CACHE["err"]
    now = time.time()
    with _EV_LOCK:
        _EV_CACHE.update(at=now, full_at=now, evs=evs, err=None,
                         fetch_s=round(now - _EV_CACHE.get("t0", now), 2))
        _EV_STATS["full"] += 1
        _EV_STATS["full_s"] = _EV_CACHE["fetch_s"]
    return evs, None


# ── THE TARGETED REFRESH (Nov 8 2026, Rob: "look up the fucking game you actually
# want to look up") ──────────────────────────────────────────────────────────
# Measured before building (/api/gemini/coverage?shape=1): GET
# /v1/prediction-markets/events/{ticker} answers in ~0.5s with the same contracts
# and the same bid/ask as the catalog's copy of that event (BOS@MIL spread, 33 of
# 33, pre-game); an unknown ticker answers 404; the catalog walk is ~4s from the
# site and 14-26s on the box. So between full walks the box re-reads ONLY the
# event tickers somebody is using (a resolve, a lane bet, the touch walk's near
# games — `mark_hot`), one event at a time, and swaps each into the cached list.
# A failed read (404 / 500 / 503 / timeout) keeps the cached copy; the next full
# walk is what drops a game Gemini removed.
FULL_EVERY_S = 300.0       # discovery: the whole catalog at most this often (the touch tape's near tier is 3 min)
HOT_TTL_S = 1800.0         # an event ticker stays hot this long after its last use
HOT_MAX = 40               # event reads per targeted pass
_HOT: dict = {}            # event ticker → last use (epoch)
_EV_STATS = {"full": 0, "targeted": 0, "event_ok": 0, "event_err": 0, "last_err": None, "last_pass": None}


def event_ticker_of(symbol) -> Optional[str]:
    """Pure. Contract → its event: GEMI-NBA-2611090100-BOS-MIL-S-BOS12 →
    NBA-2611090100-BOS-MIL-S."""
    s_ = str(symbol or "")
    if s_.upper().startswith("GEMI-"):
        s_ = s_[5:]
    parts = s_.split("-")
    return "-".join(parts[:-1]) if len(parts) >= 6 else None


def mark_hot(tickers) -> None:
    now = time.time()
    with _EV_LOCK:
        for t in tickers or ():
            if t:
                _HOT[str(t)] = now


def hot_tickers(now: Optional[float] = None) -> list:
    """Pure-ish. The hot event tickers, most recently used first, dead ones dropped."""
    now = time.time() if now is None else now
    with _EV_LOCK:
        for t in [t for t, at in _HOT.items() if now - at > HOT_TTL_S]:
            _HOT.pop(t, None)
        return [t for t, _ in sorted(_HOT.items(), key=lambda kv: -kv[1])][:HOT_MAX]


def merge_events(evs: list, fresh: dict) -> list:
    """Pure. A NEW list with each event whose ticker is in `fresh` replaced by
    the fresh copy (the old list is never mutated — the index memo keys on it).
    A fresh event the list doesn't hold is appended (listed since the walk)."""
    out, seen = [], set()
    for e in evs or []:
        t = (e or {}).get("ticker")
        if t in fresh:
            out.append(fresh[t])
            seen.add(t)
        else:
            out.append(e)
    out.extend(v for t, v in fresh.items() if t not in seen)
    return out


def _valid_event(ev, ticker) -> bool:
    """Pure. A single-event answer we trust enough to swap in: the right ticker,
    a contracts list, every contract carrying its symbol."""
    if not isinstance(ev, dict) or ev.get("ticker") != ticker:
        return False
    cs = ev.get("contracts")
    return isinstance(cs, list) and all(isinstance(c, dict) and c.get("instrumentSymbol") for c in cs)


def _refresh_hot() -> int:
    """Re-read each hot event ticker on its own and swap it into the cache.
    Returns how many were refreshed. Never clears a cached event."""
    tickers = hot_tickers()
    t0 = time.time()
    fresh: dict = {}
    for t in tickers:
        try:
            ev = _g.event(t)
            if isinstance(ev, dict) and isinstance(ev.get("data"), dict) and "ticker" not in ev:
                ev = ev["data"]
            if _valid_event(ev, t):
                fresh[t] = ev
                _EV_STATS["event_ok"] += 1
            else:
                _EV_STATS["event_err"] += 1
                _EV_STATS["last_err"] = f"{t}: unexpected shape"
        except Exception as e:                     # 404 / 500 / 503 / timeout → keep the cached copy
            _EV_STATS["event_err"] += 1
            _EV_STATS["last_err"] = f"{t}: {type(e).__name__}: {e}"[:200]
    with _EV_LOCK:
        if fresh and _EV_CACHE["evs"] is not None:
            _EV_CACHE["evs"] = merge_events(_EV_CACHE["evs"], fresh)
        _EV_CACHE["at"] = time.time()
        _EV_STATS["targeted"] += 1
        _EV_STATS["last_pass"] = {"events": len(tickers), "ok": len(fresh), "ms": int((time.time() - t0) * 1000)}
    return len(fresh)


def _bg_refresh() -> None:
    """The background refresher: the full walk when discovery is due (or there
    is nothing cached), else only the hot events."""
    try:
        with _EV_LOCK:
            _EV_CACHE["t0"] = time.time()
            full_due = (_EV_CACHE["evs"] is None
                        or time.time() - float(_EV_CACHE.get("full_at") or 0.0) >= FULL_EVERY_S)
        if full_due:
            _fetch_events()
        else:
            _refresh_hot()
    finally:
        with _EV_LOCK:
            _EV_CACHE["bg"] = False


def refresh_stats() -> dict:
    with _EV_LOCK:
        return {**_EV_STATS, "hot": len(_HOT),
                "age_s": None if _EV_CACHE["evs"] is None else round(time.time() - _EV_CACHE["at"], 1),
                "full_age_s": (round(time.time() - float(_EV_CACHE["full_at"]), 1)
                               if _EV_CACHE.get("full_at") else None)}


def sports_events(ttl_s: float = 30.0, stale_ok_s: float = 0.0) -> tuple[list, Optional[str]]:
    """Every active sports event (one paged REST walk), cached `ttl_s`.

    THE WALK IS SLOW (Nov 8 2026: ~14s from the site, ~26s on the box — every
    sport, every game, every contract, 500 events a page — and a bet placed
    more than 30s after the last one paid all of it: Rob, "taking so damn
    long"). With `stale_ok_s` a cache older than `ttl_s` but younger than
    `stale_ok_s` is returned AT ONCE and refreshed in the background; the
    contract names barely change and every placement reads the live book for
    its price anyway."""
    with _EV_LOCK:
        have = _EV_CACHE["evs"] is not None
        age = time.time() - _EV_CACHE["at"]
        if have and age < ttl_s:
            return _EV_CACHE["evs"], None
        if have and stale_ok_s > 0 and age < stale_ok_s:
            if not _EV_CACHE.get("bg"):
                _EV_CACHE["bg"] = True
                threading.Thread(target=_bg_refresh, name="gemini-events", daemon=True).start()
            return _EV_CACHE["evs"], None
        _EV_CACHE["t0"] = time.time()
    return _fetch_events()


def warm(max_age_s: float = 60.0) -> bool:
    """Keep the catalog fresh OFF the bet path: start a background walk when the
    cache is empty or older than `max_age_s` (never blocks). The handbets tick
    calls it every tick; the bet path only ever reads memory. True = a walk
    was started."""
    with _EV_LOCK:
        have = _EV_CACHE["evs"] is not None
        age = time.time() - _EV_CACHE["at"]
        if (have and age < max_age_s) or _EV_CACHE.get("bg"):
            return False
        _EV_CACHE["bg"] = True
    threading.Thread(target=_bg_refresh, name="gemini-events", daemon=True).start()
    return True


def events_age_s() -> Optional[float]:
    with _EV_LOCK:
        return None if _EV_CACHE["evs"] is None else round(time.time() - _EV_CACHE["at"], 1)


_IDX_MEMO: dict = {"key": None, "idx": None}


def game_index_cached(evs) -> dict:
    """game_index(evs) built once per events list (the list is replaced, never
    mutated, on each refresh)."""
    if _IDX_MEMO.get("ref") is not evs or _IDX_MEMO["idx"] is None:
        _IDX_MEMO["idx"] = game_index(evs)
        _IDX_MEMO["ref"] = evs
    return _IDX_MEMO["idx"]


# ───────────────────────── matching (pure) ─────────────────────────

def norm(x) -> str:
    t = unicodedata.normalize("NFKD", str(x or "")).encode("ascii", "ignore").decode().lower()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", t).split())


def _cents(v):
    try:
        return round(float(v) * 100.0, 2) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def game_index(evs) -> dict:
    """Pure. Gemini sports events → {(PREFIX, t1, t2, code): {"start": epoch,
    "a": title team 1, "b": title team 2, "T": {line: (sym, bid_c, ask_c)},
    "S": {(team, line): …}, "M": {team: …}, "events": {kind: event ticker}}}."""
    out: dict = {}
    for e in evs or []:
        parts = (e.get("ticker") or "").split("-")
        if len(parts) < 5 or not parts[1].isdigit() or len(parts[1]) < 10:
            continue
        try:
            utc = datetime.strptime(parts[1][:10], "%y%m%d%H%M").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        g = out.setdefault((parts[0].upper(), parts[2].lower(), parts[3].lower(), parts[1][:10]),
                           {"start": utc.timestamp(), "a": None, "b": None,
                            "T": {}, "S": {}, "M": {}, "events": {}})
        title = str(e.get("title") or "").split(":")[0]
        if " vs " in title and g["a"] is None:
            a, b = title.split(" vs ", 1)
            g["a"], g["b"] = norm(a), norm(b)
        kind = "-".join(parts[4:]).upper()
        g["events"][kind] = e.get("ticker")
        for c in e.get("contracts") or []:
            sym = c.get("instrumentSymbol") or ""
            tail = sym.rsplit("-", 1)[-1]
            pr = c.get("prices") or {}
            q = (sym, _cents(pr.get("bestBid")), _cents(pr.get("bestAsk")))
            if kind == "T":
                m = re.match(r"^O(\d+)$", tail)
                if m:
                    g["T"][float(m.group(1)) + 0.5] = q
            elif kind == "S":
                m = re.match(r"^([A-Z]+?)(\d+)$", tail)
                if m:
                    g["S"][(m.group(1).lower(), float(m.group(2)) + 0.5)] = q
            elif kind == "M":
                if re.match(r"^[A-Z]+$", tail):
                    g["M"][tail.lower()] = q
    return out


def name_hit(gem_name, our_name) -> bool:
    """Pure. Gemini names a team by city / school ('Los Angeles C', 'Western
    Michigan', 'New York J'); ours are full names. A prefix of ours = a hit."""
    gn, on = norm(gem_name), norm(our_name)
    return bool(gn) and (on == gn or on.startswith(gn + " ") or (on.startswith(gn) and len(gn) >= 4))


def _epoch(start) -> Optional[float]:
    if start is None:
        return None
    if isinstance(start, (int, float)):
        return float(start)
    try:
        d = datetime.fromisoformat(str(start).replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.timestamp()
    except ValueError:
        return None


def find_game(idx: dict, away, home, start) -> tuple:
    """Pure. Our game (full names + start) → (gemini game, away code, home code),
    else (None, None, None). Both names must hit the title, start within 12h;
    the nearest start wins (a series or a rematch)."""
    t = _epoch(start)
    if t is None:
        return None, None, None
    best = None
    for (pre, t1, t2, code), g in idx.items():
        if abs(g["start"] - t) > 12 * 3600 or not g.get("a"):
            continue
        if name_hit(g["a"], away) and name_hit(g["b"], home):
            cand = (g, t1, t2)
        elif name_hit(g["a"], home) and name_hit(g["b"], away):
            cand = (g, t2, t1)
        else:
            continue
        d = abs(g["start"] - t)
        if best is None or d < best[0]:
            best = (d, cand)
    return best[1] if best else (None, None, None)


def quotes(g: dict, mt: str, away: str, home: str, center, span: float = 3.0) -> list:
    """Pure. One game → [(line, bid_c, ask_c, sym)] HOME / OVER orientation:
    total rungs near `center`; spread rungs as the HOME line (home −L = home's S
    contract YES; home +L = the away S contract's NO, mirrored); ml = home M."""
    out = []
    if mt == "total":
        for L, q in g["T"].items():
            if center is None or abs(L - float(center)) <= span:
                out.append((L, q[1], q[2], q[0]))
    elif mt == "spread":
        for (team, L), q in g["S"].items():
            if team == home:
                hl, b, a = -L, q[1], q[2]
            elif team == away:
                hl = L
                b = round(100.0 - q[2], 2) if q[2] is not None else None
                a = round(100.0 - q[1], 2) if q[1] is not None else None
            else:
                continue
            if center is None or abs(hl - float(center)) <= span:
                out.append((hl, b, a, q[0]))
    elif mt == "ml":
        q = g["M"].get(home)
        if q:
            out.append((None, q[1], q[2], q[0]))
    return sorted(out, key=lambda x: (x[0] is None, x[0] or 0))


# THE SECOND VENUE TAKES THE SLIP'S LINE OR THE NEXT HALF POINT, NEVER A
# DIFFERENT BET (Nov 13 2026, Rob: "you put it at −10.5, when the line was
# always 13.5"): Gemini listed no −13.5 for Northwestern @ Ohio State, "nearest
# more favorable" hopped 3 points to −10.5, and a bid there looked like a bargain
# against Pinnacle's −10.5 fair. Past one point, Polymarket takes the bet.
MAX_HOP_PTS = 1.0


def rung_for(item: dict, g: dict, away: str, home: str) -> dict:
    """Pure. The Gemini contract for a slip item {market_type ml|spread|total,
    side home|away|over|under, line = the SIDE'S OWN line}: exact rung, else the
    nearest MORE favorable (the slip's rule — never a worse line). Returns
    {ok, symbol, buy_yes, line, why, yes_bid, yes_ask, ladder} or {ok: False, why}.
    buy_yes False = buy NO on that contract."""
    mt, side = item.get("market_type"), item.get("side")
    try:
        want = float(item.get("line")) if item.get("line") is not None else None
    except (TypeError, ValueError):
        want = None
    code = {"home": home, "away": away}.get(side)
    if mt == "ml":
        q = g["M"].get(code) if code else None
        if q:
            return {"ok": True, "symbol": q[0], "buy_yes": True, "line": None, "why": "exact",
                    "yes_bid": q[1], "yes_ask": q[2]}
        other = g["M"].get(home if code == away else away) if code else None
        if other:     # only the other team's contract listed: buy its NO
            return {"ok": True, "symbol": other[0], "buy_yes": False, "line": None, "why": "exact",
                    "yes_bid": other[1], "yes_ask": other[2]}
        return {"ok": False, "why": "no Gemini moneyline for this game"}
    ladder = []
    if mt == "total":
        for L, q in g["T"].items():
            ladder.append((L, q, side == "over"))
    elif mt == "spread":
        if not code:
            return {"ok": False, "why": "spread side must be home or away"}
        for (team, L), q in g["S"].items():
            if team == code:
                ladder.append((-L, q, True))           # our team −L covers = YES
            elif team in (home, away):
                ladder.append((L, q, False))           # the other team −L = our +L, NO
    else:
        return {"ok": False, "why": f"market type {mt} not on Gemini"}
    if want is None:
        return {"ok": False, "why": "item has no line"}
    if not ladder:
        return {"ok": False, "why": f"no Gemini {mt} rungs for this game"}
    lines = sorted({r[0] for r in ladder})
    exact = [r for r in ladder if abs(r[0] - want) < 0.01]
    if exact:
        ln, q, by = exact[0]
        return {"ok": True, "symbol": q[0], "buy_yes": by, "line": ln, "why": "exact",
                "yes_bid": q[1], "yes_ask": q[2], "ladder": lines}
    better = (lambda ln: ln > want) if (mt == "spread" or side == "under") else (lambda ln: ln < want)
    fav = sorted([r for r in ladder if better(r[0]) and abs(r[0] - want) <= MAX_HOP_PTS + 1e-9], key=lambda r: abs(r[0] - want))
    if not fav:
        return {"ok": False, "why": f"no Gemini rung at {want:g} or better", "ladder": lines}
    ln, q, by = fav[0]
    return {"ok": True, "symbol": q[0], "buy_yes": by, "line": ln, "why": "favorable",
            "yes_bid": q[1], "yes_ask": q[2], "ladder": lines}


def our_side_quote(yes_bid, yes_ask, buy_yes: bool) -> dict:
    """Pure. (bid, ask) in cents for the side we would BUY."""
    if buy_yes:
        return {"bid": yes_bid, "ask": yes_ask}
    return {"bid": round(100.0 - yes_ask, 2) if yes_ask is not None else None,
            "ask": round(100.0 - yes_bid, 2) if yes_bid is not None else None}


def resolve(sport: str, away: str, home: str, start_iso, item: dict) -> dict:
    """One slip item → its Gemini contract + our-side quote off the cached events
    list. {ok, symbol, buy_yes, line, why, quote{bid, ask}, ladder, event} or
    {ok: False, why}."""
    # THE BET PATH NEVER WALKS THE CATALOG (Nov 8 2026, Rob: "look up the fucking
    # game you actually want to look up… this is the third time"). It reads the
    # game out of memory; `warm()` (the handbets tick) keeps memory fresh in the
    # background. Empty or a miss → kick a background walk and answer now.
    # Off the box (a Vercel request: the slip Quote, a probe) nothing keeps the
    # catalog warm and a background thread dies with the request, so there a
    # cold cache is read once in-line. Bets execute on the box.
    if os.environ.get("CELLAR_SIDE", "").strip() != "cellar":
        evs, err = sports_events(ttl_s=60.0, stale_ok_s=1800.0)
        g, aw, hm = find_game(game_index_cached(evs), away, home, start_iso) if evs else (None, None, None)
        if g is None:
            return {"ok": False, "why": f"Gemini events unreadable: {err}" if (err and not evs) else "Gemini does not list this game"}
    else:
        with _EV_LOCK:
            evs, err = _EV_CACHE["evs"], _EV_CACHE.get("err")
        if not evs:
            warm(0.0)
            return {"ok": False, "why": f"Gemini catalog loading{(': ' + err) if err else ''}"}
        g, aw, hm = find_game(game_index_cached(evs), away, home, start_iso)
        if g is None:
            warm(60.0)
            return {"ok": False, "why": "Gemini does not list this game (as of the last catalog read)"}
    mark_hot((g.get("events") or {}).values())
    r = rung_for(item, g, aw, hm)
    if not r.get("ok"):
        return r
    r["quote"] = our_side_quote(r.get("yes_bid"), r.get("yes_ask"), bool(r["buy_yes"]))
    r["event"] = g["events"].get({"ml": "M", "spread": "S", "total": "T"}.get(item.get("market_type"), ""))
    r["away_code"], r["home_code"] = aw, hm
    return r


# ───────────────────────── the order book ─────────────────────────

def orderbook(symbol: str) -> dict:
    """GET /v1/book/{symbol} → {ok, bids:[(c, q)], asks:[(c, q)], error}, YES
    prices in cents, best first. Never cached."""
    try:
        d = _g.book(symbol)
    except Exception as e:
        return {"ok": False, "bids": [], "asks": [], "error": f"{type(e).__name__}: {e}"[:200]}

    def lv(rows, rev):
        out = []
        for r in rows or []:
            try:
                c, q = round(float(r.get("price")) * 100.0, 2), float(r.get("amount"))
                if 0 < c < 100 and q > 0:
                    out.append((c, q))
            except (TypeError, ValueError, AttributeError):
                continue
        return sorted(out, key=lambda x: (-x[0] if rev else x[0]))
    return {"ok": True, "bids": lv(d.get("bids"), True), "asks": lv(d.get("asks"), False), "error": None}


def poly_book(ob: dict) -> Optional[dict]:
    """Pure. A Gemini YES book → the `_pmm_book` shape {bids, asks, best_bid,
    best_ask}; a NO bet reads it inverted exactly like a Polymarket NO."""
    if not ob or not ob.get("ok"):
        return None
    bids, asks = list(ob.get("bids") or []), list(ob.get("asks") or [])
    return {"bids": bids, "asks": asks,
            "best_bid": bids[0][0] if bids else None,
            "best_ask": asks[0][0] if asks else None}


def opening_seat_c(our_bid_c, poly_seat_c, under_c: float = 5.0) -> Optional[float]:
    """Pure. ROB'S OPENING SEAT (Nov 8 2026: "we'll start at 5 cents cheaper than
    Polymarket… if there's no market"). With a bid on our side, the seat rule
    decides (None here). With NO bid on our side, open at Polymarket's seat −
    `under_c`, whole cents, never under 1¢. No Polymarket seat → None."""
    if our_bid_c is not None or poly_seat_c is None:
        return None
    return float(max(1, math.floor(float(poly_seat_c) - float(under_c) + 1e-9)))
