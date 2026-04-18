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

# Books we score, in display order. Must match book codes written by
# kahla-scanner/scrapers/owls.py. Order is "sharp-ish public → big retail
# → smaller books" so the table reads from left-most-likely-sharpest.
BRIER_BOOKS: list[str] = ["POLY", "PIN", "CIR", "DK", "FD", "MGM", "CAE", "HR", "NVG"]


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------

def activity(days: int = 7) -> dict[str, Any]:
    """High-level counts + freshness timestamps for the scanner UI."""
    c = _client()
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    markets = c.table("markets").select("id,sport,status", count="exact").execute()
    active_markets = c.table("markets").select("id", count="exact").eq("status", "active").execute()

    # "Tracking" = upcoming or just-started games whose outcome hasn't landed
    # yet. These are the games that will populate Brier rows once they settle.
    now_dt = datetime.now(timezone.utc)
    window_past = (now_dt - timedelta(hours=6)).isoformat()
    window_future = (now_dt + timedelta(hours=48)).isoformat()
    in_window = (
        c.table("markets").select("id,event_start,sport")
        .gte("event_start", window_past).lte("event_start", window_future)
        .eq("status", "active").limit(2000).execute()
    )
    window_rows = in_window.data or []
    window_ids = [r["id"] for r in window_rows]
    tracking_settled_ids: set[str] = set()
    if window_ids:
        # Query in chunks to avoid URL length limits
        CHUNK = 150
        for i in range(0, len(window_ids), CHUNK):
            chunk = window_ids[i:i + CHUNK]
            res = (
                c.table("market_outcomes").select("market_id")
                .in_("market_id", chunk).execute()
            )
            for r in (res.data or []):
                tracking_settled_ids.add(r["market_id"])
    tracking_rows = [r for r in window_rows if r["id"] not in tracking_settled_ids]
    tracking_by_sport: dict[str, int] = {}
    for r in tracking_rows:
        s = r.get("sport") or "?"
        tracking_by_sport[s] = tracking_by_sport.get(s, 0) + 1

    book_snaps_recent = (
        c.table("book_snapshots").select("id", count="exact").gte("captured_at", since).execute()
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

    # Latest captured_at per book. All books (including POLY) now land in
    # book_snapshots via scrapers/owls.py.
    def _first(res):
        data = res.data or []
        return (data[0] or {}).get("captured_at") if data else None

    last_seen: dict[str, str | None] = {}
    for book in BRIER_BOOKS:
        res = (
            c.table("book_snapshots").select("captured_at").eq("book", book)
            .order("captured_at", desc=True).limit(1).execute()
        )
        last_seen[book] = _first(res)

    return {
        "window_days": days,
        "markets_total": markets.count or 0,
        "markets_active": active_markets.count or 0,
        "book_snapshots_recent": book_snaps_recent.count or 0,
        "signals_recent": signals_recent.count or 0,
        "outcomes_total": outcomes_total.count or 0,
        "unmatched_open": unmatched_open.count or 0,
        "last_seen": last_seen,
        "tracking": {
            "total": len(tracking_rows),
            "by_sport": tracking_by_sport,
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

def _book_home_probs_nearest(
    c, market_id: str, target: datetime, tol_min: int = 30
) -> dict[str, float]:
    """For a given market + checkpoint time, return {book: devig'd home prob}
    for every book that has a moneyline snapshot in the ±tol_min window.

    Single query per (market, checkpoint). Python-side groups by book and
    picks the newest (home, away) pair to devig.
    """
    lower = (target - timedelta(minutes=tol_min)).isoformat()
    upper = target.isoformat()
    res = (
        c.table("book_snapshots")
        .select("book,market_type,side,implied_prob,captured_at")
        .eq("market_id", market_id)
        .eq("market_type", "moneyline")
        .gte("captured_at", lower)
        .lte("captured_at", upper)
        .order("captured_at", desc=True)
        .limit(500)
        .execute()
    )
    rows = res.data or []

    # Keep the newest (home, away) snapshot per book.
    newest: dict[tuple[str, str], dict[str, Any]] = {}
    for r in rows:
        side = r.get("side")
        if side not in ("home", "away"):
            continue
        k = (r["book"], side)
        newest.setdefault(k, r)  # first wins (rows are desc)

    # Devig each book that has both sides.
    book_sides: dict[str, dict[str, float]] = {}
    for (book, side), r in newest.items():
        p = r.get("implied_prob")
        if p is None:
            continue
        book_sides.setdefault(book, {})[side] = float(p)

    out: dict[str, float] = {}
    for book, sides in book_sides.items():
        h = sides.get("home")
        a = sides.get("away")
        if h is None or a is None or (h + a) <= 0:
            continue
        out[book] = _devig_two_way(h, a)
    return out


def brier(sport: str | None = None, days: int = 30) -> dict[str, Any]:
    """Compute Brier scores for every supported book at each checkpoint.

    Returns a dict keyed by book code (POLY, PIN, DK, FD, CIR, MGM, CAE, HR,
    NVG) with per-checkpoint mean squared error and game counts, plus the
    lowest-Brier book per checkpoint ("winner" = sharpest predictor).
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

    agg: dict[str, dict[float, dict[str, float]]] = {
        book: {h: {"n": 0, "sse": 0.0} for h in CHECKPOINTS_HOURS}
        for book in BRIER_BOOKS
    }

    for m in settled:
        actual = 1.0 if m["winning_side"] == "home" else 0.0
        start = datetime.fromisoformat(m["event_start"].replace("Z", "+00:00"))
        for h in CHECKPOINTS_HOURS:
            target = start - timedelta(hours=h)
            probs = _book_home_probs_nearest(c, m["id"], target)
            for book, p in probs.items():
                if book not in agg:
                    continue
                agg[book][h]["n"] += 1
                agg[book][h]["sse"] += (p - actual) ** 2

    summary: dict[str, Any] = {
        "sport": sport,
        "days": days,
        "n_settled": len(settled),
        "checkpoints": CHECKPOINTS_HOURS,
        "books": list(BRIER_BOOKS),
        "scores": {},
        "winner_per_checkpoint": {},
    }
    for book in BRIER_BOOKS:
        summary["scores"][book] = {}
        for h in CHECKPOINTS_HOURS:
            n = int(agg[book][h]["n"])
            b = (agg[book][h]["sse"] / n) if n else None
            summary["scores"][book][str(int(h))] = {
                "n": n,
                "brier": round(b, 5) if b is not None else None,
            }
    for h in CHECKPOINTS_HOURS:
        candidates = []
        for book in BRIER_BOOKS:
            s = summary["scores"][book][str(int(h))]
            # Require at least 5 games for a "winner" claim — a book with n=1
            # that happened to be right once would otherwise win spuriously.
            if s["brier"] is not None and s["n"] >= 5:
                candidates.append((book, s["brier"], s["n"]))
        if candidates:
            best = min(candidates, key=lambda x: x[1])
            summary["winner_per_checkpoint"][str(int(h))] = {
                "source": best[0], "brier": best[1], "n": best[2],
            }
        else:
            summary["winner_per_checkpoint"][str(int(h))] = None
    return summary
