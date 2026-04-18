"""Polymarket US poller (M1).

Operates on manually-seeded markets. Each market row in the `markets` table
carries a `poly_market_id` (a Polymarket slug) plus metadata the scanner
needs. The poller iterates every tracked market at POLY_POLL_INTERVAL and:

  1. Fetches BBO via polymarket-us SDK
  2. Computes mid = (bestBid + bestAsk) / 2
  3. Maps to the HOME side probability using the row's stored home_side
     (configured at seed time — either 'yes' or 'no')
  4. Appends a tick to poly_ticks(outcome='HOME', price=home_prob)
  5. Updates in-memory book depth cache for divergence.poly_book_depth

Why BBO mid (not real trades)? The polymarket-us SDK exposes BBO cleanly
(we already use it in app.py for the dashboard). Trade polling would be
nicer but requires SDK method names we haven't verified. BBO mid captured
every POLY_POLL_INTERVAL seconds gives us the same time-series shape the
Brier scorer needs.

CLI — seed a market for tracking:

  python -m scrapers.polymarket seed \\
      --slug chiefs-beat-bills-super-bowl \\
      --sport NFL \\
      --event "Bills @ Chiefs" \\
      --start "2026-01-19T23:30:00Z" \\
      --home-side yes    # YES token = home team winning

  python -m scrapers.polymarket list          # show tracked markets
  python -m scrapers.polymarket poll          # run one poll cycle
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any

from config import config
from signals import divergence
from storage import supabase_client as db
from storage.models import Market, PolyTick

log = logging.getLogger(__name__)

# In-memory, process-local state.
_book_depth_cache: dict[str, dict[float, float]] = {}


# ---------------------------------------------------------------------------
# SDK client
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _client():
    if not config.poly_api_key_id or not config.poly_api_secret:
        raise RuntimeError(
            "Polymarket credentials missing "
            "(set POLY_API_KEY_ID/POLY_API_SECRET or POLYMARKET_KEY_ID/POLYMARKET_SECRET_KEY)"
        )
    from polymarket_us import PolymarketUS
    return PolymarketUS(
        key_id=config.poly_api_key_id,
        secret_key=config.poly_api_secret,
    )


# ---------------------------------------------------------------------------
# BBO -> HOME probability
# ---------------------------------------------------------------------------

def _safe_float(v) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, dict):
        v = v.get("value")
        if v is None:
            return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


_LOGGED_BBO_KEYS = False


def fetch_bbo(slug: str) -> dict[str, Any] | None:
    """Fetch BBO for a market slug. Returns {bid, ask, bid_size, ask_size, mid} or None."""
    global _LOGGED_BBO_KEYS
    try:
        resp = _client().markets.bbo(slug)
    except Exception as e:
        log.warning("bbo(%s) failed: %s", slug, e)
        return None
    if not resp:
        return None

    # Normalize pydantic model -> dict; Polymarket US returns typed objects.
    if hasattr(resp, "model_dump"):
        resp_dict = resp.model_dump()
    elif isinstance(resp, dict):
        resp_dict = resp
    else:
        log.warning("bbo(%s) unexpected type: %s", slug, type(resp).__name__)
        return None

    if not _LOGGED_BBO_KEYS:
        log.info("bbo(%s) sample keys=%s sample=%s",
                 slug, sorted(resp_dict.keys()),
                 {k: resp_dict[k] for k in list(resp_dict.keys())[:6]})
        _LOGGED_BBO_KEYS = True

    def _g(*keys):
        for k in keys:
            v = resp_dict.get(k)
            if v is not None:
                return v
        return None

    bid = _safe_float(_g("bestBidPrice", "bestBid", "best_bid", "bid", "bidPrice"))
    ask = _safe_float(_g("bestAskPrice", "bestAsk", "best_ask", "ask", "askPrice"))

    # Some APIs return best quotes as nested objects: {bids: [{price, size}, ...]}
    if bid is None:
        bids = resp_dict.get("bids") or []
        if bids and isinstance(bids, list):
            bid = _safe_float((bids[0] or {}).get("price"))
    if ask is None:
        asks = resp_dict.get("asks") or []
        if asks and isinstance(asks, list):
            ask = _safe_float((asks[0] or {}).get("price"))

    bid_size = _safe_float(_g("bestBidSize", "bidSize", "bidSizeUsd", "bid_size"))
    ask_size = _safe_float(_g("bestAskSize", "askSize", "askSizeUsd", "ask_size"))
    if bid_size is None:
        bids = resp_dict.get("bids") or []
        if bids and isinstance(bids, list):
            bid_size = _safe_float((bids[0] or {}).get("size"))
    if ask_size is None:
        asks = resp_dict.get("asks") or []
        if asks and isinstance(asks, list):
            ask_size = _safe_float((asks[0] or {}).get("size"))

    if bid is None or ask is None:
        log.warning("bbo(%s): could not extract bid/ask from keys=%s",
                    slug, sorted(resp_dict.keys()))
        return None
    return {
        "bid": bid,
        "ask": ask,
        "bid_size": bid_size,
        "ask_size": ask_size,
        "mid": (bid + ask) / 2,
        "raw": resp_dict,
    }


def _home_prob(mid: float, home_side: str) -> float:
    """Map BBO mid to HOME-win probability based on which Poly side represents home.

    If the YES token is the HOME team, HOME prob = mid.
    If the NO token is the HOME team (i.e., YES=AWAY), HOME prob = 1 - mid.
    """
    return mid if (home_side or "yes").lower() == "yes" else (1.0 - mid)


# ---------------------------------------------------------------------------
# Book depth for divergence engine
# ---------------------------------------------------------------------------

def _update_depth(market_id: str, home_prob: float, ask_size_usd: float | None) -> None:
    """Seed a single-level depth entry at the current mid. Very coarse — a
    full order book walk would be better once we know the SDK shape."""
    if ask_size_usd is None:
        return
    # Round to a cent so lookups are tolerant.
    key = round(home_prob, 2)
    _book_depth_cache[market_id] = {key: float(ask_size_usd)}


def _book_depth(market_id: str, price: float) -> float:
    per_market = _book_depth_cache.get(market_id) or {}
    if not per_market:
        return 0.0
    best = min(per_market.keys(), key=lambda p: abs(p - price))
    if abs(best - price) > 0.02:
        return 0.0
    return per_market[best]


divergence.poly_book_depth = _book_depth


# ---------------------------------------------------------------------------
# Poll
# ---------------------------------------------------------------------------

def _tracked_markets() -> list[dict[str, Any]]:
    """Return active markets with a poly_market_id set (i.e., seeded for tracking)."""
    rows = db.list_active_markets()
    return [r for r in rows if r.get("poly_market_id")]


def poll_once() -> int:
    """Poll every tracked market once. Returns number of ticks inserted."""
    markets = _tracked_markets()
    if not markets:
        log.info("no tracked Polymarket markets — seed some with `scrapers.polymarket seed`")
        return 0

    inserted = 0
    for m in markets:
        slug = m["poly_market_id"]
        market_id = m["id"]
        notes = m.get("notes") if isinstance(m.get("notes"), dict) else {}
        home_side = (notes or {}).get("poly_home_side", "yes")

        bbo = fetch_bbo(slug)
        if not bbo:
            continue

        home_prob = _home_prob(bbo["mid"], home_side)
        if not (0 < home_prob < 1):
            log.warning("skip %s: computed HOME prob out of range (%.4f)", slug, home_prob)
            continue

        tick = PolyTick(
            market_id=market_id,
            outcome="HOME",
            price=round(home_prob, 4),
            # Tick 'size' here is the visible ask size (proxy for one-side depth).
            # It's not a real fill, but preserves the SDK-observed liquidity figure.
            size=round(bbo.get("ask_size") or 0.0, 2),
            side="bbo_mid",
            tick_ts=datetime.now(timezone.utc),
        )
        try:
            db.insert_poly_ticks([tick])
            inserted += 1
        except Exception as e:
            log.warning("insert_poly_ticks(%s) failed: %s", slug, e)
            continue

        _update_depth(market_id, home_prob, bbo.get("ask_size"))

    log.info("poly poll: %d ticks across %d markets", inserted, len(markets))
    return inserted


# ---------------------------------------------------------------------------
# Seed CLI — manually register a market for tracking
# ---------------------------------------------------------------------------

def seed_market(
    slug: str,
    sport: str,
    event_name: str,
    event_start: datetime,
    home_side: str = "yes",
) -> str:
    """Insert or update a markets row tagged with a Polymarket slug.

    home_side: 'yes' if the YES token represents the HOME team winning,
               'no' if the NO token does. Stored under notes.poly_home_side.

    Returns the markets.id (uuid).
    """
    if home_side.lower() not in {"yes", "no"}:
        raise ValueError("home_side must be 'yes' or 'no'")

    # Verify the market exists on Poly before we write anything.
    try:
        meta = _client().markets.retrieve_by_slug(slug)
    except Exception as e:
        raise RuntimeError(f"could not retrieve Poly market {slug!r}: {e}")

    title = (meta.get("title") or meta.get("question") or slug) if meta else slug
    log.info("seeding %s (poly title: %s)", slug, title)

    m = Market(
        sport=sport,
        event_name=event_name,
        event_start=event_start,
        poly_market_id=slug,
    )
    row = db.upsert_market(m)
    market_id = row["id"]

    # Stash home_side mapping. Supabase ignores unknown columns on insert, so
    # we store it in a per-market config table... actually, simplest: reuse
    # unmatched_markets.payload? No — add a dedicated "notes" column.
    # For M1 we write this into `markets.notes` via a direct UPDATE; if the
    # schema hasn't been migrated to add a notes column, fall back to using
    # team_aliases (hack) — nope, that's gross. Use a tiny kv on payload.
    # Simplest correct fix: attach notes via update with a jsonb column.
    try:
        db.client().table("markets").update(
            {"notes": {"poly_home_side": home_side.lower()}}
        ).eq("id", market_id).execute()
    except Exception:
        # markets.notes column may not exist on older DBs. Best-effort.
        log.warning("could not persist poly_home_side — add a `notes jsonb` column to markets")
    return market_id


def list_tracked(limit: int = 50) -> list[dict[str, Any]]:
    return _tracked_markets()[:limit]


# ---------------------------------------------------------------------------
# Auto-seed from portfolio positions
# ---------------------------------------------------------------------------

def _fetch_positions() -> list[tuple[str, dict[str, Any]]]:
    """Same call the dashboard uses — returns (slug, position_dict) pairs."""
    try:
        resp = _client().portfolio.positions()
    except Exception as e:
        log.error("positions() failed: %s", e)
        return []
    positions_map = (resp or {}).get("positions", {}) or {}
    return list(positions_map.items())


def _market_start_time(market: dict[str, Any]) -> datetime | None:
    """Try several field names Poly uses for event start."""
    for key in (
        "startTime", "startDate", "eventStartTime", "openTime", "commenceTime",
        "gameStartTime", "startsAt",
    ):
        v = market.get(key)
        if not v:
            continue
        try:
            return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        except Exception:
            continue
    # Fallback: end/resolution time (less accurate but preserves ordering)
    for key in ("endDate", "endTime", "resolutionTime", "closeTime"):
        v = market.get(key)
        if not v:
            continue
        try:
            return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        except Exception:
            continue
    return None


def _infer_sport(market: dict[str, Any], position: dict[str, Any]) -> str:
    """Best-effort sport guess from market metadata. Returns 'UNKNOWN' if unclear."""
    haystacks: list[str] = []
    for source in (market, position.get("marketMetadata") or {}):
        for key in ("category", "sport", "subcategory", "tags", "title", "question"):
            v = source.get(key)
            if isinstance(v, str):
                haystacks.append(v.lower())
            elif isinstance(v, list):
                haystacks.extend(str(x).lower() for x in v)
    hay = " ".join(haystacks)
    matches = [
        ("NFL",   ["nfl", "super bowl"]),
        ("NBA",   ["nba", "lakers", "celtics", "warriors"]),
        ("MLB",   ["mlb", "yankees", "dodgers", "red sox", "world series"]),
        ("NHL",   ["nhl", "stanley cup"]),
        ("CBB",   ["ncaa basketball", "march madness", "college basketball", "final four"]),
        ("NCAAF", ["college football", "ncaa football", "cfp"]),
        ("UFC",   ["ufc", "mma"]),
        ("SOCCER",["soccer", "epl", "la liga", "premier league", "champions league", "fifa"]),
        ("TENNIS",["tennis", "atp", "wta"]),
    ]
    for sport, needles in matches:
        if any(n in hay for n in needles):
            return sport
    return "UNKNOWN"


def _split_event_name_auto(title: str) -> tuple[str, str]:
    """Split a market title into (home, away) best-effort. Returns ('','') on failure."""
    for sep in (" @ ", " at ", " vs. ", " vs ", " v. ", " v "):
        if sep in title:
            a, b = title.split(sep, 1)
            a, b = a.strip(), b.strip()
            if sep in (" @ ", " at "):
                return b, a   # "Away @ Home"
            return a, b
    return "", ""


def autoseed(skip_expired: bool = True) -> dict[str, int]:
    """Register every Poly position as a tracked scanner market.

    For each non-expired position:
      - Look up market metadata via retrieve_by_slug
      - Infer sport, parse event name, extract start time
      - upsert into the markets table with poly_market_id=slug
      - Skip if already seeded

    Returns counts: {'seeded', 'skipped_existing', 'skipped_no_start', 'failed'}.
    """
    out = {"seeded": 0, "skipped_existing": 0, "skipped_no_start": 0, "failed": 0}
    positions = _fetch_positions()
    if not positions:
        log.warning("no positions returned — are Poly credentials set?")
        return out

    existing = {m["poly_market_id"] for m in _tracked_markets()}

    for slug, pos in positions:
        if skip_expired and pos.get("expired"):
            continue
        if slug in existing:
            out["skipped_existing"] += 1
            continue

        meta = pos.get("marketMetadata") or {}
        # Prefer slug from metadata if present (sometimes positions are keyed by id)
        real_slug = meta.get("slug") or slug

        try:
            market = _client().markets.retrieve_by_slug(real_slug) or {}
        except Exception as e:
            log.warning("retrieve_by_slug(%s) failed: %s", real_slug, e)
            market = {}

        title = (market.get("title") or meta.get("title")
                 or market.get("question") or meta.get("question") or real_slug)
        start = _market_start_time(market)
        if not start:
            log.warning("no start time for %s — skipping (fix manually later)", real_slug)
            out["skipped_no_start"] += 1
            continue

        sport = _infer_sport(market, pos)
        home, away = _split_event_name_auto(title)
        event_name = f"{away} @ {home}" if home and away else title

        try:
            m = Market(
                sport=sport,
                event_name=event_name,
                event_start=start,
                poly_market_id=real_slug,
            )
            row = db.upsert_market(m)
            db.client().table("markets").update(
                {"notes": {"poly_home_side": "yes", "auto_seeded": True,
                           "original_title": title}}
            ).eq("id", row["id"]).execute()
            out["seeded"] += 1
            log.info("seeded %s (%s) sport=%s", real_slug, event_name, sport)
        except Exception as e:
            log.warning("upsert_market(%s) failed: %s", real_slug, e)
            out["failed"] += 1

    log.info("autoseed: %s", out)
    return out


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def _parse_ts(s: str) -> datetime:
    s = s.replace("Z", "+00:00")
    return datetime.fromisoformat(s)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    p = argparse.ArgumentParser(prog="polymarket")
    sub = p.add_subparsers(dest="cmd", required=True)

    seed = sub.add_parser("seed", help="Register a Polymarket market for tracking")
    seed.add_argument("--slug", required=True, help="Polymarket market slug")
    seed.add_argument("--sport", required=True, help="NFL, NBA, MLB, ...")
    seed.add_argument("--event", required=True, help="'Away @ Home' event name")
    seed.add_argument("--start", required=True, help="ISO8601 event start (UTC)")
    seed.add_argument(
        "--home-side", choices=["yes", "no"], default="yes",
        help="Which Poly token represents HOME team winning (default: yes)",
    )

    sub.add_parser("list", help="Show tracked markets")
    sub.add_parser("poll", help="Run one poll cycle")
    auto = sub.add_parser(
        "autoseed",
        help="Register every Poly position you hold as a tracked market",
    )
    auto.add_argument(
        "--include-expired", action="store_true",
        help="Include expired positions (default: skip)",
    )

    args = p.parse_args(argv)

    if args.cmd == "seed":
        mid = seed_market(
            slug=args.slug,
            sport=args.sport.upper(),
            event_name=args.event,
            event_start=_parse_ts(args.start),
            home_side=args.home_side,
        )
        log.info("seeded market_id=%s", mid)
    elif args.cmd == "list":
        rows = list_tracked()
        print(json.dumps(rows, indent=2, default=str))
    elif args.cmd == "poll":
        n = poll_once()
        log.info("poll complete: %d ticks", n)
    elif args.cmd == "autoseed":
        stats = autoseed(skip_expired=not args.include_expired)
        log.info("autoseed complete: %s", stats)
    return 0


if __name__ == "__main__":
    sys.exit(main())
