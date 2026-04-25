"""Supabase client wrapper. Uses the service key — bypasses RLS.

Trimmed to only the helpers used by `scrapers/owls.py`:
  - client()
  - upsert_market / list_active_markets
  - insert_book_snapshots
  - list_team_aliases
"""
from __future__ import annotations

import logging
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
