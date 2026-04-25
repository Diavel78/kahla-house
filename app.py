#!/usr/bin/env python3
"""The Kahla House — Multi-app platform backend.

Flask app deployed on Vercel. Firebase Auth for user management,
Firestore for data storage. First app: Bet System (odds board + P&L dashboard).
"""

import os
import re
import sys
import json
import secrets
import functools
from datetime import datetime, timezone, timedelta

import firebase_admin
from firebase_admin import auth as fb_auth, credentials, firestore

from dotenv import load_dotenv
from flask import (
    Flask, render_template, request, redirect, url_for,
    jsonify, g, make_response,
)

load_dotenv()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
POLYMARKET_KEY_ID = os.getenv("POLYMARKET_KEY_ID", "")
POLYMARKET_SECRET_KEY = os.getenv("POLYMARKET_SECRET_KEY", "")

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", secrets.token_hex(32))

# ---------------------------------------------------------------------------
# Firebase Admin SDK init
# ---------------------------------------------------------------------------
_firebase_app = None
_firestore_client = None


def _init_firebase():
    global _firebase_app, _firestore_client
    if _firebase_app is not None:
        return
    sa_json = os.getenv("FIREBASE_SERVICE_ACCOUNT", "")
    if sa_json:
        try:
            sa_dict = json.loads(sa_json)
            cred = credentials.Certificate(sa_dict)
            _firebase_app = firebase_admin.initialize_app(cred)
        except Exception as e:
            print(f"Firebase init error: {e}")
            _firebase_app = firebase_admin.initialize_app()
    else:
        # Fall back to default credentials (local dev with GOOGLE_APPLICATION_CREDENTIALS)
        _firebase_app = firebase_admin.initialize_app()
    _firestore_client = firestore.client()


def get_db():
    """Return Firestore client, initializing Firebase if needed."""
    _init_firebase()
    return _firestore_client


# ---------------------------------------------------------------------------
# Supabase (read-only) for line-movement charts
# ---------------------------------------------------------------------------
_supabase_client = None


def get_supabase():
    """Return a Supabase client using the service key. Lazy-init."""
    global _supabase_client
    if _supabase_client is not None:
        return _supabase_client
    url = os.getenv("SUPABASE_URL", "").strip()
    key = os.getenv("SUPABASE_SERVICE_KEY", "").strip()
    if not url or not key:
        return None
    try:
        from supabase import create_client
        _supabase_client = create_client(url, key)
        return _supabase_client
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Auth decorators
# ---------------------------------------------------------------------------

def firebase_auth_required(f):
    """Verify Firebase ID token from Authorization header.
    Sets g.uid and g.user_data on the request context.
    """
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return jsonify({"ok": False, "error": "Missing or invalid Authorization header"}), 401

        token = auth_header[7:]
        try:
            _init_firebase()
            decoded = fb_auth.verify_id_token(token)
            g.uid = decoded["uid"]
        except Exception as e:
            return jsonify({"ok": False, "error": f"Invalid token: {e}"}), 401

        # Load user data from Firestore
        try:
            db = get_db()
            doc = db.collection("users").document(g.uid).get()
            if not doc.exists:
                return jsonify({"ok": False, "error": "User not found in database"}), 403
            g.user_data = doc.to_dict()
            if not g.user_data.get("approved"):
                return jsonify({"ok": False, "error": "Account not yet approved"}), 403
        except Exception as e:
            return jsonify({"ok": False, "error": f"Database error: {e}"}), 500

        return f(*args, **kwargs)
    return wrapper


def admin_required(f):
    """Require Firebase auth + admin role."""
    @functools.wraps(f)
    @firebase_auth_required
    def wrapper(*args, **kwargs):
        if g.user_data.get("role") != "admin":
            return jsonify({"ok": False, "error": "Admin access required"}), 403
        return f(*args, **kwargs)
    return wrapper


# ---------------------------------------------------------------------------
# Page routes (serve templates — auth handled client-side by Firebase JS SDK)
# ---------------------------------------------------------------------------

@app.route("/")
def landing():
    return render_template("index.html")


@app.route("/odds")
def odds_page():
    resp = make_response(render_template("odds.html"))
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp


@app.route("/dashboard")
def dashboard():
    """Polymarket P&L dashboard — admin only (client-side gated)."""
    return render_template("dashboard.html")


# ---------------------------------------------------------------------------
# Polymarket SDK client
# ---------------------------------------------------------------------------

def get_client():
    """Return an authenticated PolymarketUS client."""
    from polymarket_us import PolymarketUS
    if not POLYMARKET_KEY_ID or not POLYMARKET_SECRET_KEY:
        raise RuntimeError("Polymarket API credentials not configured")
    return PolymarketUS(key_id=POLYMARKET_KEY_ID, secret_key=POLYMARKET_SECRET_KEY)


def _safe_float(val):
    """Extract a float from a value, handling Amount dicts."""
    if val is None:
        return None
    if isinstance(val, dict) and "value" in val:
        val = val["value"]
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _get(obj, *keys, default=None):
    """Get first matching key from a dict."""
    for key in keys:
        if isinstance(obj, dict) and key in obj:
            return obj[key]
    return default


# ---------------------------------------------------------------------------
# Data fetching — Polymarket SDK
# ---------------------------------------------------------------------------

def fetch_positions(client):
    try:
        response = client.portfolio.positions()
        positions_map = response.get("positions", {})
        return list(positions_map.items())
    except Exception as e:
        print(f"ERROR fetching positions: {e}")
        return []


def fetch_market_price(client, market_slug):
    try:
        bbo = client.markets.bbo(market_slug)
        best_bid = _safe_float(bbo.get("bestBidPrice") or bbo.get("bid"))
        best_ask = _safe_float(bbo.get("bestAskPrice") or bbo.get("ask"))
        if best_bid is not None and best_ask is not None:
            return (best_bid + best_ask) / 2
        return best_bid or best_ask
    except Exception:
        return None


def fetch_market(client, slug_or_id):
    try:
        return client.markets.retrieve_by_slug(slug_or_id)
    except Exception:
        try:
            return client.markets.retrieve(slug_or_id)
        except Exception:
            return None


def fetch_activities(client, max_pages=20):
    all_activities = []
    cursor = None
    try:
        for _ in range(max_pages):
            params = {"limit": 100}
            if cursor:
                params["cursor"] = cursor
            response = client.portfolio.activities(params=params)
            activities = response.get("activities", [])
            all_activities.extend(activities)
            if response.get("eof", True) or not response.get("nextCursor"):
                break
            cursor = response.get("nextCursor")
    except Exception as e:
        print(f"ERROR fetching activities: {e}")
    return all_activities


def fetch_balances(client):
    try:
        response = client.account.balances()
        bal_list = response.get("balances", [])
        if bal_list:
            return bal_list[0]
        return None
    except Exception as e:
        print(f"ERROR fetching balances: {e}")
        return None


def enrich_positions(client, positions):
    enriched = []
    for slug, pos in positions:
        metadata = pos.get("marketMetadata", {})
        market_name = metadata.get("title") or metadata.get("question") or slug
        market_slug = metadata.get("slug") or slug
        event_slug = metadata.get("eventSlug") or ""
        raw_outcome = metadata.get("outcome") or ""
        team = metadata.get("team") or {}
        team_name = team.get("name", "") if isinstance(team, dict) else ""

        market_detail = fetch_market(client, market_slug)
        md = {}
        if market_detail and isinstance(market_detail, dict):
            md = market_detail.get("market", market_detail)

        question = md.get("question", "")

        if team_name and raw_outcome and re.search(r'[0-9]', raw_outcome):
            outcome = f"{team_name} {raw_outcome}"
        elif raw_outcome.lower() in ("over", "under") and question:
            total_match = re.search(r'(\d+\.?\d*)', question)
            if total_match:
                outcome = f"{raw_outcome} {total_match.group(1)}"
            else:
                outcome = raw_outcome
        elif team_name:
            outcome = team_name
        elif raw_outcome.lower() not in ("yes", "no", ""):
            outcome = raw_outcome
        elif event_slug and market_slug.startswith(event_slug + "-"):
            suffix = market_slug[len(event_slug) + 1:]
            outcome = suffix.replace("-", " ").title()
        else:
            outcome = ""

        net_position = _safe_float(pos.get("netPosition")) or 0
        quantity = abs(net_position)
        side = "YES" if net_position >= 0 else "NO"

        cost = _safe_float(pos.get("cost"))
        entry_price = (cost / quantity) if cost is not None and quantity > 0 else None

        cash_value = _safe_float(pos.get("cashValue"))
        realized = _safe_float(pos.get("realized"))

        current_price = None
        if market_slug:
            current_price = fetch_market_price(client, market_slug)
        if current_price is None:
            current_price = (cash_value / quantity) if cash_value is not None and quantity > 0 else None

        current_value = cash_value if cash_value is not None else (
            quantity * current_price if current_price is not None and quantity else None
        )

        pnl = None
        pnl_pct = None
        if current_value is not None and cost is not None:
            pnl = current_value - cost
            if realized is not None:
                pnl += realized
            if cost > 0:
                pnl_pct = (pnl / cost) * 100
        elif realized is not None:
            pnl = realized

        expired = pos.get("expired", False)

        enriched.append({
            "market_name": market_name,
            "market_slug": market_slug,
            "outcome": outcome,
            "side": side,
            "quantity": quantity,
            "entry_price": entry_price,
            "current_price": current_price,
            "current_value": current_value,
            "pnl": pnl,
            "pnl_pct": pnl_pct,
            "expired": expired,
        })

    return enriched


def compute_summary(enriched, parsed_activities, tz_offset_minutes=0):
    total_invested = 0.0
    total_current = 0.0
    open_pnl = 0.0

    for p in enriched:
        if p["entry_price"] is not None and p["quantity"]:
            total_invested += p["quantity"] * p["entry_price"]
        if p["current_value"] is not None:
            total_current += p["current_value"]
        if p["pnl"] is not None:
            open_pnl += p["pnl"]

    realized_pnl = 0.0
    resolved_wins = 0
    resolved_total = 0
    today_pnl = 0.0
    yesterday_pnl = 0.0
    maker_rewards = 0.0

    client_tz = timezone(timedelta(minutes=-tz_offset_minutes))
    now_local = datetime.now(client_tz)
    today_str = now_local.strftime("%Y-%m-%d")
    yesterday_str = (now_local - timedelta(days=1)).strftime("%Y-%m-%d")

    for act in parsed_activities:
        has_pnl = act["pnl"] is not None
        is_resolution = act["type"] == "Position Resolution"
        is_trade_close = act["type"] == "Trade" and act.get("_is_close") and has_pnl
        is_maker = act["type"] == "Transfer" and has_pnl

        if is_maker:
            maker_rewards += act["pnl"]

        if (is_resolution or is_trade_close) and has_pnl:
            realized_pnl += act["pnl"]
            resolved_total += 1
            if act["pnl"] > 0:
                resolved_wins += 1

        if (is_resolution or is_trade_close or is_maker) and has_pnl:
            ts = act.get("timestamp", "")
            act_local = ""
            if ts:
                try:
                    ts_norm = str(ts).replace(" ", "T").replace("Z", "+00:00")
                    act_dt = datetime.fromisoformat(ts_norm)
                    if act_dt.tzinfo is None:
                        act_dt = act_dt.replace(tzinfo=timezone.utc)
                    act_local = act_dt.astimezone(client_tz).strftime("%Y-%m-%d")
                except (ValueError, TypeError):
                    act_local = ""

            if act_local == today_str:
                today_pnl += act["pnl"]
            elif act_local == yesterday_str:
                yesterday_pnl += act["pnl"]

    total_pnl = open_pnl + realized_pnl + maker_rewards
    win_rate = (resolved_wins / resolved_total * 100) if resolved_total > 0 else None

    return {
        "total_positions": len([p for p in enriched if not p.get("expired")]),
        "total_invested": total_invested,
        "total_current": total_current,
        "total_pnl": total_pnl,
        "open_pnl": open_pnl,
        "realized_pnl": realized_pnl,
        "maker_rewards": maker_rewards,
        "today_pnl": today_pnl,
        "yesterday_pnl": yesterday_pnl,
        "resolved_total": resolved_total,
        "resolved_wins": resolved_wins,
        "win_rate": win_rate,
    }


def parse_balances(balances):
    if not isinstance(balances, dict):
        return {}
    return {
        "current_balance": _safe_float(balances.get("currentBalance")),
        "buying_power": _safe_float(balances.get("buyingPower")),
        "open_orders": _safe_float(balances.get("openOrders")),
        "unsettled": _safe_float(balances.get("unsettledFunds")),
    }


def _resolve_market_title(client, slug):
    try:
        market = client.markets.retrieve_by_slug(slug)
        return market.get("title", "") or market.get("question", "") or slug
    except Exception:
        return slug.replace("-", " ").replace("aec ", "").replace("asc ", "").title()


def _activity_type_label(raw_type):
    label = raw_type.replace("ACTIVITY_TYPE_", "").replace("_", " ").title()
    return label or raw_type


def parse_activities(client, activities):
    TYPE_KEY_MAP = {
        "ACTIVITY_TYPE_POSITION_RESOLUTION": "positionResolution",
        "ACTIVITY_TYPE_TRADE": "trade",
        "ACTIVITY_TYPE_ACCOUNT_BALANCE_CHANGE": "accountBalanceChange",
        "ACTIVITY_TYPE_TRANSFER": "transfer",
        "ACTIVITY_TYPE_ACCOUNT_DEPOSIT": "deposit",
        "ACTIVITY_TYPE_ACCOUNT_WITHDRAWAL": "withdrawal",
    }

    slug_to_title = {}
    parsed = []
    for act in activities:
        act_type = act.get("type", "unknown")
        detail_key = TYPE_KEY_MAP.get(act_type, "")
        detail = act.get(detail_key, {}) if detail_key else {}
        # Fallback: if mapped key not found, try to find the detail dict in the activity
        if not detail:
            for k, v in act.items():
                if k != "type" and isinstance(v, dict) and ("amount" in v or "updateTime" in v):
                    detail = v
                    break

        timestamp = detail.get("updateTime") or detail.get("timestamp") or ""
        market_slug = detail.get("marketSlug", "")

        market = ""
        side = ""
        price = None
        quantity = None
        pnl = None
        is_close = False

        if act_type == "ACTIVITY_TYPE_TRADE":
            sdk_price = _safe_float(detail.get("price"))
            quantity = _safe_float(detail.get("qty"))
            sdk_rpnl = _safe_float(detail.get("realizedPnl"))
            trade_cost = _safe_float(detail.get("cost"))
            pnl = None

            # SDK `price` field is the COMPLEMENT (YES price when trading NO,
            # or vice versa). The `cost` field / qty gives the actual per-share
            # price paid or received. Always use cost/qty.
            if trade_cost is not None and quantity and quantity > 0:
                price = trade_cost / quantity
            else:
                price = sdk_price

            t_before = detail.get("beforePosition") or {}
            t_after = detail.get("afterPosition") or {}
            bq = abs(_safe_float(t_before.get("netPosition")) or 0)
            aq = abs(_safe_float(t_after.get("netPosition")) or 0)
            is_close = sdk_rpnl is not None or bq > aq

            if market_slug:
                if market_slug not in slug_to_title:
                    slug_to_title[market_slug] = _resolve_market_title(client, market_slug)
                market = slug_to_title[market_slug]

        elif act_type == "ACTIVITY_TYPE_POSITION_RESOLUTION":
            before = detail.get("beforePosition", {})
            after = detail.get("afterPosition", {})
            meta = before.get("marketMetadata", {}) or after.get("marketMetadata", {})
            market = meta.get("title", "")
            if market_slug and market:
                slug_to_title[market_slug] = market

            side = detail.get("side", "")
            side = side.replace("POSITION_RESOLUTION_SIDE_", "")

            quantity = abs(_safe_float(before.get("netPosition")) or 0)
            cost = _safe_float(before.get("cost"))
            if cost is not None and quantity > 0:
                price = cost / quantity

            if cost is not None:
                net = _safe_float(before.get("netPosition")) or 0
                held_yes = net > 0
                yes_won = side in ("YES", "LONG")
                no_won = side in ("NO", "SHORT")
                won = (held_yes and yes_won) or (not held_yes and no_won)
                if won:
                    pnl = quantity - cost
                else:
                    pnl = -cost

        elif act_type == "ACTIVITY_TYPE_ACCOUNT_BALANCE_CHANGE":
            amount = _safe_float(detail.get("amount"))
            reason = detail.get("reason", "")
            market = reason.replace("_", " ").title() if reason else "Balance Change"
            pnl = amount

        elif act_type == "ACTIVITY_TYPE_TRANSFER":
            # Maker rewards — count as P&L income
            amount = _safe_float(detail.get("amount"))
            market = "Maker Reward"
            pnl = amount
            is_close = False

        elif act_type == "ACTIVITY_TYPE_ACCOUNT_DEPOSIT":
            # User deposits — NOT P&L, just funding
            amount = _safe_float(detail.get("amount"))
            market = "Deposit"
            pnl = None  # Don't count deposits as P&L

        elif act_type == "ACTIVITY_TYPE_ACCOUNT_WITHDRAWAL":
            amount = _safe_float(detail.get("amount"))
            market = "Withdrawal"
            pnl = None  # Don't count withdrawals as P&L

        if timestamp and "T" in str(timestamp):
            timestamp = str(timestamp).replace("T", " ")[:19]

        parsed.append({
            "timestamp": str(timestamp),
            "market": str(market) or market_slug,
            "_market_slug": market_slug,
            "_is_close": is_close if act_type == "ACTIVITY_TYPE_TRADE" else False,
            "side": str(side),
            "price": price,
            "quantity": quantity,
            "type": _activity_type_label(act_type),
            "pnl": pnl,
        })

    # Post-process: compute trade P&L from tracked average cost
    slug_positions = {}
    for i in range(len(parsed) - 1, -1, -1):
        act = parsed[i]
        if act["type"] != "Trade":
            continue
        slug = act["_market_slug"]
        if not slug or act["price"] is None or not act["quantity"]:
            continue

        if slug not in slug_positions:
            slug_positions[slug] = {"qty": 0.0, "total_cost": 0.0}
        pos = slug_positions[slug]

        if not act["_is_close"]:
            pos["qty"] += act["quantity"]
            pos["total_cost"] += act["price"] * act["quantity"]
        else:
            if pos["qty"] > 0:
                avg_cost = pos["total_cost"] / pos["qty"]
                act["pnl"] = round((act["price"] - avg_cost) * act["quantity"], 2)
                act["price"] = round(avg_cost, 4)
                sold_qty = min(act["quantity"], pos["qty"])
                pos["total_cost"] -= avg_cost * sold_qty
                pos["qty"] -= sold_qty

    for act in parsed:
        act.pop("_market_slug", None)

    return parsed


# ---------------------------------------------------------------------------
# Owls Insight API helpers
# ---------------------------------------------------------------------------

# Generic in-memory cache (also used by the Polymarket dashboard helpers).
# Name retained for backwards compat with api_my_bets / api_data.
_owls_cache = {}


# ---------------------------------------------------------------------------
# Odds Board — read latest snapshots from Supabase (cron-only architecture).
# ---------------------------------------------------------------------------
#
# As of the Owls retirement, the live page no longer hits any odds-vendor
# API. The 5/15-min `kahla-scanner/scrapers/odds_api.py` cron writes deduped
# rows to `book_snapshots`; the Odds Board reads the latest row per
# (market, book, market_type, side) and reconstructs the same JSON shape
# the frontend used to get from the Owls passthrough.
#
# Frontend impact: data freshness drops from "10s server cache" to "15-min
# cron cadence". For sharp books (PIN, CIR) this is a non-event — they
# post a line and sit on it for hours. For retail books that thrash, the
# user sees their value as of the most recent cron run.

# Owls path (lowercase)  ->  scanner sport code stored in markets.sport
_SPORT_PATH_TO_CODE = {
    "mlb":   "MLB",
    "nba":   "NBA",
    "nhl":   "NHL",
    "nfl":   "NFL",
    "ncaab": "CBB",
    "ncaaf": "NCAAF",
    "mma":   "UFC",
}

# book_snapshots.book (uppercase short code)  ->  display key the Odds Board
# template uses to look up book metadata in its `BL` map. Anything not in
# this map falls back to lowercased verbatim — no data lost.
_SHORT_TO_DISPLAY_KEY = {
    "PIN":    "pinnacle",
    "DK":     "draftkings",
    "FD":     "fanduel",
    "MGM":    "betmgm",
    "CAE":    "caesars",
    "HR":     "hardrock",
    "BOL":    "betonline",
}

# Allowlist for the Odds Board. The Odds API EU region returns dozens of
# European books we don't care about (unibet_se, winamax_fr, tipico_de,
# bovada, betsson, betclic_fr, marathonbet, fanatics, etc.) and Supabase
# still holds Owls-era short codes (POLY, NOV, KAL, STN, WG, WYN, SP, CZR).
# We hide everything not in this set both server-side (response filter) and
# in the scraper (no junk written going forward).
_ALLOWED_BOOKS = {"PIN", "DK", "FD", "MGM", "CAE", "HR", "BOL"}


def _split_event_name(name: str) -> tuple[str, str] | tuple[None, None]:
    """`event_name` convention is 'Away @ Home'. Returns (away, home) or (None, None)."""
    if not name:
        return None, None
    for sep in (" @ ", " vs ", " v. ", " vs. "):
        if sep in name:
            parts = name.split(sep, 1)
            if len(parts) == 2:
                return parts[0].strip(), parts[1].strip()
    return None, None


def _fetch_odds_from_snapshots(sport_path: str):
    """Build Odds Board events list from the latest book_snapshots in Supabase.

    Returns (events, active_books, leagues). Empty lists if Supabase isn't
    configured or the sport isn't mapped.
    """
    sb = get_supabase()
    if sb is None:
        return [], [], []

    sport_code = _SPORT_PATH_TO_CODE.get(sport_path)
    if not sport_code:
        return [], [], []

    now = datetime.now(timezone.utc)
    # Show games from 6h ago (in-progress / just-started) through the next
    # 2 days. Beyond that is future schedule clutter.
    low = (now - timedelta(hours=6)).isoformat()
    high = (now + timedelta(days=2)).isoformat()

    try:
        markets = (
            sb.table("markets")
            .select("id,event_name,event_start")
            .eq("sport", sport_code)
            .eq("status", "active")
            .gte("event_start", low)
            .lte("event_start", high)
            .order("event_start", desc=False)
            .limit(500)
            .execute()
            .data
            or []
        )
    except Exception:
        return [], [], []

    if not markets:
        return [], [], []

    market_ids = [m["id"] for m in markets]

    # Fresh snapshots from the last 90 min — covers two cron runs (30 min
    # cadence + slack). Markets without ANY snapshot in this window are
    # considered stale: either duplicate Owls-era rows the new cron didn't
    # match, or games whose books stopped posting after they started. We
    # filter those out below so the board only shows live, updated markets.
    fresh_cutoff = (now - timedelta(minutes=90)).isoformat()
    try:
        snaps = (
            sb.table("book_snapshots")
            .select("market_id,book,market_type,side,price_american,line,captured_at")
            .in_("market_id", market_ids)
            .gte("captured_at", fresh_cutoff)
            .order("captured_at", desc=True)
            .limit(50000)
            .execute()
            .data
            or []
        )
    except Exception:
        snaps = []

    # Restrict markets to those with fresh snapshots. This drops:
    #   - Owls-era duplicate markets that the new Odds API cron didn't match
    #   - Games whose books stopped pricing hours ago (board would show stale)
    fresh_market_ids = {s["market_id"] for s in snaps}
    markets = [m for m in markets if m["id"] in fresh_market_ids]
    market_ids = [m["id"] for m in markets]
    if not markets:
        return [], [], []

    # Anchor: latest pre-fresh-window row per (market, book, market_type, side)
    # not already represented. Sharp books (PIN, CIR) sit on lines for hours
    # and would otherwise drop off the board entirely.
    present = {(s["market_id"], s["book"], s["market_type"], s["side"]) for s in snaps}
    try:
        anchor_rows = (
            sb.table("book_snapshots")
            .select("market_id,book,market_type,side,price_american,line,captured_at")
            .in_("market_id", market_ids)
            .lt("captured_at", fresh_cutoff)
            .order("captured_at", desc=True)
            .limit(20000)
            .execute()
            .data
            or []
        )
    except Exception:
        anchor_rows = []

    seen: set[tuple] = set()
    for r in anchor_rows:
        key = (r["market_id"], r["book"], r["market_type"], r["side"])
        if key in present or key in seen:
            continue
        seen.add(key)
        snaps.append(r)

    # Bucket: market_id -> book -> (market_type, side) -> latest snapshot
    by_market: dict[str, dict[str, dict[tuple[str, str], dict]]] = {}
    for s in snaps:
        bucket = by_market.setdefault(s["market_id"], {}).setdefault(s["book"], {})
        key = (s["market_type"], s["side"])
        cur = bucket.get(key)
        if cur is None or s["captured_at"] > cur["captured_at"]:
            bucket[key] = s

    # Build response events
    events_out = []
    active_books: set[str] = set()
    for m in markets:
        away, home = _split_event_name(m.get("event_name", "") or "")
        if not (away and home):
            continue

        books_block: dict[str, dict] = {}
        for short_book, mkt_data in (by_market.get(m["id"]) or {}).items():
            if short_book not in _ALLOWED_BOOKS:
                continue
            display_key = _SHORT_TO_DISPLAY_KEY.get(short_book, short_book.lower())
            ml: dict = {}
            spread: dict = {}
            total: dict = {}
            for (mtype, side), s in mkt_data.items():
                price = s["price_american"]
                line = s.get("line")
                if mtype == "moneyline":
                    team = home if side == "home" else away if side == "away" else None
                    if team:
                        ml[team] = price
                elif mtype == "spread":
                    team = home if side == "home" else away if side == "away" else None
                    if team and line is not None:
                        spread[team] = {"price": price, "point": line}
                elif mtype == "total":
                    label = "Over" if side == "over" else "Under" if side == "under" else None
                    if label and line is not None:
                        total[label] = {"price": price, "point": line}
            if ml or spread or total:
                books_block[display_key] = {
                    "moneyline": ml,
                    "spread":    spread,
                    "total":     total,
                    "event_link": "",
                }
                active_books.add(display_key)

        # Skip games where no book has any market data. Without this, every
        # market in the time window renders even when the chart is empty,
        # producing rows of "--" cells that look like a bug.
        if not books_block:
            continue

        events_out.append({
            "id":            m["id"],
            "numeric_id":    m["id"],
            "sport":         sport_path,
            "home_team":     home,
            "away_team":     away,
            "commence_time": m["event_start"],
            "league":        sport_path.upper(),
            "status":        "",
            "books":         books_block,
        })

    leagues = sorted({e["league"] for e in events_out if e.get("league")})
    sorted_books = sorted(active_books, key=lambda b: (
        0 if b == "circa" else 1 if b == "pinnacle" else 2, b
    ))
    return events_out, sorted_books, leagues



# ---------------------------------------------------------------------------
# Openers API (Firestore — replaces localStorage)
# ---------------------------------------------------------------------------

@app.route("/api/openers/scanner")
@firebase_auth_required
def api_openers_scanner():
    """Scanner-backed first-seen lines for upcoming games in this sport.

    Reads `book_snapshots` for active markets and returns the earliest
    PIN/CIR snapshot per (market_type, side). The Odds Board client merges
    this into its in-memory openers map, with these values taking priority
    over the legacy Firestore openers.

    Why this exists: the legacy Firestore openers were captured client-side
    on first page load, so the "opener" was really "first time a user
    opened the page." This endpoint replaces that with the genuine
    earliest-seen line from the 5-min Owls ingest cron.

    Response: { ok, sport, events: [{home, away, commence, opener: {ml, spread, total, src}}] }
    """
    sb = get_supabase()
    if sb is None:
        return jsonify({"ok": True, "sport": request.args.get("sport"), "events": []})

    sport_owls = (request.args.get("sport") or "").lower().strip()
    sport_code = _SCANNER_SPORT_FROM_OWLS.get(sport_owls)
    if not sport_code:
        return jsonify({"ok": True, "sport": sport_owls, "events": []})

    # Trailing 6h window so games still in progress still appear, but we
    # don't drag in last week's settled markets.
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=6)).isoformat()
    try:
        markets = (
            sb.table("markets")
            .select("id,event_name,event_start")
            .eq("sport", sport_code)
            .eq("status", "active")
            .gte("event_start", cutoff)
            .limit(500)
            .execute()
            .data
            or []
        )
    except Exception as e:
        return jsonify({"ok": False, "error": f"markets query failed: {e}"}), 500

    if not markets:
        return jsonify({"ok": True, "sport": sport_owls, "events": []})

    market_ids = [m["id"] for m in markets]

    # Pull PIN/CIR snapshots ascending — first row per (market, book, mkt, side)
    # is the opener for that combo. 50K row cap is plenty for one sport's
    # active slate (typical: a few hundred markets × 8 sides × 2 books).
    try:
        snaps = (
            sb.table("book_snapshots")
            .select("market_id,book,market_type,side,price_american,line,captured_at")
            .in_("market_id", market_ids)
            .in_("book", ["PIN", "CIR"])
            .order("captured_at", desc=False)
            .limit(50000)
            .execute()
            .data
            or []
        )
    except Exception as e:
        return jsonify({"ok": False, "error": f"snapshots query failed: {e}"}), 500

    firsts: dict[tuple, dict] = {}
    for r in snaps:
        key = (r["market_id"], r["book"], r["market_type"], r["side"])
        if key not in firsts:
            firsts[key] = r

    out = []
    for m in markets:
        mid = m["id"]
        ename = m.get("event_name", "") or ""

        # event_name format: "Away @ Home"
        away_name, home_name = None, None
        for sep in [" @ ", " vs ", " v. ", " vs. "]:
            if sep in ename:
                parts = ename.split(sep, 1)
                if len(parts) == 2:
                    away_name, home_name = parts[0].strip(), parts[1].strip()
                    break
        if not (away_name and home_name):
            continue

        side_to_team = {"home": home_name, "away": away_name}
        opener: dict = {"ml": {}, "spread": {}, "total": {}, "src": None}
        used_pin = used_cir = False

        for mkt_type, mkt_key in [("moneyline", "ml"), ("spread", "spread"), ("total", "total")]:
            for side in ["home", "away", "over", "under"]:
                pin_row = firsts.get((mid, "PIN", mkt_type, side))
                cir_row = firsts.get((mid, "CIR", mkt_type, side))
                # Pick whichever was captured first (the actual opener)
                if pin_row and cir_row:
                    src_row = pin_row if pin_row["captured_at"] <= cir_row["captured_at"] else cir_row
                else:
                    src_row = pin_row or cir_row
                if not src_row:
                    continue
                if src_row["book"] == "PIN":
                    used_pin = True
                else:
                    used_cir = True

                price = src_row["price_american"]
                line = src_row.get("line")

                if mkt_type == "moneyline":
                    team = side_to_team.get(side)
                    if team is not None:
                        opener["ml"][team] = price
                elif mkt_type == "spread":
                    team = side_to_team.get(side)
                    if team is not None and line is not None:
                        opener["spread"][team] = {"price": price, "point": line}
                elif mkt_type == "total":
                    label = "Over" if side == "over" else ("Under" if side == "under" else None)
                    if label and line is not None:
                        opener["total"][label] = {"price": price, "point": line}

        if not (opener["ml"] or opener["spread"] or opener["total"]):
            continue

        if used_pin and used_cir:
            opener["src"] = "PIN+CIR"
        elif used_pin:
            opener["src"] = "PIN"
        elif used_cir:
            opener["src"] = "CIR"

        out.append({
            "home": home_name,
            "away": away_name,
            "commence": m["event_start"],
            "opener": opener,
        })

    return jsonify({"ok": True, "sport": sport_owls, "events": out, "count": len(out)})


@app.route("/api/openers", methods=["GET"])
@firebase_auth_required
def api_openers_get():
    """Retrieve opening lines for a sport from Firestore (permanent per game ID).

    Note: scanner-backed openers via /api/openers/scanner are now the
    primary source. This endpoint remains for backward compat — the
    Odds Board client merges scanner data over Firestore data, so
    Firestore is now a fallback for games predating the scanner cron.
    """
    sport = request.args.get("sport", "mlb")

    try:
        db = get_db()
        doc_id = f"openers:{sport}"
        doc = db.collection("openers").document(doc_id).get()
        if doc.exists:
            data = doc.to_dict()
            return jsonify({"ok": True, "events": data.get("events", {}), "sport": sport})
        # Migrate: try loading from old date-based docs
        old_events = _migrate_old_openers(db, sport)
        if old_events:
            return jsonify({"ok": True, "events": old_events, "sport": sport})
        return jsonify({"ok": True, "events": {}, "sport": sport})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


def _migrate_old_openers(db, sport):
    """One-time migration: merge all old date-based opener docs into the new permanent doc."""
    try:
        merged = {}
        old_docs = db.collection("openers").where("sport", "==", sport).stream()
        for doc in old_docs:
            doc_id = doc.id
            if doc_id.startswith("openers:"):
                continue  # skip new-style docs
            data = doc.to_dict()
            for eid, opener_data in data.get("events", {}).items():
                if eid not in merged:
                    merged[eid] = opener_data
        if merged:
            new_ref = db.collection("openers").document(f"openers:{sport}")
            new_ref.set({"sport": sport, "events": merged, "createdAt": firestore.SERVER_TIMESTAMP})
        return merged
    except Exception:
        return {}


@app.route("/api/openers", methods=["POST"])
@firebase_auth_required
def api_openers_save():
    """Store opening lines for a sport to Firestore (permanent per game ID).
    Body: { "sport": "mlb", "events": { ... } }
    Merges with existing data — never overrides already captured openers.
    """
    body = request.get_json(force=True)
    sport = body.get("sport", "mlb")
    new_events = body.get("events", {})

    if not new_events:
        return jsonify({"ok": True, "saved": 0})

    try:
        db = get_db()
        doc_id = f"openers:{sport}"
        doc_ref = db.collection("openers").document(doc_id)
        doc = doc_ref.get()

        if doc.exists:
            existing = doc.to_dict().get("events", {})
            added = 0
            for eid, opener_data in new_events.items():
                if eid not in existing:
                    existing[eid] = opener_data
                    added += 1
                else:
                    # Backfill missing markets (ml, spread, total) without overriding
                    updated = False
                    for mkt in ("ml", "spread", "total"):
                        existing_mkt = existing[eid].get(mkt, {})
                        new_mkt = opener_data.get(mkt, {})
                        if not existing_mkt and new_mkt:
                            existing[eid][mkt] = new_mkt
                            updated = True
                    if updated:
                        added += 1
            doc_ref.update({"events": existing})
        else:
            doc_ref.set({
                "sport": sport,
                "events": new_events,
                "createdAt": firestore.SERVER_TIMESTAMP,
            })
            added = len(new_events)

        return jsonify({"ok": True, "saved": added})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


def _cleanup_old_openers(db):
    """Legacy cleanup — no longer called but kept for reference."""
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")
        docs = db.collection("openers").where("date", "<", cutoff).stream()
        batch = db.batch()
        count = 0
        for doc in docs:
            if doc.id.startswith("openers:"):
                continue  # never delete new-style permanent docs
            batch.delete(doc.reference)
            count += 1
            if count >= 50:
                break
        if count > 0:
            batch.commit()
    except Exception as e:
        print(f"Opener cleanup error: {e}")



# ---------------------------------------------------------------------------
# API routes — Current user (lightweight role probe for client-side gating)
# ---------------------------------------------------------------------------

@app.route("/api/me")
@firebase_auth_required
def api_me():
    """Return the current user's role + approval state.
    Used by sub-pages to gate UI before loading data."""
    return jsonify({
        "ok": True,
        "uid": g.uid,
        "role": g.user_data.get("role"),
        "approved": bool(g.user_data.get("approved")),
        "displayName": g.user_data.get("displayName"),
        "email": g.user_data.get("email"),
    })


# ---------------------------------------------------------------------------
# API routes — User Preferences
# ---------------------------------------------------------------------------

@app.route("/api/preferences", methods=["GET"])
@firebase_auth_required
def api_preferences_get():
    """Return user preferences from Firestore user doc."""
    prefs = g.user_data.get("preferences", {})
    return jsonify({"ok": True, "preferences": prefs})


@app.route("/api/preferences", methods=["POST"])
@firebase_auth_required
def api_preferences_save():
    """Save user preferences to Firestore user doc.
    Body: { "preferences": { "odds_books": [...], "odds_book_order": [...], "odds_sport": "mlb" } }
    Merges with existing preferences.
    """
    try:
        body = request.get_json(force=True)
        new_prefs = body.get("preferences", {})
        if not isinstance(new_prefs, dict):
            return jsonify({"ok": False, "error": "preferences must be an object"}), 400

        # Whitelist allowed preference keys
        ALLOWED_KEYS = {"odds_books", "odds_book_order", "odds_sport"}
        filtered = {k: v for k, v in new_prefs.items() if k in ALLOWED_KEYS}

        if not filtered:
            return jsonify({"ok": False, "error": "No valid preference keys"}), 400

        db = get_db()
        doc_ref = db.collection("users").document(g.uid)
        # Merge into existing preferences
        existing_prefs = g.user_data.get("preferences", {})
        existing_prefs.update(filtered)
        doc_ref.update({"preferences": existing_prefs})

        return jsonify({"ok": True, "saved": list(filtered.keys())})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ---------------------------------------------------------------------------
# API routes — Odds
# ---------------------------------------------------------------------------

@app.route("/api/odds")
@firebase_auth_required
def api_odds():
    """Return events with latest snapshots from Supabase. Cron-only — no
    live odds-vendor API call here. Cron writes to book_snapshots every 15
    min via kahla-scanner/scrapers/odds_api.py."""
    sport = request.args.get("sport", "mlb")
    events, books, leagues = _fetch_odds_from_snapshots(sport)
    return jsonify({
        "ok":            True,
        "cached":        False,
        "sport":         sport,
        "events":        events,
        "books":         books,
        "leagues":       leagues,
        "meta_message":  "",
        "errors":        [],
    })


@app.route("/api/my-bets")
@admin_required
def api_my_bets():
    import time
    cache_key = "my_bets"
    now = time.time()
    cached = _owls_cache.get(cache_key)
    if cached and (now - cached["ts"]) < 60:
        return jsonify(cached["data"])

    bets = []
    try:
        client = get_client()
        positions = fetch_positions(client)
        for slug, pos in positions:
            if pos.get("expired"):
                continue
            net = _safe_float(pos.get("netPosition")) or 0
            if abs(net) < 0.01:
                continue

            meta = pos.get("marketMetadata", {})
            market_name = meta.get("title", "")
            market_slug = meta.get("slug") or slug
            team = meta.get("team") or {}
            team_name = team.get("name", "") if isinstance(team, dict) else ""
            raw_outcome = meta.get("outcome", "")
            event_slug = meta.get("eventSlug", "")

            pick = raw_outcome
            if team_name and raw_outcome and re.search(r'[0-9]', raw_outcome):
                pick = f"{team_name} {raw_outcome}"
            elif raw_outcome.lower() in ("over", "under"):
                try:
                    md_raw = fetch_market(client, market_slug)
                    md = md_raw.get("market", md_raw) if md_raw and isinstance(md_raw, dict) else {}
                    question = md.get("question", "")
                    total_match = re.search(r'(\d+\.?\d*)', question)
                    if total_match:
                        pick = f"{raw_outcome} {total_match.group(1)}"
                except Exception:
                    pass
            elif team_name:
                pick = team_name

            cost = _safe_float(pos.get("cost"))
            quantity = abs(net)
            entry_price = (cost / quantity) if cost and quantity > 0 else None
            entry_american = None
            if entry_price and 0 < entry_price < 1:
                if entry_price >= 0.5:
                    entry_american = round(-entry_price / (1 - entry_price) * 100)
                else:
                    entry_american = round((1 - entry_price) / entry_price * 100)

            bets.append({
                "slug": slug,
                "event_slug": event_slug,
                "market_name": market_name,
                "team_name": team_name,
                "pick": pick,
                "side": "YES" if net > 0 else "NO",
                "entry_american": entry_american,
            })
    except Exception as e:
        return jsonify({"ok": False, "bets": [], "error": str(e)})

    result = {"ok": True, "bets": bets}
    _owls_cache[cache_key] = {"data": result, "ts": now}
    return jsonify(result)


# ---------------------------------------------------------------------------
# API routes — Dashboard
# ---------------------------------------------------------------------------

@app.route("/api/data")
@admin_required
def api_data():
    errors = []
    now = datetime.now(timezone.utc)

    enriched = []
    parsed_acts = []
    balance = 0.0

    try:
        client = get_client()

        try:
            positions = fetch_positions(client)
        except Exception as e:
            positions = []
            errors.append(f"positions: {e}")

        try:
            enriched = enrich_positions(client, positions)
        except Exception as e:
            errors.append(f"enrich: {e}")

        activities = []
        try:
            activities = fetch_activities(client)
        except Exception as e:
            errors.append(f"activities: {e}")

        balances = None
        try:
            balances = fetch_balances(client)
        except Exception as e:
            errors.append(f"balances: {e}")

        parsed_acts = parse_activities(client, activities)
        bal = parse_balances(balances)
        balance = bal.get("current_balance") or 0.0

    except Exception as e:
        errors.append(f"client: {e}")
        bal = {}

    CUTOFF_DATE = "2026-03-01"
    parsed_acts = [a for a in parsed_acts if a.get("timestamp", "") >= CUTOFF_DATE]
    parsed_acts.sort(key=lambda a: a.get("timestamp", ""), reverse=True)

    open_positions = [p for p in enriched if not p.get("expired")]
    closed_positions = [a for a in parsed_acts
                        if a["type"] == "Position Resolution"
                        or (a["type"] == "Trade" and a.get("_is_close") and a.get("pnl") is not None)
                        or (a["type"] == "Transfer" and a.get("pnl") is not None)]

    tz_offset = request.args.get("tz", 0, type=int)
    summary = compute_summary(enriched, parsed_acts, tz_offset_minutes=tz_offset)

    for act in parsed_acts:
        act.pop("_is_close", None)

    return jsonify({
        "ok": True,
        "timestamp": now.isoformat(),
        "positions": open_positions,
        "closed_positions": closed_positions,
        "balances": {"current_balance": balance},
        "summary": summary,
        "errors": errors,
    })


# ---------------------------------------------------------------------------
# Line-movement history (powers the per-game chart modal on /odds)
# ---------------------------------------------------------------------------

# Owls API sport path  ->  scanner sport code stored in markets.sport
_SCANNER_SPORT_FROM_OWLS = {
    "mlb": "MLB",
    "nba": "NBA",
    "nhl": "NHL",
    "nfl": "NFL",
    "ncaab": "CBB",
    "ncaaf": "NCAAF",
    "mma": "UFC",
}

# Books we surface on the chart. POLY excluded — its prices are 0-1
# (probability) not American odds. NVG (Novig) excluded too — not legal
# in Rob's state, no point graphing it.
_CHART_BOOKS = ["PIN", "CIR", "DK", "FD", "MGM", "CAE", "HR"]

# `since` query param  ->  timedelta. Used to bound the snapshot query.
_HISTORY_SPANS = {
    "15m":  timedelta(minutes=15),
    "30m":  timedelta(minutes=30),
    "1h":   timedelta(hours=1),
    "6h":   timedelta(hours=6),
    "12h":  timedelta(hours=12),
    "24h":  timedelta(hours=24),
    "all":  None,  # no lower bound
}


def _norm_team(name: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace. Mirrors the
    same normalization used by the scanner ingest so team-name matching
    against `markets.event_name` works without an alias table lookup."""
    if not name:
        return ""
    s = re.sub(r"[^\w\s]", " ", name.lower())
    return re.sub(r"\s+", " ", s).strip()


def _parse_iso(ts: str) -> datetime | None:
    if not ts:
        return None
    try:
        # Tolerate the trailing 'Z' that JS toISOString emits
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None


@app.route("/api/odds/history")
@firebase_auth_required
def api_odds_history():
    """Return book_snapshots history for one event's market.

    Query params:
      sport     — Owls path: mlb|nba|nhl|nfl|ncaab|ncaaf|mma
      home, away — team names exactly as Odds Board has them
      commence  — ISO timestamp of event start (Owls commence_time)
      market    — ml|spread|total  (defaults to ml)
      since     — 15m|30m|1h|6h|12h|24h|all  (defaults to 24h)

    Response:
      { ok, market_id, market, since_iso, books: {
          BOOK_CODE: { side: [{ts, price, line}, ...], ... }, ...
      } }

    Books returned: PIN, CIR, DK, FD, MGM, CAE, HR. POLY + NVG excluded.
    """
    sb = get_supabase()
    if sb is None:
        return jsonify({"ok": False, "error": "Supabase not configured"}), 503

    sport_owls = (request.args.get("sport") or "").lower().strip()
    sport_code = _SCANNER_SPORT_FROM_OWLS.get(sport_owls)
    if not sport_code:
        return jsonify({"ok": False, "error": f"unsupported sport: {sport_owls}"}), 400

    home_raw = (request.args.get("home") or "").strip()
    away_raw = (request.args.get("away") or "").strip()
    commence = _parse_iso(request.args.get("commence", ""))
    if not home_raw or not away_raw or not commence:
        return jsonify({"ok": False, "error": "home, away, commence required"}), 400

    market_in = (request.args.get("market") or "ml").lower()
    market_type = {"ml": "moneyline", "spread": "spread", "total": "total"}.get(market_in)
    if not market_type:
        return jsonify({"ok": False, "error": f"bad market: {market_in}"}), 400

    span_key = (request.args.get("since") or "24h").lower()
    if span_key not in _HISTORY_SPANS:
        return jsonify({"ok": False, "error": f"bad since: {span_key}"}), 400
    span = _HISTORY_SPANS[span_key]

    # ---- 1. Find the matching market row ----
    home_n = _norm_team(home_raw)
    away_n = _norm_team(away_raw)
    window = timedelta(minutes=30)
    try:
        rows = (
            sb.table("markets")
            .select("id,event_name,event_start")
            .eq("sport", sport_code)
            .eq("status", "active")
            .gte("event_start", (commence - window).isoformat())
            .lte("event_start", (commence + window).isoformat())
            .limit(50)
            .execute()
            .data
            or []
        )
    except Exception as e:
        return jsonify({"ok": False, "error": f"markets query failed: {e}"}), 500

    market_id = None
    for r in rows:
        ev = _norm_team(r.get("event_name", ""))
        if home_n and away_n and home_n in ev and away_n in ev:
            market_id = r["id"]
            break

    if not market_id:
        return jsonify({
            "ok": True, "market_id": None, "market": market_type,
            "since_iso": None, "books": {},
        })

    # ---- 2. Pull snapshots for that market_id + market_type ----
    # Two queries:
    #   (A) all rows within the requested time window
    #   (B) the latest row at-or-before the cutoff per (book, side) — these are
    #       "anchor" samples so a book that hasn't priced inside the window
    #       (typical for sharp books PIN/CIR which post a line and sit) still
    #       gets a flat line drawn across the entire range. Anchor rows have
    #       their captured_at rewritten to the cutoff before merging so the
    #       line visually starts at the left edge with the carried-forward Y.
    since_iso = None
    try:
        q = (
            sb.table("book_snapshots")
            .select("book,side,price_american,line,captured_at")
            .eq("market_id", market_id)
            .eq("market_type", market_type)
            .in_("book", _CHART_BOOKS)
            .order("captured_at", desc=False)
            .limit(5000)
        )
        if span is not None:
            since_iso = (datetime.now(timezone.utc) - span).isoformat()
            q = q.gte("captured_at", since_iso)
        snaps = q.execute().data or []

        if since_iso is not None:
            anchor_rows = (
                sb.table("book_snapshots")
                .select("book,side,price_american,line,captured_at")
                .eq("market_id", market_id)
                .eq("market_type", market_type)
                .in_("book", _CHART_BOOKS)
                .lt("captured_at", since_iso)
                .order("captured_at", desc=True)
                .limit(2000)
                .execute()
                .data
                or []
            )
            # First (most recent) row per (book, side) wins.
            present = {(s["book"], s["side"]) for s in snaps}
            seen: set[tuple[str, str]] = set()
            for r in anchor_rows:
                key = (r["book"], r["side"])
                if key in seen or key in present:
                    continue
                seen.add(key)
                # Pin the anchor at the window's left edge so the line draws
                # across the full range starting from this carry-forward value.
                r["captured_at"] = since_iso
                snaps.insert(0, r)
    except Exception as e:
        return jsonify({"ok": False, "error": f"snapshots query failed: {e}"}), 500

    # ---- 3. Group into { book: { side: [{ts, price, line}, ...] } } ----
    books: dict[str, dict[str, list[dict]]] = {}
    for s in snaps:
        bk = s["book"]
        side = s["side"]
        books.setdefault(bk, {}).setdefault(side, []).append({
            "ts": s["captured_at"],
            "price": s["price_american"],
            "line": s.get("line"),
        })

    return jsonify({
        "ok": True,
        "market_id": market_id,
        "market": market_type,
        "since_iso": since_iso,
        "books": books,
    })


# ---------------------------------------------------------------------------
# Debug routes (admin only)
# ---------------------------------------------------------------------------


@app.route("/api/raw")
@admin_required
def api_raw():
    try:
        client = get_client()
    except Exception as e:
        return jsonify({"error": f"Client init: {e}"}), 500

    raw = {}
    for name, call in [
        ("positions", lambda: client.portfolio.positions()),
        ("balances", lambda: client.account.balances()),
        ("activities", lambda: client.portfolio.activities()),
    ]:
        try:
            result = call()
            raw[name] = result
        except Exception as e:
            raw[name] = {"_error": str(e), "_type": type(e).__name__}

    return jsonify(raw)


@app.route("/api/debug-deposits")
@admin_required
def api_debug_deposits():
    """Show all balance changes with their reasons — helps identify maker rewards vs deposits."""
    try:
        client = get_client()
        activities = fetch_activities(client)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    # Collect all activity types to see what exists
    type_counts = {}
    balance_changes = []
    for act in activities:
        act_type = act.get("type", "unknown")
        type_counts[act_type] = type_counts.get(act_type, 0) + 1

        # Try multiple possible keys for balance changes
        if "balance" in act_type.lower() or "account" in act_type.lower() or "deposit" in act_type.lower() or "transfer" in act_type.lower():
            balance_changes.append({
                "type": act_type,
                "keys": list(act.keys()),
                "raw": {k: v for k, v in act.items() if k != "type"},
            })

        # Also check for accountBalanceChange key regardless of type
        if act.get("accountBalanceChange"):
            detail = act["accountBalanceChange"]
            balance_changes.append({
                "type": act_type,
                "timestamp": detail.get("updateTime") or detail.get("timestamp", ""),
                "amount": detail.get("amount"),
                "reason": detail.get("reason", ""),
                "raw_keys": list(detail.keys()),
            })

    balance_changes.sort(key=lambda x: str(x.get("timestamp", "")), reverse=True)
    return jsonify({
        "ok": True,
        "total_activities": len(activities),
        "activity_types": type_counts,
        "balance_changes": balance_changes,
    })


@app.route("/api/debug-snap")
@admin_required
def api_debug_snap():
    """Diagnostic: counts markets and book_snapshots in Supabase, plus a
    sample of recent rows. Used to debug why the Odds Board shows empty."""
    sb = get_supabase()
    if sb is None:
        return jsonify({"ok": False, "error": "Supabase not configured"}), 503

    sport_path = (request.args.get("sport") or "mlb").lower()
    sport_code = _SPORT_PATH_TO_CODE.get(sport_path, sport_path.upper())

    now = datetime.now(timezone.utc)
    low = (now - timedelta(hours=6)).isoformat()
    high = (now + timedelta(days=2)).isoformat()

    out: dict = {
        "ok": True,
        "now_iso": now.isoformat(),
        "sport_path": sport_path,
        "sport_code": sport_code,
        "window_low": low,
        "window_high": high,
    }

    # Total markets for this sport (no time filter)
    try:
        all_mkts = (
            sb.table("markets")
            .select("id,event_name,event_start,status")
            .eq("sport", sport_code)
            .order("event_start", desc=True)
            .limit(20)
            .execute()
            .data
            or []
        )
        out["recent_markets_any_status_count"] = len(all_mkts)
        out["recent_markets_sample"] = all_mkts[:10]
    except Exception as e:
        out["markets_error"] = str(e)

    # Markets matching the same query Flask /api/odds uses
    try:
        windowed = (
            sb.table("markets")
            .select("id,event_name,event_start")
            .eq("sport", sport_code)
            .eq("status", "active")
            .gte("event_start", low)
            .lte("event_start", high)
            .order("event_start", desc=False)
            .limit(500)
            .execute()
            .data
            or []
        )
        out["windowed_markets_count"] = len(windowed)
        out["windowed_markets_sample"] = windowed[:10]
    except Exception as e:
        out["windowed_markets_error"] = str(e)

    # Recent snapshots count
    try:
        recent_snap = (
            sb.table("book_snapshots")
            .select("market_id,book,market_type,side,price_american,captured_at")
            .order("captured_at", desc=True)
            .limit(10)
            .execute()
            .data
            or []
        )
        out["recent_snapshots_sample"] = recent_snap
    except Exception as e:
        out["snapshots_error"] = str(e)

    # What does /api/odds actually return?
    try:
        evs, books, leagues = _fetch_odds_from_snapshots(sport_path)
        out["api_odds_events_count"] = len(evs)
        out["api_odds_books"] = books
        out["api_odds_first_event"] = evs[0] if evs else None
    except Exception as e:
        out["api_odds_error"] = str(e)

    return jsonify(out)


@app.route("/debug-snap")
def debug_snap_page():
    """Auth'd browser-friendly wrapper for /api/debug-snap."""
    sport = request.args.get("sport", "mlb")
    return ('''<!DOCTYPE html><html><head>
    <script src="https://www.gstatic.com/firebasejs/10.12.0/firebase-app-compat.js"></script>
    <script src="https://www.gstatic.com/firebasejs/10.12.0/firebase-auth-compat.js"></script>
    <script>firebase.initializeApp({apiKey:"AIzaSyDQbjlc7VIYmFjbhq119Cl1-JhuXwKq0fY",authDomain:"kahla-house.firebaseapp.com",projectId:"kahla-house"});</script>
    </head><body style="background:#0b0e13;color:#e2e8f0;font-family:monospace;padding:16px;font-size:11px">
    <h2 style="color:#f59e0b">Supabase diagnostic — sport=''' + sport + '''</h2>
    <pre id="out" style="white-space:pre-wrap;word-break:break-word">Loading...</pre>
    <script>
    firebase.auth().onAuthStateChanged(async u => {
        if (!u) { document.getElementById("out").textContent = "Not logged in. Go to / first."; return; }
        try {
            const t = await u.getIdToken();
            const r = await fetch("/api/debug-snap?sport=''' + sport + '''", {headers:{"Authorization":"Bearer "+t}});
            const d = await r.json();
            document.getElementById("out").textContent = JSON.stringify(d, null, 2);
        } catch (e) {
            document.getElementById("out").textContent = "ERROR: " + e.message;
        }
    });
    </script></body></html>''')


@app.route("/debug-deposits")
def debug_deposits_page():
    """Page that shows all balance changes with auth."""
    return '''<!DOCTYPE html><html><head>
    <script src="https://www.gstatic.com/firebasejs/10.12.0/firebase-app-compat.js"></script>
    <script src="https://www.gstatic.com/firebasejs/10.12.0/firebase-auth-compat.js"></script>
    <script>firebase.initializeApp({apiKey:"AIzaSyDQbjlc7VIYmFjbhq119Cl1-JhuXwKq0fY",authDomain:"kahla-house.firebaseapp.com",projectId:"kahla-house"});</script>
    </head><body style="background:#0b0e13;color:#e2e8f0;font-family:monospace;padding:20px">
    <h2 style="color:#f59e0b;margin-bottom:16px">Balance Changes (Deposits / Maker Rewards)</h2>
    <pre id="out">Loading...</pre>
    <script>
    firebase.auth().onAuthStateChanged(async u => {
        if (!u) { document.getElementById("out").textContent = "Not logged in. Go to / first."; return; }
        const t = await u.getIdToken();
        const r = await fetch("/api/debug-deposits", {headers:{"Authorization":"Bearer "+t}});
        const d = await r.json();
        document.getElementById("out").textContent = JSON.stringify(d, null, 2);
    });
    </script></body></html>'''


@app.route("/debug")
def debug_page():
    """Simple page that makes an authenticated debug-trades call."""
    slug = request.args.get("slug", "")
    return f'''<!DOCTYPE html><html><head>
    <script src="https://www.gstatic.com/firebasejs/10.12.0/firebase-app-compat.js"></script>
    <script src="https://www.gstatic.com/firebasejs/10.12.0/firebase-auth-compat.js"></script>
    <script>firebase.initializeApp({{apiKey:"AIzaSyDQbjlc7VIYmFjbhq119Cl1-JhuXwKq0fY",authDomain:"kahla-house.firebaseapp.com",projectId:"kahla-house"}});</script>
    </head><body style="background:#0b0e13;color:#e2e8f0;font-family:monospace;padding:20px">
    <pre id="out">Loading...</pre>
    <script>
    firebase.auth().onAuthStateChanged(async u => {{
        if (!u) {{ document.getElementById("out").textContent = "Not logged in. Go to / first."; return; }}
        const t = await u.getIdToken();
        const r = await fetch("/api/debug-trades?slug={slug}", {{headers:{{"Authorization":"Bearer "+t}}}});
        const d = await r.json();
        document.getElementById("out").textContent = JSON.stringify(d, null, 2);
    }});
    </script></body></html>'''


@app.route("/api/debug-trades")
@admin_required
def api_debug_trades():
    try:
        client = get_client()
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    try:
        all_acts = fetch_activities(client)
    except Exception as e:
        return jsonify({"error": f"activities: {e}"}), 500

    by_slug = {}
    for act in all_acts:
        if act.get("type") != "ACTIVITY_TYPE_TRADE":
            continue
        detail = act.get("trade", {})
        slug = detail.get("marketSlug", "unknown")
        rpnl = detail.get("realizedPnl")
        t_before = detail.get("beforePosition") or {}
        t_after = detail.get("afterPosition") or {}
        entry = {
            "timestamp": detail.get("updateTime") or detail.get("timestamp"),
            "price": detail.get("price"),
            "qty": detail.get("qty"),
            "cost": detail.get("cost"),
            "realizedPnl": rpnl,
            "is_sell": rpnl is not None,
            "before_netPosition": t_before.get("netPosition"),
            "before_cost": t_before.get("cost"),
            "after_netPosition": t_after.get("netPosition"),
            "after_cost": t_after.get("cost"),
        }
        if rpnl is not None:
            entry["costBasis"] = detail.get("costBasis")
            entry["originalPrice"] = detail.get("originalPrice")
        if slug not in by_slug:
            by_slug[slug] = []
        by_slug[slug].append(entry)

    sell_slugs = {s: trades for s, trades in by_slug.items()
                  if any(t["is_sell"] for t in trades)}

    slug_filter = request.args.get("slug", "").lower()
    if slug_filter:
        sell_slugs = {s: t for s, t in sell_slugs.items() if slug_filter in s.lower()}

    return jsonify({
        "total_slugs": len(by_slug),
        "slugs_with_sells": len(sell_slugs),
        "trades_by_slug": sell_slugs,
    })


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    debug = os.getenv("FLASK_DEBUG", "0") == "1"
    app.run(host="0.0.0.0", port=port, debug=debug)
