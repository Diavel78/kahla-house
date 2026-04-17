"""Telegram fan-out.

For each new signal:
  - Load active subscribers
  - Per subscriber: filter on sport, min_edge_pct, min_liquidity, quiet_hours
  - Send message via Bot API
  - Log to alerts_log (unique index prevents double-send across restarts)
"""
from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

from alerts import dedup
from config import config
from storage import supabase_client as db
from storage.models import Signal, Subscriber

log = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"


def _send(chat_id: int, text: str, parse_mode: str = "HTML") -> bool:
    if not config.telegram_bot_token:
        log.warning("TELEGRAM_BOT_TOKEN not set; skipping send to %s", chat_id)
        return False
    url = TELEGRAM_API.format(token=config.telegram_bot_token, method="sendMessage")
    try:
        resp = httpx.post(
            url,
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": parse_mode,
                "disable_web_page_preview": False,
            },
            timeout=10.0,
        )
        if resp.status_code != 200:
            log.warning("telegram send %s failed: %s", chat_id, resp.text[:200])
            return False
        return True
    except Exception as e:
        log.warning("telegram send %s exception: %s", chat_id, e)
        return False


def notify_ops(text: str) -> None:
    """Send a Rob-only ops alert (scraper failures, crashes)."""
    if not config.telegram_ops_chat_id:
        return
    try:
        _send(int(config.telegram_ops_chat_id), f"<b>[ops]</b> {text}")
    except Exception as e:
        log.warning("notify_ops failed: %s", e)


def _within_quiet_hours(sub: Subscriber) -> bool:
    if sub.quiet_hours_start is None or sub.quiet_hours_end is None:
        return False
    try:
        now = datetime.now(ZoneInfo(sub.timezone))
    except Exception:
        now = datetime.now()
    hour = now.hour
    start, end = sub.quiet_hours_start, sub.quiet_hours_end
    if start == end:
        return False
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end  # wraps midnight


def _format(signal: dict, market: dict) -> str:
    pct = float(signal["edge_pct"])
    fade = signal["fade_side"]
    public = float(signal["public_prob"]) * 100
    sharp = float(signal["sharp_prob"]) * 100
    liq = signal.get("liquidity_usd")
    liq_line = f"\nLiquidity:      ${liq:,.0f}" if liq else ""
    poly_url = (
        f"https://polymarket.com/event/{market['poly_market_id']}"
        if market.get("poly_market_id")
        else ""
    )
    return (
        f"🎯 <b>EDGE: {fade.upper()}</b>\n"
        f"{market.get('sport', '')} · {market.get('event_name', '')}\n"
        f"Edge: <b>+{pct:.1f}%</b>\n\n"
        f"Public (DK/FD): {public:.1f}%\n"
        f"Sharp (Poly):   {sharp:.1f}%"
        f"{liq_line}\n\n"
        f"{poly_url}"
    ).strip()


def fan_out(signal_row: dict, market_row: dict) -> None:
    """Fan out one signal to every eligible subscriber."""
    signal_id = signal_row["id"]
    subs = db.list_active_subscribers()
    for sub in subs:
        if not sub.id:
            continue
        if market_row.get("sport") and sub.sports and market_row["sport"] not in sub.sports:
            continue
        if float(signal_row["edge_pct"]) < sub.min_edge_pct:
            continue
        if (
            signal_row.get("liquidity_usd") is not None
            and float(signal_row["liquidity_usd"]) < sub.min_liquidity_usd
        ):
            continue
        if _within_quiet_hours(sub):
            continue
        if not dedup.mark_and_check(signal_id, sub.id):
            continue

        text = _format(signal_row, market_row)
        ok = _send(sub.telegram_chat_id, text)
        # alerts_log unique index is the durable dedup — if insert fails, we've
        # already sent this once.
        db.log_alert(signal_id, sub.id, "sent" if ok else "failed")


def fan_out_signal(sig: Signal, market_row: dict) -> None:
    """Convenience wrapper when you have a freshly-inserted Signal + its market."""
    # We need the DB row (for id). Callers should pass the inserted row directly,
    # this helper exists for places that only hold the dataclass.
    # Not used by default flow; scheduler reads signal rows.
    raise NotImplementedError("use fan_out with inserted signal row")
