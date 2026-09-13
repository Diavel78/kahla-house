"""Gemini Predictions client — the second rent venue (recon: docs/gemini-predictions-recon.md).

Standalone: stdlib + requests. No Flask, no Supabase. app.py / the cellar import
it; scripts/gemini_probe.py drives it from the command line.

Auth (developer.gemini.com/authentication/api-key): every private call builds a
JSON payload {"request": <path>, "nonce": <epoch seconds>, ...params}, base64s it
into X-GEMINI-PAYLOAD, signs THAT base64 string with HMAC-SHA384(secret) hex into
X-GEMINI-SIGNATURE, sends an EMPTY body with Content-Type text/plain. The key must
be account-scoped (prefix `account-`), Trading permission, "time-based nonce" ON
(nonce = epoch SECONDS, ±30s of server time).

Env: GEMINI_API_KEY, GEMINI_API_SECRET (box .env). Absent → private calls raise
GeminiNoCreds; public calls always work.

Venue facts baked in (verified Sep 13 2026):
  * events list payload key is `data`; limit max 500
  * `/v1/book/{symbol}` = full L2 with sizes (the legacy spot endpoint, works on GEMI- symbols)
  * timeInForce is GTC/IOC/FOK only — NO good-till-date; kickoff cancel is ours
  * `outcome` is a native yes/no side; `makerOrCancel: true` = post-only
  * WS: wss://ws.gemini.com?snapshot=-1 ; SUBSCRIBE params is an ARRAY of "symbol@stream"
  * WS trading (playground, Sep 13 2026): method `order.place` params {symbol, side BUY|SELL,
    type LIMIT|MARKET, timeInForce GTC|IOC|FOK|MOC (MOC = maker-or-cancel = post-only),
    price, quantity, clientOrderId, eventOutcome YES|NO}; `order.cancel` {orderId};
    `order.cancel_all`; `order.cancel_session`; `depth` {symbol, limit≤5000} = on-demand
    L2 snapshot. Auth headers go on the upgrade only (browsers can't; a daemon can).
  * REST place-order: the generic Gemini rule is empty body + payload header, but the PM
    spec's own curl sends the JSON body too — `detect_body_mode()` settles it per process.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from typing import Any, Iterable, Optional

import requests

REST = "https://api.gemini.com"
WS_URL = "wss://ws.gemini.com"
PM = "/v1/prediction-markets"
_TIMEOUT = 20

LIQ_MAX_SPREAD_C = 10      # from /liquidity-rewards/config (re-read live; this is the fallback)
LIQ_MIN_SIZE = 10          # contracts
LIQ_SIZE_CAP = 250
LIQ_MIN_PAYOUT_USD = 1.00
REBATE_BAND = (0.20, 0.80)  # maker rebate price band
TAKER_RATE = 0.07
MAKER_RATE = 0.0175


class GeminiError(RuntimeError):
    pass


class GeminiNoCreds(GeminiError):
    pass


def _creds() -> tuple[str, str]:
    k = (os.getenv("GEMINI_API_KEY") or "").strip()
    s = (os.getenv("GEMINI_API_SECRET") or "").strip()
    if not k or not s:
        raise GeminiNoCreds("GEMINI_API_KEY / GEMINI_API_SECRET not set")
    return k, s


def has_creds() -> bool:
    try:
        _creds()
        return True
    except GeminiNoCreds:
        return False


# --------------------------------------------------------------------------- fees
def maker_fee(contracts: float, price: float) -> float:
    """Venue formula, rounded UP to the cent."""
    import math
    return math.ceil(MAKER_RATE * contracts * price * (1 - price) * 100 - 1e-9) / 100


def maker_rebate(contracts: float, price: float, rate_mult: float = 0.30) -> float:
    """rebate = mult × taker_rate × C × P(1−P), capped at 5% of notional; band 20-80¢."""
    if not (REBATE_BAND[0] <= price <= REBATE_BAND[1]):
        return 0.0
    r = rate_mult * TAKER_RATE * contracts * price * (1 - price)
    return min(r, 0.05 * contracts * price)


# --------------------------------------------------------------------------- public
def _get(path: str, params: Optional[dict] = None) -> Any:
    r = requests.get(REST + path, params=params or None, timeout=_TIMEOUT,
                     headers={"User-Agent": "kahla-house/gemini_pm"})
    if r.status_code >= 400:
        raise GeminiError(f"GET {path} -> {r.status_code}: {r.text[:300]}")
    return r.json()


def events(category: str = "sports", status: str = "active", limit: int = 500,
           offset: int = 0, **filters) -> list[dict]:
    """One page of events. Payload key is `data`."""
    p = {"category": category, "status": status, "limit": limit, "offset": offset}
    p.update({k: v for k, v in filters.items() if v is not None})
    d = _get(f"{PM}/events", p)
    return d.get("data") or d.get("events") or []


def events_all(category: str = "sports", status: str = "active", **filters) -> list[dict]:
    out: list[dict] = []
    off = 0
    while True:
        page = events(category, status, 500, off, **filters)
        out.extend(page)
        if len(page) < 500 or off > 20000:
            return out
        off += 500


def event(event_ticker: str) -> dict:
    return _get(f"{PM}/events/{event_ticker}")


def book(symbol: str) -> dict:
    """Full L2: {"bids":[{price,amount,timestamp}], "asks":[...]} — prices in dollars."""
    return _get(f"/v1/book/{symbol}")


def trades(symbol: str, limit: int = 50) -> list[dict]:
    return _get(f"/v1/trades/{symbol}", {"limit_trades": limit})


def liquidity_config() -> dict:
    return _get(f"{PM}/liquidity-rewards/config")


def liquidity_events(category: Optional[str] = None) -> list[dict]:
    """Every active reward pool (paged). Fields: event_ticker, title, category,
    daily_pool_usd (str), pool_source, ends_at, qualifying_maker_count, pool_category_name."""
    out: list[dict] = []
    seen: set[str] = set()
    off = 0
    while True:
        p: dict[str, Any] = {"limit": 100, "offset": off}
        if category:
            p["category"] = category
        d = _get(f"{PM}/liquidity-rewards/events", p)
        rows = d.get("events") or []
        for e in rows:
            t = e.get("event_ticker")
            if t and t not in seen:
                seen.add(t)
                out.append(e)
        if len(rows) < 100 or off > 5000:
            return out
        off += 100


def maker_rebate_rates() -> list[dict]:
    return (_get(f"{PM}/maker-rebate/rates") or {}).get("rate_rules") or []


def current_rebate_mult(category: str = "Sports", now: Optional[float] = None) -> float:
    """Walk the rate rules: the category-specific rule in force wins, else the catch-all."""
    import datetime as dt
    ts = dt.datetime.fromtimestamp(now or time.time(), dt.timezone.utc)
    best = None
    for r in maker_rebate_rates():
        f = r.get("effective_from"); t = r.get("effective_to")
        try:
            f_dt = dt.datetime.fromisoformat(f.replace("Z", "+00:00")) if f else None
            t_dt = dt.datetime.fromisoformat(t.replace("Z", "+00:00")) if t else None
        except Exception:
            continue
        if f_dt and ts < f_dt:
            continue
        if t_dt and ts >= t_dt:
            continue
        cat = r.get("category")
        if cat == category:
            return (r.get("rebate_multiplier_bps") or 0) / 10000.0
        if cat is None and best is None:
            best = (r.get("rebate_multiplier_bps") or 0) / 10000.0
    return best if best is not None else 0.30


# --------------------------------------------------------------------------- private
# Recipe VERIFIED against Gemini's own client (github.com/gemini/developer-platform,
# packages/mcp-server/src/auth/signer.ts, read Sep 13 2026): nonce = floor(epoch seconds);
# payload = {"request": path, "nonce": nonce, ...fields}; base64; hex HMAC-SHA384 of the
# base64 string; POST with NO body, Content-Type text/plain, Content-Length 0. That client
# places prediction-market orders this way, so "header" is the default body mode below.
# ⚠ orderId is a 17-18 digit int64 — fine in Python, but never round-trip it through JS.
def _signed_headers(path: str, params: Optional[dict] = None) -> dict:
    key, secret = _creds()
    payload = {"request": path, "nonce": int(time.time())}
    if params:
        payload.update(params)
    b64 = base64.b64encode(json.dumps(payload).encode()).decode()
    sig = hmac.new(secret.encode(), b64.encode(), hashlib.sha384).hexdigest()
    return {
        "Content-Type": "text/plain",
        "X-GEMINI-APIKEY": key,
        "X-GEMINI-PAYLOAD": b64,
        "X-GEMINI-SIGNATURE": sig,
        "Cache-Control": "no-cache",
        "User-Agent": "kahla-house/gemini_pm",
    }


_BODY_MODE: Optional[str] = None   # "header" | "json" — detected on the first private POST


def _private(method: str, path: str, params: Optional[dict] = None,
             query: Optional[dict] = None, mode: Optional[str] = None) -> Any:
    """Private call. `params` always ride in the signed X-GEMINI-PAYLOAD (the generic
    Gemini rule: empty body, text/plain). The prediction-markets spec's own curl example
    ALSO sends the params as a JSON body with the same three headers, so the body mode is
    detected once on a non-mutating POST (orders/active) and remembered — a mutating call
    is never retried in another mode. GET params ride as the query string too."""
    global _BODY_MODE
    m = mode or _BODY_MODE or "header"
    h = _signed_headers(path, params)
    q = dict(query or {})
    data: Any = b""
    if method == "GET" and params:
        q.update(params)
    if method != "GET" and m == "json":
        h["Content-Type"] = "application/json"
        data = json.dumps(params or {})
    r = requests.request(method, REST + path, headers=h, params=q or None, data=data,
                         timeout=_TIMEOUT)
    if r.status_code >= 400:
        raise GeminiError(f"{method} {path} -> {r.status_code}: {r.text[:400]}")
    if not r.text:
        return None
    return r.json()


def ws_auth_headers() -> dict:
    """Headers for the PRIVATE websocket upgrade (samples/python/pm_order.py): the payload
    is just base64(nonce-seconds), signed the same way. Public streams need none of this."""
    key, secret = _creds()
    nonce = str(int(time.time()))
    payload = base64.b64encode(nonce.encode()).decode()
    sig = hmac.new(secret.encode(), payload.encode(), hashlib.sha384).hexdigest()
    return {"X-GEMINI-APIKEY": key, "X-GEMINI-NONCE": nonce,
            "X-GEMINI-PAYLOAD": payload, "X-GEMINI-SIGNATURE": sig}


def ws_order_place_msg(req_id: str, symbol: str, side: str, outcome: str, quantity, price,
                       post_only: bool = True, client_order_id: Optional[str] = None) -> dict:
    """The socket's order.place frame (playground + samples): side BUY|SELL, type LIMIT,
    timeInForce MOC = maker-or-cancel (post-only) else GTC, eventOutcome YES|NO."""
    p: dict[str, Any] = {"symbol": symbol, "side": side.upper(), "type": "LIMIT",
                         "timeInForce": "MOC" if post_only else "GTC",
                         "price": f"{float(price):.2f}", "quantity": f"{float(quantity):g}",
                         "eventOutcome": outcome.upper()}
    if client_order_id:
        p["clientOrderId"] = client_order_id
    return {"id": req_id, "method": "order.place", "params": p}


def detect_body_mode() -> str:
    """Find which POST shape the venue accepts using a read-only POST, cache it."""
    global _BODY_MODE
    if _BODY_MODE:
        return _BODY_MODE
    for m in ("header", "json"):
        try:
            _private("POST", f"{PM}/orders/active", {"limit": 1}, mode=m)
            _BODY_MODE = m
            return m
        except GeminiError as ex:
            last = ex
    raise last  # type: ignore[name-defined]


def terms_status() -> dict:
    return _private("GET", f"{PM}/terms/status")


def terms_accept() -> Any:
    return _private("POST", f"{PM}/terms/accept")


def active_orders(symbol: Optional[str] = None, limit: int = 100, offset: int = 0) -> list[dict]:
    p: dict[str, Any] = {"limit": limit, "offset": offset}
    if symbol:
        p["symbol"] = symbol
    d = _private("POST", f"{PM}/orders/active", p)
    return _rows(d)


def order_history(limit: int = 100, **p) -> list[dict]:
    q = {"limit": limit}; q.update(p)
    return _rows(_private("POST", f"{PM}/orders/history", q))


def positions(limit: int = 100, offset: int = 0, **p) -> list[dict]:
    q = {"limit": limit, "offset": offset}; q.update(p)
    return _rows(_private("POST", f"{PM}/positions", q))


def positions_settled(limit: int = 100, **p) -> list[dict]:
    q = {"limit": limit}; q.update(p)
    return _rows(_private("POST", f"{PM}/positions/settled", q))


def liquidity_daily(date_from: str, date_to: str) -> Any:
    """Per-day, per-event score/reward breakdown. Dates YYYY-MM-DD (Eastern)."""
    return _private("GET", f"{PM}/liquidity-rewards/summary/daily",
                    {"dateFrom": date_from, "dateTo": date_to})


def liquidity_total(date_from: Optional[str] = None, date_to: Optional[str] = None) -> Any:
    p = {k: v for k, v in (("dateFrom", date_from), ("dateTo", date_to)) if v}
    return _private("GET", f"{PM}/liquidity-rewards/summary/total", p or None)


def rebate_payouts(limit: int = 50, offset: int = 0) -> Any:
    return _private("POST", f"{PM}/maker-rebate/payouts", {"limit": limit, "offset": offset})


def rebate_total(date_from: Optional[str] = None, date_to: Optional[str] = None) -> Any:
    p = {k: v for k, v in (("dateFrom", date_from), ("dateTo", date_to)) if v}
    return _private("GET", f"{PM}/maker-rebate/summary/total", p or None)


def balances() -> Any:
    """Account balances (the generic Gemini endpoint; USD row is the predictions cash)."""
    return _private("POST", "/v1/balances")


def _ensure_mode() -> None:
    if _BODY_MODE is None:
        detect_body_mode()


def place_order(symbol: str, side: str, outcome: str, quantity: float | str,
                price: float | str, post_only: bool = True,
                tif: str = "good-til-cancel", client_order_id: Optional[str] = None) -> dict:
    """Limit order. side buy|sell, outcome yes|no, price in that outcome's own terms (0-1).
    post_only → makerOrCancel: a crossing order is CANCELLED, never filled as taker."""
    p: dict[str, Any] = {
        "symbol": symbol, "orderType": "limit", "side": side, "outcome": outcome,
        "quantity": f"{float(quantity):g}", "price": f"{float(price):.2f}",
        "timeInForce": tif, "makerOrCancel": bool(post_only),
    }
    if client_order_id:
        p["clientOrderId"] = client_order_id
    _ensure_mode()
    return _private("POST", f"{PM}/order", p)


def place_batch(orders: Iterable[dict]) -> Any:
    """1-20 orders, each the same fields as place_order's payload (already-built dicts)."""
    _ensure_mode()
    return _private("POST", f"{PM}/order/batch", {"orders": list(orders)})


def cancel_order(order_id: int | str) -> Any:
    _ensure_mode()
    return _private("POST", f"{PM}/order/cancel", {"orderId": int(order_id)})


def cancel_batch(order_ids: Iterable[int | str]) -> Any:
    _ensure_mode()
    return _private("POST", f"{PM}/order/batch/cancel",
                    {"orderIds": [int(x) for x in order_ids]})


def _rows(d: Any) -> list[dict]:
    if isinstance(d, list):
        return d
    if isinstance(d, dict):
        for k in ("data", "orders", "positions", "results", "items"):
            if isinstance(d.get(k), list):
                return d[k]
    return []


# --------------------------------------------------------------------------- helpers
def contract_index(evs: Iterable[dict]) -> dict[str, dict]:
    """instrumentSymbol → {event_ticker, title, label, strike, bestBid, bestAsk, startTime, ...}."""
    out: dict[str, dict] = {}
    for e in evs:
        for c in e.get("contracts") or []:
            s = c.get("instrumentSymbol")
            if not s:
                continue
            pr = c.get("prices") or {}
            out[s] = {
                "symbol": s, "event_ticker": e.get("ticker"), "event_title": e.get("title"),
                "label": c.get("label"), "strike": c.get("strike"),
                "best_bid": _f(pr.get("bestBid")), "best_ask": _f(pr.get("bestAsk")),
                "last": _f(pr.get("lastTradePrice")),
                "start": e.get("startTime"), "is_live": e.get("isLive"),
                "expiry": c.get("expiryDate"), "status": c.get("status"),
                "tick": _f(c.get("priceIncrement")) or 0.01,
                "qty_min": _f(c.get("quantityMinimum")) or 0.01,
                "sports_market": e.get("sportsMarket"),
            }
    return out


def _f(x: Any) -> Optional[float]:
    try:
        return None if x is None else float(x)
    except (TypeError, ValueError):
        return None


def nfl_game_ticker_parts(event_ticker: str) -> Optional[dict]:
    """NFL-2609131700-BUF-HOU-M → {code, away, home, kind}. Returns None for futures."""
    parts = (event_ticker or "").split("-")
    if len(parts) < 5 or parts[0] != "NFL" or not parts[1].isdigit():
        return None
    return {"code": parts[1], "t1": parts[2], "t2": parts[3], "kind": "-".join(parts[4:])}
