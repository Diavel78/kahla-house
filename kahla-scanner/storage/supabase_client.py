"""Supabase client wrapper. Uses the service key — bypasses RLS."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Any, Iterable

from supabase import Client, create_client

from config import config
from storage.models import BookSnapshot, Market, PolyTick, Signal, Subscriber

log = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def client() -> Client:
    return create_client(config.supabase_url, config.supabase_service_key)


# ---------- markets ----------

def upsert_market(m: Market) -> dict[str, Any]:
    """Upsert by poly_market_id when present, else insert."""
    row = m.to_row()
    if m.poly_market_id:
        res = (
            client()
            .table("markets")
            .upsert(row, on_conflict="poly_market_id")
            .execute()
        )
    else:
        res = client().table("markets").insert(row).execute()
    return (res.data or [{}])[0]


def find_market_by_poly(poly_market_id: str) -> dict[str, Any] | None:
    res = (
        client()
        .table("markets")
        .select("*")
        .eq("poly_market_id", poly_market_id)
        .limit(1)
        .execute()
    )
    return (res.data or [None])[0]


def list_active_markets(sport: str | None = None) -> list[dict[str, Any]]:
    q = client().table("markets").select("*").eq("status", "active")
    if sport:
        q = q.eq("sport", sport)
    return q.execute().data or []


def update_market_link(
    market_id: str,
    *,
    dk_event_id: str | None = None,
    fd_event_id: str | None = None,
    kalshi_ticker: str | None = None,
) -> None:
    patch: dict[str, Any] = {}
    if dk_event_id is not None:
        patch["dk_event_id"] = dk_event_id
    if fd_event_id is not None:
        patch["fd_event_id"] = fd_event_id
    if kalshi_ticker is not None:
        patch["kalshi_ticker"] = kalshi_ticker
    if not patch:
        return
    client().table("markets").update(patch).eq("id", market_id).execute()


# ---------- book_snapshots ----------

def insert_book_snapshots(snaps: Iterable[BookSnapshot]) -> None:
    rows = [s.to_row() for s in snaps]
    if not rows:
        return
    client().table("book_snapshots").insert(rows).execute()


def latest_book_snapshots(
    market_id: str, book: str, within_minutes: int = 10
) -> list[dict[str, Any]]:
    """Most recent snapshot per (market_type, side) within the freshness window."""
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=within_minutes)).isoformat()
    res = (
        client()
        .table("book_snapshots")
        .select("*")
        .eq("market_id", market_id)
        .eq("book", book)
        .gte("captured_at", cutoff)
        .order("captured_at", desc=True)
        .limit(200)
        .execute()
    )
    rows = res.data or []
    newest: dict[tuple[str, str], dict[str, Any]] = {}
    for r in rows:
        key = (r["market_type"], r["side"])
        if key not in newest:
            newest[key] = r
    return list(newest.values())


# ---------- poly_ticks ----------

def insert_poly_ticks(ticks: Iterable[PolyTick]) -> None:
    rows = [t.to_row() for t in ticks]
    if not rows:
        return
    client().table("poly_ticks").insert(rows).execute()


def latest_poly_tick(market_id: str, outcome: str | None = None) -> dict[str, Any] | None:
    q = (
        client()
        .table("poly_ticks")
        .select("*")
        .eq("market_id", market_id)
        .order("tick_ts", desc=True)
        .limit(1)
    )
    if outcome:
        q = q.eq("outcome", outcome)
    res = q.execute()
    return (res.data or [None])[0]


# ---------- signals ----------

def insert_signal(s: Signal) -> dict[str, Any]:
    res = client().table("signals").insert(s.to_row()).execute()
    return (res.data or [{}])[0]


def recent_signal_exists(market_id: str, window_minutes: int) -> bool:
    cutoff = (
        datetime.now(timezone.utc) - timedelta(minutes=window_minutes)
    ).isoformat()
    res = (
        client()
        .table("signals")
        .select("id")
        .eq("market_id", market_id)
        .gte("triggered_at", cutoff)
        .limit(1)
        .execute()
    )
    return bool(res.data)


def list_open_signals_since(cutoff: datetime) -> list[dict[str, Any]]:
    res = (
        client()
        .table("signals")
        .select("*")
        .eq("status", "open")
        .gte("triggered_at", cutoff.isoformat())
        .order("triggered_at", desc=True)
        .execute()
    )
    return res.data or []


# ---------- subscribers ----------

def list_active_subscribers() -> list[Subscriber]:
    res = (
        client()
        .table("subscribers")
        .select("*")
        .eq("active", True)
        .execute()
    )
    out: list[Subscriber] = []
    for r in res.data or []:
        out.append(
            Subscriber(
                id=r.get("id"),
                telegram_chat_id=r["telegram_chat_id"],
                handle=r.get("handle"),
                display_name=r.get("display_name"),
                sports=r.get("sports") or [],
                min_edge_pct=float(r.get("min_edge_pct") or 0),
                min_liquidity_usd=float(r.get("min_liquidity_usd") or 0),
                quiet_hours_start=r.get("quiet_hours_start"),
                quiet_hours_end=r.get("quiet_hours_end"),
                timezone=r.get("timezone") or "America/Phoenix",
                active=r.get("active", True),
            )
        )
    return out


def upsert_subscriber(sub: Subscriber) -> dict[str, Any]:
    row = {
        "telegram_chat_id": sub.telegram_chat_id,
        "handle": sub.handle,
        "display_name": sub.display_name,
        "sports": sub.sports,
        "min_edge_pct": sub.min_edge_pct,
        "min_liquidity_usd": sub.min_liquidity_usd,
        "quiet_hours_start": sub.quiet_hours_start,
        "quiet_hours_end": sub.quiet_hours_end,
        "timezone": sub.timezone,
        "active": sub.active,
    }
    res = (
        client()
        .table("subscribers")
        .upsert(row, on_conflict="telegram_chat_id")
        .execute()
    )
    return (res.data or [{}])[0]


# ---------- alerts_log ----------

def log_alert(
    signal_id: str, subscriber_id: str, delivery_status: str
) -> bool:
    """Returns True if a row was inserted (new alert), False if duplicate (already sent)."""
    try:
        client().table("alerts_log").insert(
            {
                "signal_id": signal_id,
                "subscriber_id": subscriber_id,
                "delivery_status": delivery_status,
            }
        ).execute()
        return True
    except Exception as e:  # unique index violation = already alerted
        log.debug("alerts_log insert skipped: %s", e)
        return False


# ---------- team_aliases ----------

def list_team_aliases(sport: str) -> dict[str, str]:
    """Returns alias -> canonical for the sport."""
    res = (
        client()
        .table("team_aliases")
        .select("alias,canonical")
        .eq("sport", sport)
        .execute()
    )
    return {r["alias"].lower(): r["canonical"] for r in res.data or []}


# ---------- unmatched_markets ----------

def log_unmatched(
    source: str,
    source_id: str,
    *,
    sport: str | None = None,
    event_name: str | None = None,
    event_start: datetime | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    row = {
        "source": source,
        "source_id": source_id,
        "sport": sport,
        "event_name": event_name,
        "event_start": event_start.isoformat() if event_start else None,
        "payload": payload,
    }
    row = {k: v for k, v in row.items() if v is not None}
    try:
        client().table("unmatched_markets").upsert(
            row, on_conflict="source,source_id"
        ).execute()
    except Exception as e:
        log.warning("unmatched_markets upsert failed: %s", e)
