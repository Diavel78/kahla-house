"""Supabase client wrapper. Uses the service key — bypasses RLS.

Trimmed to only the helpers used by `scrapers/odds_api.py` and
`scripts/cleanup_snapshots.py`:
  - client()
  - upsert_market / list_active_markets
  - insert_book_snapshots
  - list_team_aliases
  - last_ingest_run / record_ingest_run / nearest_upcoming_event
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Any, Iterable

from supabase import Client, create_client

from config import config
from storage.models import BookSnapshot, Market

log = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def client() -> Client:
    return create_client(config.supabase_url, config.supabase_service_key)


# ---------- markets ----------

def upsert_market(m: Market) -> dict[str, Any]:
    """Insert a new market row. Renamed from upsert because the Polymarket
    cross-venue conflict path is gone — every call now just inserts.
    Caller is responsible for not creating duplicates (see
    scrapers/odds_api.py:_find_or_create_market for the dedup logic)."""
    res = client().table("markets").insert(m.to_row()).execute()
    return (res.data or [{}])[0]


def update_market_start(market_id: str, event_start: datetime) -> None:
    """Correct an existing active market's event_start in place. Mirrors the
    in-place retime odds_api did every tick (gotcha #30). Post-cutover the
    ESPN spine is the ONLY market writer, so without this a stale placeholder
    start (e.g. an old odds_api card-time) freezes the game in the past and it
    drops out of the games list before it actually starts — the UFC
    'no upcoming games' regression."""
    client().table("markets").update(
        {"event_start": event_start.isoformat()}
    ).eq("id", market_id).execute()


def list_active_markets(sport: str | None = None) -> list[dict[str, Any]]:
    q = client().table("markets").select("*").eq("status", "active")
    if sport:
        q = q.eq("sport", sport)
    return q.execute().data or []


# ---------- book_snapshots ----------

def insert_book_snapshots(snaps: Iterable[BookSnapshot]) -> None:
    rows = [s.to_row() for s in snaps]
    if not rows:
        return
    client().table("book_snapshots").insert(rows).execute()


# ---------- team aliases ----------

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


# ---------- odds_ingest_runs (adaptive cadence heartbeat) ----------

def last_ingest_run(sport: str, status: str = "ok") -> datetime | None:
    """Most recent fetched_at for (sport, status). Used by the cadence
    gate to decide whether enough time has elapsed since the last
    successful API call to fire again."""
    try:
        res = (
            client()
            .table("odds_ingest_runs")
            .select("fetched_at")
            .eq("sport", sport)
            .eq("status", status)
            .order("fetched_at", desc=True)
            .limit(1)
            .execute()
        )
        rows = res.data or []
        if not rows:
            return None
        s = rows[0]["fetched_at"].replace("Z", "+00:00")
        return datetime.fromisoformat(s)
    except Exception as e:
        log.warning("last_ingest_run(%s) failed: %s", sport, e)
        return None


def record_ingest_run(
    sport: str,
    status: str,
    cadence_min: int | None = None,
    next_game_h: float | None = None,
    events: int | None = None,
    snapshots: int | None = None,
    detail: str | None = None,
) -> None:
    """Insert a heartbeat row. Never raises — failure to log a heartbeat
    must not break the ingest pipeline."""
    row: dict[str, Any] = {"sport": sport, "status": status}
    if cadence_min is not None:
        row["cadence_min"] = cadence_min
    if next_game_h is not None:
        row["next_game_h"] = round(next_game_h, 2)
    if events is not None:
        row["events"] = events
    if snapshots is not None:
        row["snapshots"] = snapshots
    if detail:
        row["detail"] = detail[:500]
    try:
        client().table("odds_ingest_runs").insert(row).execute()
    except Exception as e:
        log.warning("record_ingest_run(%s, %s) failed: %s", sport, status, e)


def nearest_upcoming_event(sport: str, horizon_days: int = 7) -> datetime | None:
    """Earliest event_start for an active market in this sport that
    starts in the future, within `horizon_days`. None = off-season /
    nothing scheduled. Used to pick adaptive-cadence bucket."""
    now = datetime.now(timezone.utc)
    horizon = now + timedelta(days=horizon_days)
    try:
        res = (
            client()
            .table("markets")
            .select("event_start")
            .eq("sport", sport)
            .eq("status", "active")
            .gte("event_start", now.isoformat())
            .lte("event_start", horizon.isoformat())
            .order("event_start")
            .limit(1)
            .execute()
        )
        rows = res.data or []
        if not rows:
            return None
        s = rows[0]["event_start"].replace("Z", "+00:00")
        return datetime.fromisoformat(s)
    except Exception as e:
        log.warning("nearest_upcoming_event(%s) failed: %s", sport, e)
        return None
