"""Scanner review surface — reads Supabase data from kahla-scanner.

Standalone module imported by app.py. Lazy Supabase init so missing env vars
don't break the rest of the site — only /scanner-related endpoints will
error out cleanly if SUPABASE_URL / SUPABASE_SERVICE_KEY are unset.

Data model lives in the kahla-scanner repo (../kahla-scanner/supabase/
schema.sql). Same Postgres DB; this module is a read-only consumer.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any

_supabase_client = None
_supabase_import_error: str | None = None


def _client():
    global _supabase_client, _supabase_import_error
    if _supabase_client is not None:
        return _supabase_client
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_SERVICE_KEY")
    if not url or not key:
        raise RuntimeError("SUPABASE_URL / SUPABASE_SERVICE_KEY not configured")
    try:
        from supabase import create_client  # type: ignore
    except Exception as e:  # pragma: no cover
        _supabase_import_error = str(e)
        raise RuntimeError(f"supabase-py not installed: {e}")
    _supabase_client = create_client(url, key)
    return _supabase_client


def is_configured() -> bool:
    return bool(os.getenv("SUPABASE_URL") and os.getenv("SUPABASE_SERVICE_KEY"))


# ---------------------------------------------------------------------------
# Odds helpers (duplicated minimally from kahla-scanner/signals/normalize.py
# — keeping this module self-contained so Vercel doesn't need the scanner
# package on its PYTHONPATH)
# ---------------------------------------------------------------------------

def _devig_two_way(p_a: float, p_b: float) -> float:
    total = p_a + p_b
    if total <= 0:
        raise ValueError
    return p_a / total


CHECKPOINTS_HOURS: list[float] = [24, 6, 1, 0]


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------

def activity(days: int = 7) -> dict[str, Any]:
    """High-level counts + freshness timestamps for the scanner UI."""
    c = _client()
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    markets = c.table("markets").select("id,sport,status", count="exact").execute()
    active_markets = c.table("markets").select("id", count="exact").eq("status", "active").execute()

    book_snaps_recent = (
        c.table("book_snapshots").select("id", count="exact").gte("captured_at", since).execute()
    )
    poly_ticks_recent = (
        c.table("poly_ticks").select("id", count="exact").gte("captured_at", since).execute()
    )
    signals_recent = (
        c.table("signals").select("id", count="exact").gte("triggered_at", since).execute()
    )
    outcomes_total = c.table("market_outcomes").select("market_id", count="exact").execute()
    unmatched_open = (
        c.table("unmatched_markets")
        .select("id", count="exact")
        .eq("resolved", False)
        .execute()
    )

    # Latest captured_at per source (book_snapshots + poly_ticks)
    latest_dk = (
        c.table("book_snapshots").select("captured_at").eq("book", "DK")
        .order("captured_at", desc=True).limit(1).execute()
    )
    latest_fd = (
        c.table("book_snapshots").select("captured_at").eq("book", "FD")
        .order("captured_at", desc=True).limit(1).execute()
    )
    latest_poly = (
        c.table("poly_ticks").select("captured_at")
        .order("captured_at", desc=True).limit(1).execute()
    )

    def _first(res):
        data = res.data or []
        return (data[0] or {}).get("captured_at") if data else None

    return {
        "window_days": days,
        "markets_total": markets.count or 0,
        "markets_active": active_markets.count or 0,
        "book_snapshots_recent": book_snaps_recent.count or 0,
        "poly_ticks_recent": poly_ticks_recent.count or 0,
        "signals_recent": signals_recent.count or 0,
        "outcomes_total": outcomes_total.count or 0,
        "unmatched_open": unmatched_open.count or 0,
        "last_seen": {
            "DK": _first(latest_dk),
            "FD": _first(latest_fd),
            "poly": _first(latest_poly),
        },
    }


def recent_signals(limit: int = 20) -> list[dict[str, Any]]:
    c = _client()
    res = (
        c.table("signals")
        .select("*,markets(sport,event_name,event_start)")
        .order("triggered_at", desc=True)
        .limit(limit)
        .execute()
    )
    out: list[dict[str, Any]] = []
    for r in res.data or []:
        m = r.pop("markets", None) or {}
        r["sport"] = m.get("sport")
        r["event_name"] = m.get("event_name")
        r["event_start"] = m.get("event_start")
        out.append(r)
    return out


def recent_matches(limit: int = 40) -> list[dict[str, Any]]:
    c = _client()
    res = (
        c.table("markets")
        .select("id,sport,event_name,event_start,poly_market_id,dk_event_id,fd_event_id,status,created_at")
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )
    return res.data or []


def unmatched(limit: int = 40) -> list[dict[str, Any]]:
    c = _client()
    res = (
        c.table("unmatched_markets")
        .select("*")
        .eq("resolved", False)
        .order("seen_at", desc=True)
        .limit(limit)
        .execute()
    )
    return res.data or []


# ---------------------------------------------------------------------------
# Brier scoring
# ---------------------------------------------------------------------------

def _book_home_prob(snaps: list[dict[str, Any]]) -> float | None:
    home = away = None
    for s in snaps:
        if s.get("market_type") != "moneyline":
            continue
        p = s.get("implied_prob")
        if p is None:
            continue
        if s.get("side") == "home":
            home = float(p)
        elif s.get("side") == "away":
            away = float(p)
    if home is None or away is None or (home + away) <= 0:
        return None
    return _devig_two_way(home, away)


def _snap_nearest(c, market_id: str, book: str, target: datetime, tol_min: int = 30):
    lower = (target - timedelta(minutes=tol_min)).isoformat()
    upper = target.isoformat()
    res = (
        c.table("book_snapshots")
        .select("market_type,side,implied_prob,captured_at")
        .eq("market_id", market_id)
        .eq("book", book)
        .gte("captured_at", lower)
        .lte("captured_at", upper)
        .order("captured_at", desc=True)
        .limit(50)
        .execute()
    )
    rows = res.data or []
    newest: dict[tuple[str, str], dict[str, Any]] = {}
    for r in rows:
        k = (r["market_type"], r["side"])
        newest.setdefault(k, r)
    return list(newest.values())


def _poly_nearest(c, market_id: str, target: datetime, tol_min: int = 30):
    lower = (target - timedelta(minutes=tol_min)).isoformat()
    upper = target.isoformat()
    res = (
        c.table("poly_ticks")
        .select("price,tick_ts")
        .eq("market_id", market_id)
        .eq("outcome", "HOME")
        .gte("tick_ts", lower)
        .lte("tick_ts", upper)
        .order("tick_ts", desc=True)
        .limit(1)
        .execute()
    )
    return (res.data or [None])[0]


def brier(sport: str | None = None, days: int = 30) -> dict[str, Any]:
    """Compute Brier scores for poly/dk/fd at each checkpoint.

    Returns a dict with per-source, per-checkpoint mean squared error and
    game counts, plus the winning source per checkpoint.
    """
    c = _client()
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    q = (
        c.table("market_outcomes")
        .select("winning_side,resolved_at,markets(id,sport,event_name,event_start)")
        .gte("resolved_at", since)
    )
    rows = q.execute().data or []

    # Flatten
    settled: list[dict[str, Any]] = []
    for r in rows:
        m = r.get("markets") or {}
        if not m:
            continue
        if sport and m.get("sport") != sport:
            continue
        if r.get("winning_side") == "void":
            continue
        settled.append({
            "id": m["id"],
            "sport": m.get("sport"),
            "event_name": m.get("event_name"),
            "event_start": m["event_start"],
            "winning_side": r["winning_side"],
        })

    agg = {
        src: {h: {"n": 0, "sse": 0.0} for h in CHECKPOINTS_HOURS}
        for src in ("poly", "dk", "fd")
    }

    for m in settled:
        actual = 1.0 if m["winning_side"] == "home" else 0.0
        start = datetime.fromisoformat(m["event_start"].replace("Z", "+00:00"))
        for h in CHECKPOINTS_HOURS:
            target = start - timedelta(hours=h)
            tick = _poly_nearest(c, m["id"], target)
            if tick:
                p = float(tick["price"])
                agg["poly"][h]["n"] += 1
                agg["poly"][h]["sse"] += (p - actual) ** 2
            for book in ("DK", "FD"):
                snaps = _snap_nearest(c, m["id"], book, target)
                p = _book_home_prob(snaps)
                if p is None:
                    continue
                key = "dk" if book == "DK" else "fd"
                agg[key][h]["n"] += 1
                agg[key][h]["sse"] += (p - actual) ** 2

    summary: dict[str, Any] = {
        "sport": sport,
        "days": days,
        "n_settled": len(settled),
        "checkpoints": CHECKPOINTS_HOURS,
        "scores": {},
        "winner_per_checkpoint": {},
    }
    for src in ("poly", "dk", "fd"):
        summary["scores"][src] = {}
        for h in CHECKPOINTS_HOURS:
            n = agg[src][h]["n"]
            b = (agg[src][h]["sse"] / n) if n else None
            summary["scores"][src][str(int(h))] = {
                "n": n,
                "brier": round(b, 5) if b is not None else None,
            }
    for h in CHECKPOINTS_HOURS:
        candidates = []
        for src in ("poly", "dk", "fd"):
            s = summary["scores"][src][str(int(h))]
            if s["brier"] is not None and s["n"] > 0:
                candidates.append((src, s["brier"], s["n"]))
        if candidates:
            best = min(candidates, key=lambda x: x[1])
            summary["winner_per_checkpoint"][str(int(h))] = {
                "source": best[0], "brier": best[1], "n": best[2],
            }
        else:
            summary["winner_per_checkpoint"][str(int(h))] = None
    return summary
