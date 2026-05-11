#!/usr/bin/env python3
"""The Kahla House — Multi-app platform backend.

Flask app deployed on Vercel. Firebase Auth for user management,
Firestore for data storage. First app: Bet System (odds board + P&L dashboard).
"""

import os
import re
import json
import secrets
import functools
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import firebase_admin
from firebase_admin import auth as fb_auth, credentials, firestore

from dotenv import load_dotenv
from flask import (
    Flask, render_template, request,
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


def bot_required(f):
    """Require Firebase auth + bot_access (admin always implicit).
    Used for the Handicapper Bot page + API."""
    @functools.wraps(f)
    @firebase_auth_required
    def wrapper(*args, **kwargs):
        role = g.user_data.get("role")
        if role != "admin" and not g.user_data.get("bot_access"):
            return jsonify({"ok": False, "error": "Bot access required"}), 403
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


@app.route("/sharp-bot")
def sharp_bot_page():
    """Phase 4 paper-bet bot tracker — admin only (client-side gated)."""
    return render_template("sharp_bot.html")


@app.route("/handicapper")
def handicapper_page():
    """Handicapper Bot — admin + bot_access gated (client-side via /api/me)."""
    return render_template("handicapper.html")


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

    def _pick_from_meta(meta: dict) -> str:
        """Mirror enrich_positions: prefer team_name+spread, then team
        name, then raw outcome (skip generic YES/NO since those don't
        identify the bet). Used for closed-position betslip labels."""
        if not isinstance(meta, dict):
            return ""
        team = meta.get("team") or {}
        team_name = (team.get("name", "") if isinstance(team, dict) else "") or ""
        raw_outcome = meta.get("outcome", "") or ""
        if team_name and raw_outcome and re.search(r'[0-9]', raw_outcome):
            return f"{team_name} {raw_outcome}"
        if team_name:
            return team_name
        if raw_outcome.lower() not in ("yes", "no", ""):
            return raw_outcome
        return raw_outcome  # last resort: bare YES/NO if nothing else

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
        # Pretty pick label + entry price for the betslip's Settled
        # Today rows. Without these, settled rows would only show the
        # game name + W/L badge — losing which side and at what price.
        pick = ""
        entry_price = None

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

            # Pick + entry price from the BEFORE-trade position. Avoids
            # the SDK's intent-based price-flip mess (originalPrice is
            # YES-canonical; cost/qty on the before-position is the real
            # per-share price they paid regardless of LONG/SHORT).
            t_meta = (t_before.get("marketMetadata") or
                      t_after.get("marketMetadata") or {})
            pick = _pick_from_meta(t_meta)
            b_cost = _safe_float(t_before.get("cost"))
            b_qty  = abs(_safe_float(t_before.get("netPosition")) or 0)
            if b_cost is not None and b_qty > 0:
                entry_price = b_cost / b_qty

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
            pick = _pick_from_meta(meta)

            side = detail.get("side", "")
            side = side.replace("POSITION_RESOLUTION_SIDE_", "")

            quantity = abs(_safe_float(before.get("netPosition")) or 0)
            cost = _safe_float(before.get("cost"))
            if cost is not None and quantity > 0:
                price = cost / quantity
                entry_price = price

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
            # Aliases so the betslip's Settled Today rows can use the
            # same buildBetSlipLabel(p) + entry-odds path as Pending /
            # Open Orders (which expect market_name + outcome + entry_price).
            "market_name": str(market) or market_slug,
            "outcome": pick,
            "entry_price": entry_price,
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
# In-memory caches
# ---------------------------------------------------------------------------

# Generic dict cache used by the Polymarket dashboard helpers
# (api_my_bets / api_data) to avoid hammering the polymarket-us SDK on
# every request. Keys are domain-specific strings, values are
# `{"data": ..., "ts": time.time()}`. Vercel cold starts wipe it.
_cache: dict = {}


# ---------------------------------------------------------------------------
# Odds Board — read latest snapshots from Supabase (cron-only architecture).
# ---------------------------------------------------------------------------
#
# As of the Owls retirement, the live page no longer hits any odds-vendor
# API. The 30-min `kahla-scanner/scrapers/odds_api.py` cron writes deduped
# rows to `book_snapshots`; the Odds Board reads the latest row per
# (market, book, market_type, side) and reconstructs the same JSON shape
# the frontend used to get from the legacy passthrough.
#
# Frontend freshness: ~30 min cron cadence. For sharp books (PIN) that's
# a non-event — they post a line and sit on it for hours. For retail
# books that thrash, the user sees their value as of the most recent
# cron run. Live games freeze at the closing line (event_start) anyway.

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
    # Sharp + big-4 US retail
    "PIN":    "pinnacle",
    "DK":     "draftkings",
    "FD":     "fanduel",
    "MGM":    "betmgm",
    "CAE":    "caesars",
    # Other US-licensed / Rob-accessible books
    "HR":     "hardrock",
    "BET365": "bet365",
    "BR":     "betrivers",
    "BOL":    "betonline",
    "LV":     "lowvig",
    "BVD":    "bovada",
    "ESPN":   "espnbet",
    "FAN":    "fanatics",
    "MB":     "mybookie",
}

# Allowlist for the Odds Board. Mirrors `ALLOWED_BOOKS` in
# kahla-scanner/scrapers/odds_api.py — must stay in sync. Books outside
# this set (EU regional junk, Owls-era short codes left in Supabase from
# old scrapes) are filtered out of the response.
_ALLOWED_BOOKS = {
    "PIN", "DK", "FD", "MGM", "CAE",
    "HR", "BET365", "BR", "BOL",
    "LV", "BVD", "ESPN", "FAN", "MB",
}


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

    # Live-game freeze. Once a game's event_start passes we want the BOARD
    # to display the closing line (last pre-start snapshot per book) and stop
    # showing every cron-cadence retail twitch — those updates are useless
    # mid-game and were causing user whiplash. We don't drop the post-start
    # rows from book_snapshots (the chart still uses them), we just filter
    # them out of the live-board read here.
    now_iso = now.isoformat()
    event_start_by_mid: dict[str, str] = {m["id"]: m.get("event_start", "") for m in markets}

    def _post_start(mid: str, captured_at: str) -> bool:
        es = event_start_by_mid.get(mid, "")
        return bool(es) and es <= now_iso and captured_at > es

    snaps = [s for s in snaps if not _post_start(s["market_id"], s["captured_at"])]

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
        # Same live-game freeze applies to the anchor: drop post-start rows
        # so a game that started 2h ago shows its closing line, not whatever
        # the books churned out mid-game.
        if _post_start(r["market_id"], r["captured_at"]):
            continue
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

        es = event_start_by_mid.get(m["id"], "")
        is_live = bool(es) and es <= now_iso

        events_out.append({
            "id":            m["id"],
            "numeric_id":    m["id"],
            "sport":         sport_path,
            "home_team":     home,
            "away_team":     away,
            "commence_time": m["event_start"],
            "league":        sport_path.upper(),
            "status":        "live" if is_live else "",
            "is_live":       is_live,
            "books":         books_block,
        })

    leagues = sorted({e["league"] for e in events_out if e.get("league")})
    sorted_books = sorted(active_books, key=lambda b: (
        0 if b == "circa" else 1 if b == "pinnacle" else 2, b
    ))
    # Most-recent captured_at across all snaps that fed events_out — i.e.
    # when the cron last wrote fresh data we surfaced. Used by the page to
    # display an honest "Updated Nm ago" instead of the wall clock, which
    # was making the user think live polling = live data updates.
    last_data_iso: str | None = None
    for s in snaps:
        ca = s.get("captured_at")
        if ca and (last_data_iso is None or ca > last_data_iso):
            last_data_iso = ca
    return events_out, sorted_books, leagues, last_data_iso



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
    earliest-seen PIN line captured by the 30-min Odds API ingest cron.

    Response: { ok, sport, events: [{home, away, commence, opener: {ml, spread, total, src}}] }
    """
    sb = get_supabase()
    if sb is None:
        return jsonify({"ok": True, "sport": request.args.get("sport"), "events": []})

    sport_owls = (request.args.get("sport") or "").lower().strip()
    sport_code = _SPORT_PATH_TO_CODE.get(sport_owls)
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

    # Pull PIN snapshots ascending — first row per (market, market_type, side)
    # is the genuine opener for that combo. Pinnacle is the only sharp source
    # post-Owls; Circa was the historical fallback but isn't returned by The
    # Odds API at any region, so we just trust PIN.
    try:
        snaps = (
            sb.table("book_snapshots")
            .select("market_id,market_type,side,price_american,line,captured_at")
            .in_("market_id", market_ids)
            .eq("book", "PIN")
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
        key = (r["market_id"], r["market_type"], r["side"])
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
        opener: dict = {"ml": {}, "spread": {}, "total": {}, "src": "PIN"}

        for mkt_type, _mkt_key in [("moneyline", "ml"), ("spread", "spread"), ("total", "total")]:
            for side in ["home", "away", "over", "under"]:
                src_row = firsts.get((mid, mkt_type, side))
                if not src_row:
                    continue
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
    Used by sub-pages to gate UI before loading data.

    `bot_access` is a per-user toggle that admins flip in User Management.
    Admins always have it implicitly (the access-gating page treats
    role=admin as bot_access=true)."""
    role = g.user_data.get("role")
    return jsonify({
        "ok": True,
        "uid": g.uid,
        "role": role,
        "approved": bool(g.user_data.get("approved")),
        "displayName": g.user_data.get("displayName"),
        "email": g.user_data.get("email"),
        "bot_access": bool(g.user_data.get("bot_access")) or role == "admin",
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
    live odds-vendor API call here. Cron writes to book_snapshots every 30
    min via kahla-scanner/scrapers/odds_api.py.

    Live games (event_start in the past) get an `is_live: true` flag plus
    `score` data merged from ESPN's free public scoreboard JSON.
    """
    sport = request.args.get("sport", "mlb")
    events, books, leagues, last_data_iso = _fetch_odds_from_snapshots(sport)
    try:
        events = _merge_espn_scores(sport, events)
    except Exception:
        # ESPN failures must never break the board.
        pass
    return jsonify({
        "ok":             True,
        "cached":         False,
        "sport":          sport,
        "events":         events,
        "books":          books,
        "leagues":        leagues,
        "last_data_iso":  last_data_iso,
        "meta_message":   "",
        "errors":         [],
    })


# ---------------------------------------------------------------------------
# ESPN free scoreboard for live game scores (no auth, no rate limit issues)
# ---------------------------------------------------------------------------

import requests as _http  # used by ESPN scoreboard fetch

# Owls path  ->  ESPN scoreboard URL slug pair (sport, league)
_ESPN_PATH = {
    "mlb":   ("baseball",       "mlb"),
    "nba":   ("basketball",     "nba"),
    "nhl":   ("hockey",         "nhl"),
    "nfl":   ("football",       "nfl"),
    "ncaab": ("basketball",     "mens-college-basketball"),
    "ncaaf": ("football",       "college-football"),
    # ESPN doesn't have one consolidated MMA scoreboard endpoint — skip for now.
}

_ESPN_CACHE: dict[str, tuple[float, list]] = {}
_ESPN_TTL = 30  # seconds


def _fetch_espn_scoreboard(sport: str) -> list:
    """Hit ESPN's free scoreboard API. Returns the events array, [] on error
    or unsupported sport. 30s in-memory cache. No API key required."""
    pair = _ESPN_PATH.get(sport)
    if not pair:
        return []
    import time
    now = time.time()
    cached = _ESPN_CACHE.get(sport)
    if cached and (now - cached[0]) < _ESPN_TTL:
        return cached[1]
    sport_grp, league = pair
    url = f"https://site.api.espn.com/apis/site/v2/sports/{sport_grp}/{league}/scoreboard"
    try:
        r = _http.get(url, timeout=8)
        if r.status_code != 200:
            return []
        events = (r.json() or {}).get("events", []) or []
        _ESPN_CACHE[sport] = (now, events)
        return events
    except Exception:
        return []


def _merge_espn_scores(sport: str, events: list) -> list:
    """Attach a `score` field to each event whose teams + start time match
    an ESPN scoreboard entry. Score shape:
      { state, display_status, period, clock, away_score, home_score, live }
    Match strategy: lowercase team-name substring + commence_time within
    ±90 min. Skips silently if ESPN doesn't cover the sport."""
    espn_events = _fetch_espn_scoreboard(sport)
    if not espn_events:
        return events

    def _norm(s: str) -> str:
        return (s or "").lower().strip()

    # Pre-build a lookup of ESPN games keyed by team-pair fragments
    espn_lookup = []
    for g in espn_events:
        comp = (g.get("competitions") or [{}])[0]
        competitors = comp.get("competitors") or []
        if len(competitors) != 2:
            continue
        # ESPN flags "homeAway"; tolerate both shapes
        home = next((c for c in competitors if c.get("homeAway") == "home"), competitors[0])
        away = next((c for c in competitors if c.get("homeAway") == "away"), competitors[1])
        home_team = (home.get("team") or {}).get("displayName") or ""
        away_team = (away.get("team") or {}).get("displayName") or ""
        status = (comp.get("status") or {})
        type_ = status.get("type") or {}
        state = type_.get("state", "")
        is_live = state in ("in", "live")
        comp_dt_str = comp.get("date") or g.get("date") or ""
        try:
            comp_dt = _parse_iso(comp_dt_str) if comp_dt_str else None
        except Exception:
            comp_dt = None
        espn_lookup.append({
            "home": _norm(home_team),
            "away": _norm(away_team),
            "commence": comp_dt,
            "score": {
                "state": state,
                "display_status": type_.get("shortDetail") or type_.get("description") or "",
                "period": str(status.get("period", "")),
                "clock": status.get("displayClock", ""),
                "away_score": away.get("score"),
                "home_score": home.get("score"),
                "live": is_live,
            },
        })

    for ev in events:
        eh = _norm(ev.get("home_team", ""))
        ea = _norm(ev.get("away_team", ""))
        ec_str = ev.get("commence_time", "")
        ec = _parse_iso(ec_str) if ec_str else None

        for el in espn_lookup:
            if not el["home"] or not el["away"]:
                continue
            # Substring match in either direction so "Mariners" matches
            # "Seattle Mariners" both ways
            if not ((eh in el["home"] or el["home"] in eh) and
                    (ea in el["away"] or el["away"] in ea)):
                continue
            # Commence time within 90 min if both available
            if ec and el["commence"]:
                if abs((ec - el["commence"]).total_seconds()) > 90 * 60:
                    continue
            ev["score"] = el["score"]
            break

    return events


@app.route("/api/my-bets")
@admin_required
def api_my_bets():
    import time
    cache_key = "my_bets"
    now = time.time()
    cached = _cache.get(cache_key)
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
    _cache[cache_key] = {"data": result, "ts": now}
    return jsonify(result)


# Open / unfilled limit orders. Distinct from positions (which are
# already-filled bets awaiting outcome) — these are working orders
# sitting in Polymarket's CLOB book that haven't matched yet. Powers
# the "Open Orders" section of the dashboard betslip so Rob can share
# his planned bets with friends before they fill.
_OPEN_ORDER_STATES = {
    "ORDER_STATE_NEW",
    "ORDER_STATE_PENDING_NEW",
    "ORDER_STATE_PENDING_REPLACE",
    "ORDER_STATE_PARTIALLY_FILLED",
}

# Map SDK intent → human-readable side label for the betslip
_INTENT_LABEL = {
    "ORDER_INTENT_BUY_LONG":   "BUY YES",
    "ORDER_INTENT_BUY_SHORT":  "BUY NO",
    "ORDER_INTENT_SELL_LONG":  "SELL YES",
    "ORDER_INTENT_SELL_SHORT": "SELL NO",
}


@app.route("/api/my-orders")
@admin_required
def api_my_orders():
    """Open/unfilled limit orders on Polymarket. Filtered to working
    states (NEW / PENDING_NEW / PENDING_REPLACE / PARTIALLY_FILLED).
    Filled, canceled, expired, and rejected orders are excluded.

    Response shape:
      { ok, orders: [{
            id, market_name, outcome, team_name, pick,
            slug, event_slug, intent, side_label,
            type, tif, price, quantity, cum_quantity,
            leaves_quantity, fill_pct, created_at
        }] }

    The shape mirrors /api/my-bets's `pick` / `market_name` /
    `team_name` triplet so the frontend's buildBetSlipLabel() works
    without changes."""
    import time
    cache_key = "my_orders"
    now = time.time()
    cached = _cache.get(cache_key)
    if cached and (now - cached["ts"]) < 30:
        return jsonify(cached["data"])

    out_orders: list[dict] = []
    try:
        client = get_client()
        resp = client.orders.list()
        # SDK returns either a dict-like with .orders or a typed object
        raw = resp.get("orders") if isinstance(resp, dict) else getattr(resp, "orders", []) or []
        for o in raw:
            # Each SDK order may be dict or model — normalize accessor.
            def _g(key, default=None):
                if isinstance(o, dict): return o.get(key, default)
                return getattr(o, key, default)

            state = _g("state") or ""
            if state not in _OPEN_ORDER_STATES:
                continue

            md = _g("marketMetadata") or {}
            if not isinstance(md, dict):
                # If it's a model, dict it for safe lookup
                md = {k: getattr(md, k, None) for k in
                      ("slug", "title", "outcome", "eventSlug", "team")}

            slug = md.get("slug") or ""
            title = md.get("title") or ""
            outcome = md.get("outcome") or ""
            event_slug = md.get("eventSlug") or ""
            team = md.get("team") or {}
            team_name = team.get("name", "") if isinstance(team, dict) else ""

            # Match the same pick-derivation logic as api_my_bets so a
            # "Heat -3.5" market shows team-prefixed in the slip.
            pick = outcome
            if team_name and outcome and re.search(r"[0-9]", outcome):
                pick = f"{team_name} {outcome}"
            elif outcome.lower() in ("over", "under"):
                try:
                    md_raw = fetch_market(client, slug)
                    md_full = md_raw.get("market", md_raw) if isinstance(md_raw, dict) else {}
                    question = md_full.get("question", "")
                    total_match = re.search(r"(\d+\.?\d*)", question)
                    if total_match:
                        pick = f"{outcome} {total_match.group(1)}"
                except Exception:
                    pass
            elif team_name:
                pick = team_name

            qty       = _g("quantity") or 0
            cum_qty   = _g("cumQuantity") or 0
            leaves    = _g("leavesQuantity") or 0
            fill_pct  = (cum_qty / qty * 100) if qty else 0
            raw_price = _safe_float(_g("price"))
            intent    = _g("intent") or ""
            # Polymarket's CLOB stores prices canonically as the YES
            # probability of the market. When the user's pick is the
            # NO side (intent = *_SHORT), the SDK's price field is the
            # YES price — so to display what they actually pay/receive
            # for their picked outcome we need the complement.
            # When their pick IS the YES side (intent = *_LONG) the
            # SDK price is already the right number — DO NOT flip.
            # Empirically verified against Polymarket app order list.
            needs_flip = intent.endswith("_SHORT")
            if raw_price is not None and 0 <= raw_price <= 1 and needs_flip:
                price = 1 - raw_price
            else:
                price = raw_price
            side_label = _INTENT_LABEL.get(intent, intent.replace("ORDER_INTENT_", "").replace("_", " "))

            out_orders.append({
                "id":               _g("id") or "",
                "market_name":      title,
                # `outcome` is what the dashboard's buildBetSlipLabel()
                # appends after the market title. Use `pick` (which has
                # the team prefix logic for spread bets like
                # "Tampa Bay Rays -1.5") instead of the raw outcome
                # (which is just "-1.5" for spreads — no team).
                "outcome":          pick or outcome,
                "raw_outcome":      outcome,
                "team_name":        team_name,
                "pick":             pick,
                "slug":             slug,
                "event_slug":       event_slug,
                "intent":           intent,
                "side_label":       side_label,
                "type":             _g("type") or "",
                "tif":              _g("tif") or "",
                "state":            state,
                "price":            price,
                "quantity":         qty,
                "cum_quantity":     cum_qty,
                "leaves_quantity":  leaves,
                "fill_pct":         round(fill_pct, 1),
                "created_at":       _g("createTime") or _g("insertTime") or "",
            })
    except Exception as e:
        return jsonify({"ok": False, "orders": [], "error": str(e)})

    # Newest first (most recently placed at top of betslip).
    out_orders.sort(key=lambda o: o.get("created_at") or "", reverse=True)
    result = {"ok": True, "orders": out_orders}
    _cache[cache_key] = {"data": result, "ts": now}
    return jsonify(result)


# ---------------------------------------------------------------------------
# CLV (Closing Line Value) — how each Polymarket bet compares to PIN's
# closing line. Positive = you got a better price than the close (sharp).
# Negative = you got picked off. Rolling 30-day average is the
# professional metric for whether YOU are sharp regardless of W/L noise.
# ---------------------------------------------------------------------------

# Sport keys: Polymarket slug prefix → our scanner sport code.
_PM_SLUG_TO_SPORT = {
    "mlb":   "MLB",
    "nba":   "NBA",
    "nhl":   "NHL",
    "nfl":   "NFL",
    "ncaab": "CBB",
    "ncaaf": "NCAAF",
    "mma":   "UFC",
}


def _amer_to_prob_py(p):
    """Python equivalent of the JS helper. American → implied prob."""
    if p is None:
        return None
    try:
        p = float(p)
    except (TypeError, ValueError):
        return None
    if p > 0: return 100.0 / (p + 100.0)
    if p < 0: return -p / (-p + 100.0)
    return 0.5


def _prob_to_amer_py(prob):
    """Inverse of `_amer_to_prob_py`. Used to convert Polymarket
    share prices (0-1, = implied probability) to American odds for
    storage in `bot_picks.actual_fill_price`."""
    if prob is None:
        return None
    try:
        prob = float(prob)
    except (TypeError, ValueError):
        return None
    if not (0 < prob < 1):
        return None
    if prob >= 0.5:
        return int(round(-prob * 100.0 / (1.0 - prob)))
    return int(round((1.0 - prob) * 100.0 / prob))


# PMM slug team-code → official team-name fragment used for substring
# matching against our markets table's `event_name` field. PMM uses
# standard sports abbreviations that aren't derivable from the full
# name (St. Louis → "stl", Tampa Bay → "tb", LA Dodgers → "lad", LA
# Angels → "laa", Chicago Cubs → "chc", Chicago White Sox → "chw").
# Only the fragments needed for substring containment — full official
# names are overkill. Add new entries here when sync skips with a
# "no slug-code match" reason for a code not listed.
_TEAM_CODE_MAP = {
    # MLB
    "ari": "arizona", "az": "arizona", "atl": "atlanta",
    "bal": "baltimore", "bos": "boston",
    "chc": "chicago cubs", "chw": "chicago white sox", "cws": "chicago white sox",
    "cin": "cincinnati", "cle": "cleveland", "col": "colorado", "det": "detroit",
    "hou": "houston", "kc":  "kansas city", "laa": "angels", "lad": "dodgers",
    "mia": "miami", "mil": "milwaukee", "min": "minnesota",
    "nym": "mets", "nyy": "yankees", "oak": "athletics", "ath": "athletics",
    "phi": "philadelphia", "pit": "pittsburgh", "sd": "san diego",
    "sea": "seattle", "sf":  "san francisco", "stl": "st. louis",
    "tb":  "tampa bay", "tex": "texas", "tor": "toronto", "was": "washington",
    "wsh": "washington",
    # NHL
    "ana": "anaheim", "ari_nhl": "arizona coyotes", "bos_nhl": "bruins",
    "buf": "buffalo", "cgy": "calgary", "car": "carolina", "chi": "chicago",
    "col_nhl": "colorado avalanche", "cbj": "columbus", "dal": "dallas",
    "det_nhl": "detroit red", "edm": "edmonton", "fla": "florida",
    "la":  "los angeles kings", "lak": "los angeles kings", "min_nhl": "minnesota wild",
    "mon": "montréal", "mtl": "montréal", "nsh": "nashville",
    "nj":  "new jersey", "njd": "new jersey", "nyi": "islanders",
    "nyr": "rangers", "ott": "ottawa", "phi_nhl": "flyers",
    "pit_nhl": "penguins", "sj":  "san jose", "sjs": "san jose",
    "sea_nhl": "kraken", "stl_nhl": "st. louis blues", "tb_nhl": "tampa bay light",
    "tor_nhl": "toronto maple", "uta": "utah", "van": "vancouver",
    "veg": "vegas", "vgk": "vegas", "was_nhl": "washington capitals",
    "wpg": "winnipeg",
    # NBA
    "atl_nba": "hawks", "bos_nba": "celtics", "bkn": "brooklyn",
    "cha": "charlotte", "chi_nba": "bulls", "cle_nba": "cleveland",
    "dal_nba": "mavericks", "den": "denver", "det_nba": "pistons",
    "gs": "golden state", "gsw": "golden state", "hou_nba": "rockets",
    "ind": "indiana", "lac": "clippers", "lal": "lakers",
    "mem": "memphis", "mia_nba": "heat", "mil_nba": "bucks",
    "min_nba": "minnesota timber", "no": "new orleans", "nop": "new orleans",
    "ny": "knicks", "nyk": "knicks", "okc": "oklahoma",
    "orl": "orlando", "phi_nba": "76ers", "phx": "phoenix",
    "por": "portland", "sac": "sacramento", "sa": "san antonio",
    "sas": "san antonio", "tor_nba": "raptors", "uth": "utah",
    "was_nba": "wizards", "wsh_nba": "wizards",
}


def _clv_extract_match_info(meta):
    """Parse Polymarket marketMetadata into the fields we need to look
    up the corresponding row in our markets table.

    Returns {sport, bet_date, team_name, market_type, point} or None
    if we can't extract enough to attempt a match (e.g. non-sports
    markets, election markets).
    """
    title       = (meta.get("title") or "")
    event_slug  = (meta.get("eventSlug") or "")
    market_slug = (meta.get("slug") or "")
    raw_outcome = (meta.get("outcome") or "")
    team        = meta.get("team") if isinstance(meta.get("team"), dict) else {}
    team_name   = (team.get("name") if isinstance(team, dict) else "") or ""

    sport = None
    for slug_str in (event_slug, market_slug):
        for sk, code in _PM_SLUG_TO_SPORT.items():
            if slug_str.startswith(sk + "-"):
                sport = code
                break
        if sport:
            break
    if not sport:
        return None

    date_match = re.search(r"(\d{4}-\d{2}-\d{2})", event_slug or market_slug)
    if not date_match:
        return None
    bet_date = date_match.group(1)

    # Extract team codes + optional line suffix from the slug. PMM
    # slug patterns:
    #   ML:     aec-mlb-stl-sd-2026-05-10
    #   TOT:    tsc-mlb-stl-sd-2026-05-10-8pt5   (line = 8.5)
    #   SPR:    similar `-1pt5` suffix where applicable
    # Codes are usually 2-4 lowercase letters; line suffix is `\dpt\d`.
    slug_codes: dict = {}
    slug_line: float | None = None
    code_match = re.match(
        r"^[a-z]+-[a-z]+-([a-z]{2,4})-([a-z]{2,4})-\d{4}-\d{2}-\d{2}(?:-([0-9]+pt[0-9]+))?",
        market_slug or event_slug or "",
    )
    if code_match:
        slug_codes = {"away": code_match.group(1), "home": code_match.group(2)}
        line_str = code_match.group(3)
        if line_str:
            try:
                slug_line = float(line_str.replace("pt", "."))
            except ValueError:
                pass

    outcome_lower = raw_outcome.strip().lower()
    point = None
    if outcome_lower in ("over", "under"):
        market_type = "total"
        m = re.search(r"(\d+\.?\d*)", title)
        if m:
            point = float(m.group(1))
        # Slug suffix is more reliable than the title parser (which
        # picks up incidental numbers in titles). Override when the
        # slug carried a clean line value.
        if slug_line is not None:
            point = slug_line
    elif re.search(r"[+-]\d+\.?\d*", raw_outcome):
        market_type = "spread"
        m = re.search(r"([+-]?\d+\.?\d*)", raw_outcome)
        if m:
            point = float(m.group(1))
        # Slug suffix override (matches the TOT case rationale).
        if slug_line is not None and point is not None:
            # Preserve sign from raw_outcome — slug just has magnitude.
            sign = -1 if point < 0 else 1
            point = sign * slug_line
    elif team_name or raw_outcome:
        market_type = "moneyline"
    else:
        return None

    return {
        "sport":       sport,
        "bet_date":    bet_date,
        "team_name":   team_name or raw_outcome,
        "market_type": market_type,
        "point":       point,
        "raw_outcome": raw_outcome,
        "slug_codes":  slug_codes,
    }


def _clv_find_market(extracted):
    """Look up the matching markets row + figure out which side
    (home/away) the user's pick is on.

    Returns (market_id, away_event, home_event, user_side, event_start_iso)
    or None.
    """
    sb = get_supabase()
    if sb is None:
        return None

    sport     = extracted["sport"]
    bet_date  = extracted["bet_date"]
    team_name = extracted["team_name"]

    try:
        markets = (
            sb.table("markets")
            .select("id,event_name,event_start")
            .eq("sport", sport)
            .gte("event_start", f"{bet_date}T00:00:00+00:00")
            .lte("event_start", f"{bet_date}T23:59:59+00:00")
            .limit(50)
            .execute()
            .data
            or []
        )
    except Exception:
        return None
    if not markets:
        return None

    try:
        from rapidfuzz import fuzz
    except ImportError:
        return None

    tn = team_name.lower().strip()
    if not tn:
        return None

    best = None
    best_score = 0
    for m in markets:
        ev = m.get("event_name") or ""
        if " @ " not in ev:
            continue
        away, home = ev.split(" @ ", 1)
        s_a = fuzz.partial_ratio(tn, away.lower())
        s_h = fuzz.partial_ratio(tn, home.lower())
        max_s = max(s_a, s_h)
        if max_s > best_score and max_s >= 75:
            best_score = max_s
            user_side = "away" if s_a >= s_h else "home"
            best = (m["id"], away, home, user_side, m["event_start"])
    return best


def _clv_pin_close_pair(market_id, market_type, before_iso):
    """PIN's last-pre-event-start prices on BOTH sides of a market.

    Returns {side: implied_prob} (e.g. {'home': 0.55, 'away': 0.45})
    when both sides have a snapshot, else None.
    """
    sb = get_supabase()
    if sb is None:
        return None

    sides = ("over", "under") if market_type == "total" else ("home", "away")
    out = {}
    for side in sides:
        try:
            rows = (
                sb.table("book_snapshots")
                .select("price_american,line,captured_at")
                .eq("market_id", market_id)
                .eq("book", "PIN")
                .eq("market_type", market_type)
                .eq("side", side)
                .lte("captured_at", before_iso)
                .order("captured_at", desc=True)
                .limit(1)
                .execute()
                .data
                or []
            )
        except Exception:
            continue
        if not rows:
            continue
        prob = _amer_to_prob_py(rows[0].get("price_american"))
        if prob is not None:
            out[side] = {"prob": prob, "price": rows[0].get("price_american")}
    return out if len(out) == 2 else None


@app.route("/api/clv")
@admin_required
def api_clv():
    """Closing Line Value per open Polymarket position.

    For each filled position whose game has started (so PIN has a
    closing line), match to our markets table, fetch PIN's pre-start
    snapshots on both sides, devig the pair, compare to your fill price.

    CLV = (devigged_close_prob − entry_implied_prob) × 100
    Positive = your entry was at a longer price than the close (sharp).
    Negative = line moved against you.

    v1 scope: open positions only. v2 will add closed/settled bets +
    30-day rolling average per signal type (Sharp Bot needs that).
    """
    import time
    cache_key = "clv"
    now = time.time()
    cached = _cache.get(cache_key)
    if cached and (now - cached["ts"]) < 60:
        return jsonify(cached["data"])

    out = []
    matched = 0
    skipped_no_match = 0
    skipped_future = 0
    skipped_no_close = 0

    try:
        client = get_client()
        positions = fetch_positions(client)
        now_iso = datetime.now(timezone.utc).isoformat()

        for slug, pos in positions:
            if pos.get("expired"):
                continue
            net = _safe_float(pos.get("netPosition")) or 0
            if abs(net) < 0.01:
                continue
            cost = _safe_float(pos.get("cost"))
            if cost is None or abs(net) <= 0:
                continue
            entry_prob = cost / abs(net)
            if not (0 < entry_prob < 1):
                continue

            meta = pos.get("marketMetadata") or {}
            extracted = _clv_extract_match_info(meta)
            if not extracted:
                skipped_no_match += 1
                continue
            match = _clv_find_market(extracted)
            if not match:
                skipped_no_match += 1
                continue
            market_id, away_ev, home_ev, user_side, event_start_iso = match

            # Skip games that haven't started — no closing line yet.
            if event_start_iso > now_iso:
                skipped_future += 1
                continue

            mt = extracted["market_type"]
            if mt == "total":
                outcome_lower = (meta.get("outcome") or "").lower()
                user_book_side = "over" if "over" in outcome_lower else "under"
            else:
                user_book_side = user_side  # 'home' / 'away'

            pair = _clv_pin_close_pair(market_id, mt, event_start_iso)
            if not pair or user_book_side not in pair:
                skipped_no_close += 1
                continue

            sum_p = pair["home" if mt != "total" else "over"]["prob"] + pair["away" if mt != "total" else "under"]["prob"]
            if sum_p <= 0:
                skipped_no_close += 1
                continue
            user_prob_devig = pair[user_book_side]["prob"] / sum_p
            clv_pp = round((user_prob_devig - entry_prob) * 100, 2)
            matched += 1

            # SDK's `outcome` for spreads is just '-1.5'; prepend team
            # name so the dashboard label looks like the betslip's.
            display_outcome = (extracted["team_name"] + " " + extracted["raw_outcome"]).strip() if mt == "spread" else (meta.get("outcome") or extracted["team_name"])

            out.append({
                "market_name":   meta.get("title") or "",
                "market_slug":   meta.get("slug") or slug,
                "outcome":       display_outcome,
                "team_name":     extracted["team_name"],
                "market_type":   mt,
                "side":          user_book_side,
                "entry_prob":    round(entry_prob, 4),
                "close_prob":    round(user_prob_devig, 4),
                "close_amer":    pair[user_book_side].get("price"),
                "clv_pp":        clv_pp,
                "commence_time": event_start_iso,
            })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "clv": []})

    out.sort(key=lambda r: r.get("commence_time") or "", reverse=True)

    # Rolling stat: average CLV across positions we could match.
    avg_clv = round(sum(r["clv_pp"] for r in out) / len(out), 2) if out else None

    result = {
        "ok":              True,
        "clv":             out,
        "avg_clv_pp":      avg_clv,
        "matched":         matched,
        "skipped_no_match":  skipped_no_match,
        "skipped_future":    skipped_future,
        "skipped_no_close":  skipped_no_close,
    }
    _cache[cache_key] = {"data": result, "ts": now}
    return jsonify(result)


# ---------------------------------------------------------------------------
# Public betting splits (% bets / % money) — scraped from Action Network's
# free public-betting page.
# ---------------------------------------------------------------------------
#
# Action Network publishes splits per sport at /{sport}/public-betting.
# Page is server-rendered HTML with the splits table inline (some
# JavaScript hydration but the table data is in the markup). Free, no
# auth, ToS gray area but the same data scrapers have used for years.
#
# We cache 30 min server-side because:
#   - splits move slowly (every few hours)
#   - Action Network rate-limits aggressive scrapers
#   - re-fetching per /api/odds poll would hammer them
#
# If parsing breaks (HTML changes), the scraper logs detail and the
# /api/splits endpoint returns ok:false so the UI degrades gracefully —
# board still works, splits just disappear.

_ACTION_SPORTS = {"mlb", "nba", "nhl", "nfl", "ncaab", "ncaaf"}
_SPLITS_CACHE_TTL = 30 * 60  # 30 min
_SPLITS_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def _fetch_action_splits(sport: str) -> dict:
    """Scrape Action Network's public-betting page for `sport`. Returns
    {"ok": bool, "events": [{home, away, ml, spread, total}], "error": str?, "html_len": int?}.

    Each event entry shape:
      {
        "home": "Atlanta Braves",
        "away": "Philadelphia Phillies",
        "spread": {"away_bets": 65, "away_money": 70, "home_bets": 35, "home_money": 30},
        "ml":     {"away_bets": ..., "home_bets": ..., ...},
        "total":  {"over_bets": ..., "over_money": ..., "under_bets": ..., "under_money": ...},
      }
    Missing markets just absent from the dict. Best-effort parse.
    """
    import time
    import requests as _http
    cache_key = f"splits:{sport}"
    now = time.time()
    cached = _cache.get(cache_key)
    if cached and (now - cached["ts"]) < _SPLITS_CACHE_TTL:
        return cached["data"]

    if sport not in _ACTION_SPORTS:
        out = {"ok": False, "error": f"unsupported sport: {sport}", "events": []}
        _cache[cache_key] = {"data": out, "ts": now}
        return out

    # Action Network's SSR for the bare URL frequently returns YESTERDAY's
    # finals (their server defaults to the previous day until late-evening
    # ET). Pass today's date in US Eastern explicitly so we always grab
    # today's slate. Also lets us cache per-date.
    from datetime import datetime
    try:
        from zoneinfo import ZoneInfo
        today_et = datetime.now(ZoneInfo("America/New_York")).strftime("%Y%m%d")
    except Exception:
        today_et = datetime.utcnow().strftime("%Y%m%d")

    url = f"https://www.actionnetwork.com/{sport}/public-betting?date={today_et}"
    try:
        r = _http.get(url, headers={"User-Agent": _SPLITS_UA, "Accept": "text/html"}, timeout=12)
        if r.status_code != 200:
            out = {"ok": False, "error": f"HTTP {r.status_code}", "events": [], "html_len": len(r.text or "")}
            _cache[cache_key] = {"data": out, "ts": now}
            return out
        html = r.text or ""
    except Exception as e:
        out = {"ok": False, "error": f"fetch: {e}", "events": []}
        _cache[cache_key] = {"data": out, "ts": now}
        return out

    try:
        from bs4 import BeautifulSoup
    except Exception as e:
        out = {"ok": False, "error": f"bs4 unavailable: {e}", "events": [], "html_len": len(html)}
        _cache[cache_key] = {"data": out, "ts": now}
        return out

    parsed = _parse_action_splits_html(html)
    parsed["html_len"] = len(html)
    parsed["url"] = url
    parsed["date_et"] = today_et

    # ALWAYS try __NEXT_DATA__ as a backup data source.
    parsed["source"] = "table"
    nxt = _parse_action_splits_next_data(html)
    if nxt.get("events"):
        parsed["events"]     = nxt["events"]
        parsed["ok"]         = True
        parsed["source"]     = "next_data"
    parsed["next_debug"]     = nxt.get("debug", {})

    # Best source: Action Network's JSON API directly. The SSR HTML
    # only contains yesterday's completed games, and __NEXT_DATA__
    # doesn't carry split percentages — both are populated/refreshed
    # via XHR to api.actionnetwork.com after the page loads. So we
    # call that API ourselves. If it returns events we use those.
    api = _fetch_action_api(sport, today_et)
    if api.get("events"):
        parsed["events"]     = api["events"]
        parsed["ok"]         = True
        parsed["source"]     = "json_api"
    parsed["api_debug"]      = api.get("debug", {})

    # Only cache successful parses. A 0-event response usually means our
    # status-prefix regex needs another pattern (NHL "Final/OT" etc.) —
    # don't pin a broken result for 30 min while iterating.
    if parsed.get("ok"):
        _cache[cache_key] = {"data": parsed, "ts": now}
    return parsed


# Action Network's API uses a different sport-key convention than
# their URL paths. Map our path codes to API league IDs / slugs.
_ACTION_API_LEAGUE = {
    "mlb":   "mlb",
    "nba":   "nba",
    "nhl":   "nhl",
    "nfl":   "nfl",
    "ncaab": "ncaab",
    "ncaaf": "ncaaf",
}


def _fetch_action_api(sport: str, date_et: str) -> dict:
    """Hit Action Network's public JSON API for the day's scoreboard.

    This is what the actionnetwork.com browser UI calls via XHR after
    page hydration to get today's games + public betting %s. The SSR
    HTML page is incidental — the real data path is this API.

    Endpoint observed in their web UI:
      GET https://api.actionnetwork.com/web/v2/scoreboard/{league}
          ?period=game&date=YYYYMMDD&bookIds=15,30,68,69,71,75,79,123,69,972
          &date_format=YYYY-MM-DD

    No auth on the public scoreboard. User-Agent + a Referer header
    keeps Cloudflare/WAF from challenging the request.
    """
    import requests as _http
    debug: dict = {"ok": False}
    league = _ACTION_API_LEAGUE.get(sport)
    if not league:
        debug["error"] = f"unsupported league: {sport}"
        return {"events": [], "debug": debug}

    url = (f"https://api.actionnetwork.com/web/v2/scoreboard/{league}"
           f"?period=game&date={date_et}")
    debug["url"] = url
    try:
        r = _http.get(url, headers={
            "User-Agent": _SPLITS_UA,
            "Accept":     "application/json, text/plain, */*",
            "Origin":     "https://www.actionnetwork.com",
            "Referer":    "https://www.actionnetwork.com/",
        }, timeout=12)
        debug["status"] = r.status_code
        if r.status_code != 200:
            debug["error"] = f"HTTP {r.status_code}"
            debug["body_snippet"] = (r.text or "")[:300]
            return {"events": [], "debug": debug}
        data = r.json()
    except Exception as e:
        debug["error"] = f"{type(e).__name__}: {e}"
        return {"events": [], "debug": debug}

    if not isinstance(data, dict):
        debug["error"] = f"unexpected JSON type: {type(data).__name__}"
        return {"events": [], "debug": debug}

    debug["top_keys"] = sorted(data.keys())[:30]

    # Most likely shape: {"games": [...]}. Fall back to scanning common
    # alt names so we don't break if Action Network renames the field.
    games = (data.get("games")
             or data.get("scoreboard")
             or data.get("events")
             or [])
    if not isinstance(games, list):
        debug["error"] = f"games field not a list: {type(games).__name__}"
        return {"events": [], "debug": debug}
    debug["game_count"] = len(games)

    splits_paths: list[str] = []
    events: list[dict] = []
    for g in games:
        ev = _next_data_event(g, splits_paths)
        if ev:
            events.append(ev)
    debug["events_extracted"] = len(events)
    debug["splits_paths_seen"] = splits_paths[:5]

    # If we got games but no splits, dump the first game's structure
    # so we can see where the API puts public betting %s (almost
    # certainly different field names than __NEXT_DATA__).
    if games and not events:
        g0 = games[0]
        if isinstance(g0, dict):
            debug["game_shape"] = {k: _shape_value(v, depth_left=2)
                                    for k, v in list(g0.items())[:120]}

    debug["ok"] = True
    return {"events": events, "debug": debug}


def _parse_action_splits_next_data(html: str) -> dict:
    """Pull splits out of Action Network's __NEXT_DATA__ JSON blob.

    Action Network is a Next.js app — the entire page-data tree is
    embedded as JSON inside <script id="__NEXT_DATA__"> so the client
    can hydrate. That blob is by definition complete (all games on the
    page) where the SSR'd HTML table is sometimes truncated to only
    completed games.

    We don't know the exact shape — it's been refactored at least once
    and isn't documented — so this walks the tree heuristically looking
    for any object that has team identifiers AND public-percentage
    fields, then maps them to our event shape. Failures return debug
    info so we can iterate.
    """
    import json as _json
    import re as _re
    debug: dict = {"found_blob": False, "json_len": 0, "candidate_count": 0}
    m = _re.search(
        r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>',
        html, _re.DOTALL,
    )
    if not m:
        return {"events": [], "debug": debug}
    debug["found_blob"] = True
    raw = m.group(1)
    debug["json_len"] = len(raw)
    try:
        data = _json.loads(raw)
    except Exception as e:
        debug["error"] = f"json: {e}"
        return {"events": [], "debug": debug}

    # Game candidate = any dict with home_team_id + away_team_id +
    # start_time. That's structural ("what is a game"), independent of
    # whether splits have been attached yet — so we catch today's
    # scheduled games and yesterday's finals alike.
    candidates: list[dict] = []
    sample_keys: set[str] = set()

    def walk(node, depth=0):
        if depth > 14 or len(candidates) > 500:
            return
        if isinstance(node, dict):
            keys = set(node.keys())
            for k in keys:
                if len(sample_keys) < 200:
                    sample_keys.add(k)
            if "home_team_id" in keys and "away_team_id" in keys and "start_time" in keys:
                candidates.append(node)
            for v in node.values():
                walk(v, depth + 1)
        elif isinstance(node, list):
            for v in node:
                walk(v, depth + 1)

    walk(data)
    debug["candidate_count"] = len(candidates)
    debug["sample_top_keys"] = sorted(sample_keys)[:200]

    events: list[dict] = []
    splits_found_in: list[str] = []  # remembers paths where we saw split %s
    for c in candidates:
        ev = _next_data_event(c, splits_found_in)
        if ev:
            events.append(ev)
    debug["events_extracted"] = len(events)
    debug["splits_paths_seen"] = splits_found_in[:5]

    # If we have games but no splits, dump deep structure of the first
    # game so we can see exactly where the split percentages live.
    if candidates and not events:
        first = candidates[0]
        shape = {}
        for k, v in list(first.items())[:120]:
            shape[k] = _shape_value(v, depth_left=2)
        debug["candidate_shape"] = shape

    return {"events": events, "debug": debug}


def _shape_value(v, depth_left=2):
    """Recursive type/key dumper for debug output. Doesn't leak raw values
    deeply — just enough to reveal field structure."""
    if isinstance(v, dict):
        if depth_left <= 0:
            return {"_type": "dict", "_keys": sorted(v.keys())[:30]}
        return {"_type": "dict",
                "_fields": {k: _shape_value(v2, depth_left - 1) for k, v2 in list(v.items())[:30]}}
    if isinstance(v, list):
        if not v:
            return {"_type": "list", "_len": 0}
        if depth_left <= 0 or not isinstance(v[0], (dict, list)):
            return {"_type": "list", "_len": len(v),
                    "_first_type": type(v[0]).__name__,
                    "_first_keys": sorted(v[0].keys())[:30] if isinstance(v[0], dict) else None}
        return {"_type": "list", "_len": len(v),
                "_first": _shape_value(v[0], depth_left - 1)}
    if isinstance(v, (int, float)):
        return f"num:{v}"
    if isinstance(v, str):
        return f"str:{v[:60]}"
    if v is None:
        return "null"
    return type(v).__name__


def _next_data_event(node: dict, splits_paths: list[str] | None = None) -> dict | None:
    """Map a __NEXT_DATA__ game object → our splits event shape.
    Walks the game's subtree to find split percentages — they don't
    live on the game object directly; they're nested somewhere inside
    `markets`, a per-book wrapper, or a top-level "consensus" subtree
    we can't see from the game alone.
    Returns None when team names can't be extracted."""
    def _name(v):
        if isinstance(v, str): return v
        if isinstance(v, dict):
            return v.get("display_name") or v.get("full_name") or v.get("name") or v.get("abbr")
        return None

    away = home = None
    teams = node.get("teams")
    if isinstance(teams, list) and len(teams) == 2:
        # Action Network's `teams` array is sometimes [away, home],
        # sometimes [home, away]. Use away_team_id / home_team_id to
        # disambiguate when present.
        away_id = node.get("away_team_id")
        home_id = node.get("home_team_id")
        for t in teams:
            tid = t.get("id") if isinstance(t, dict) else None
            if tid == away_id: away = _name(t)
            if tid == home_id: home = _name(t)
        if not (away and home):
            away = _name(teams[0])
            home = _name(teams[1])
    away = away or _name(node.get("away_team") or node.get("awayTeam"))
    home = home or _name(node.get("home_team") or node.get("homeTeam"))
    if not (away and home):
        return None

    # Walk the game's subtree looking for split percentages. Match keys
    # like bet_percent / bets_pct / ticket_percent / public_bet_percent /
    # money_percent / handle_percent. Confine to this game's subtree
    # so we don't cross-pollute between games.
    ml: dict = {}
    def harvest(n, path="", depth=0):
        if depth > 8 or (ml.get("away_bets") is not None and ml.get("home_bets") is not None
                         and ml.get("away_money") is not None and ml.get("home_money") is not None):
            return
        if isinstance(n, dict):
            for k, v in n.items():
                kl = k.lower()
                if isinstance(v, (int, float)):
                    is_bets  = ("bet" in kl or "ticket" in kl) and "percent" in kl
                    is_money = ("money" in kl or "handle" in kl) and "percent" in kl
                    is_away  = "away" in kl
                    is_home  = "home" in kl
                    if is_bets and is_away: ml.setdefault("away_bets", int(round(v)));  splits_paths and splits_paths.append(f"{path}.{k}")
                    if is_bets and is_home: ml.setdefault("home_bets", int(round(v)));  splits_paths and splits_paths.append(f"{path}.{k}")
                    if is_money and is_away: ml.setdefault("away_money", int(round(v))); splits_paths and splits_paths.append(f"{path}.{k}")
                    if is_money and is_home: ml.setdefault("home_money", int(round(v))); splits_paths and splits_paths.append(f"{path}.{k}")
                else:
                    harvest(v, f"{path}.{k}", depth + 1)
        elif isinstance(n, list):
            for i, item in enumerate(n):
                harvest(item, f"{path}[{i}]", depth + 1)
    harvest(node)

    if not ml:
        return None
    raw_status = (node.get("status_display") or node.get("status") or "").strip()
    return {
        "away_team": away,
        "home_team": home,
        "ml":        ml,
        "sharp_diff": None,
        "status":    raw_status or "scheduled",
    }


def _parse_action_splits_html(html: str) -> dict:
    """Parse Action Network's /{sport}/public-betting HTML table.

    Row layout we discovered:
        cell 0: status + teams, e.g.
                "TOP 10TH : 0-0, 2 Out Pirates PIT 907 Brewers MIL 908"
                "Final Mariners SEA 925 Cardinals STL 926"
                "PPD Rockies COL 903 Mets NYM 904"
        cell 1: open odds         (American, away then home)
        cell 2: current odds      (American, away then home)
        cell 3: % of bets         "Right Arrow 35 % Right Arrow 65 %"
        cell 4: % of money        "Right Arrow 27 % Right Arrow 73 %"
        cell 5: money-vs-bets diff (e.g. "+8 %") — public-fade signal
        cell 6: total ticket count

    Currently the page shows MONEYLINE splits only by default. Spread and
    Total live on different URLs (?period=spread / ?period=total) — left
    as a future iteration.
    """
    from bs4 import BeautifulSoup
    import re as _re

    soup = BeautifulSoup(html, "lxml")
    tables = soup.find_all("table")
    if not tables:
        return {"ok": False, "error": "no <table> elements in HTML",
                "events": [], "table_count": 0}

    table = max(tables, key=lambda t: len(t.find_all("tr")))
    rows = table.find_all("tr")

    # Pattern: <away_name>  <ABBR>  <NUM>  <home_name>  <ABBR>  <NUM>
    # Team name allows spaces (e.g. "Red Sox", "White Sox", "Blue Jays").
    # Allowing 2-4 char abbr to catch ATH, NYM, CWS, etc. ID width is
    # \d{1,4} because NHL uses 1-2 digit game IDs (CAR 7) where MLB
    # uses 3-digit IDs (SEA 925).
    team_re = _re.compile(
        r"([A-Za-z][A-Za-z .'-]*?)\s+([A-Z]{2,4})\s+(\d{1,4})\s+"
        r"([A-Za-z][A-Za-z .'-]*?)\s+([A-Z]{2,4})\s+(\d{1,4})"
    )
    # Status prefix that Action Network puts before the away team in cell 0.
    # Without stripping this, team_re greedily eats the status word as part
    # of the away team name (e.g. "Final Mariners SEA 925 ..." was parsed
    # as away_team="Final Mariners"), breaking team-match in the UI.
    status_prefix_re = _re.compile(
        r"^("
        # Final / Final - 10 (extra-inning MLB) / Final - OT / Final/SO (NHL)
        r"Final(?:\s*[-/]\s*(?:\d+|2OT|3OT|OT|SO|F))?"
        r"|Postponed|PPD"
        r"|Cancell?ed|Delayed|Suspended"
        # MLB live: TOP/BOT/MID/END Nth (optional ": 0-0, 2 Out")
        r"|(?:TOP|BOT|MID|END)\s+\d+(?:ST|ND|RD|TH)(?:\s*:[^A-Za-z]*?Out)?"
        # NHL live: "1ST 18:42", "OT 4:23", "INT", "SHOOTOUT"
        r"|\d+(?:ST|ND|RD|TH)(?:\s+PER)?(?:\s+\d{1,2}:\d{2})?"
        r"|OT(?:\s+\d{1,2}:\d{2})?"
        r"|INT|INTERMISSION|SHOOTOUT|SO"
        # NBA/NFL: "1ST QTR", "HALF/HALFTIME"
        r"|\d+(?:ST|ND|RD|TH)\s+QTR"
        r"|HALF|HALFTIME"
        # Pre-game time stamps
        r"|\d{1,2}:\d{2}\s*(?:AM|PM)(?:\s+ET)?"
        r")\s+",
        _re.IGNORECASE,
    )
    pct_re = _re.compile(r"(\d{1,3})\s*%")
    diff_re = _re.compile(r"([+-]\d+)\s*%")

    events: list[dict] = []
    parse_warnings = 0
    failed_samples: list[str] = []
    for tr in rows[1:]:
        cells = [td.get_text(" ", strip=True) for td in tr.find_all(["td", "th"])]
        if len(cells) < 5:
            continue

        cell0 = cells[0]
        sm = status_prefix_re.match(cell0)
        if sm:
            status = sm.group(1).strip()
            cell0_after_status = cell0[sm.end():]
        else:
            status = ""
            cell0_after_status = cell0

        m = team_re.search(cell0_after_status)
        if not m:
            parse_warnings += 1
            # Capture up to 5 failing cells so we can spot which status
            # patterns (NHL "Final/OT", "1ST 18:42", etc.) we're missing.
            if len(failed_samples) < 5:
                failed_samples.append(cell0[:200])
            continue
        away_name, away_abbr, away_id, home_name, home_abbr, home_id = m.groups()
        # Anything between status prefix and the team match is unexpected
        # (shouldn't happen, but if it does prefer it over "scheduled").
        leftover = cell0_after_status[:m.start()].strip()
        if leftover and not status:
            status = leftover
        if not status:
            status = "scheduled"

        bets_pcts  = [int(p) for p in pct_re.findall(cells[3] if len(cells) > 3 else "")]
        money_pcts = [int(p) for p in pct_re.findall(cells[4] if len(cells) > 4 else "")]

        ml: dict = {}
        if len(bets_pcts) >= 2:
            ml["away_bets"] = bets_pcts[0]
            ml["home_bets"] = bets_pcts[1]
        if len(money_pcts) >= 2:
            ml["away_money"] = money_pcts[0]
            ml["home_money"] = money_pcts[1]

        # Sharp signal: % money diverging from % bets by N points.
        # Action Network publishes a "+8 %" style diff in cell 5 — we
        # carry it through too in case the UI wants to surface it directly.
        sharp_diff = None
        if len(cells) > 5:
            md = diff_re.search(cells[5])
            if md:
                try:
                    sharp_diff = int(md.group(1))
                except ValueError:
                    pass

        events.append({
            "status":     status,
            "away_team":  away_name.strip(),
            "away_abbr":  away_abbr,
            "home_team":  home_name.strip(),
            "home_abbr":  home_abbr,
            "ml":         ml,
            "sharp_diff": sharp_diff,
        })

    return {
        "ok":              bool(events),
        "events":          events,
        "table_count":     len(tables),
        "parse_warnings":  parse_warnings,
        "rows_seen":       len(rows),
        "failed_samples":  failed_samples,
    }


@app.route("/api/splits")
@firebase_auth_required
def api_splits():
    """Public betting splits (% of bets, % of money) per game.
    Source: Action Network public-betting HTML, scraped + cached 30 min.
    Sport: mlb / nba / nhl / nfl / ncaab / ncaaf. MMA / soccer / tennis
    not supported by Action Network's free public splits coverage.
    """
    sport = (request.args.get("sport") or "mlb").lower().strip()
    data = _fetch_action_splits(sport)
    return jsonify(data)


@app.route("/debug-splits")
def debug_splits_page():
    """Auth'd browser-friendly view of /api/splits. Lets us iterate on the
    parser without hitting Action Network from a curl that gets 403'd —
    Vercel's runtime CAN reach them; this page surfaces what we get back."""
    sport = request.args.get("sport", "mlb")
    return ('''<!DOCTYPE html><html><head>
    <script src="https://www.gstatic.com/firebasejs/10.12.0/firebase-app-compat.js"></script>
    <script src="https://www.gstatic.com/firebasejs/10.12.0/firebase-auth-compat.js"></script>
    <script>firebase.initializeApp({apiKey:"AIzaSyDQbjlc7VIYmFjbhq119Cl1-JhuXwKq0fY",authDomain:"kahla-house.firebaseapp.com",projectId:"kahla-house"});</script>
    </head><body style="background:#0b0e13;color:#e2e8f0;font-family:monospace;padding:16px;font-size:11px">
    <h2 style="color:#f59e0b">Splits diagnostic — sport=''' + sport + '''</h2>
    <pre id="out" style="white-space:pre-wrap;word-break:break-word">Loading...</pre>
    <script>
    firebase.auth().onAuthStateChanged(async u => {
        if (!u) { document.getElementById("out").textContent = "Not logged in. Go to / first."; return; }
        try {
            const t = await u.getIdToken();
            const r = await fetch("/api/splits?sport=''' + sport + '''", {headers:{Authorization:"Bearer "+t}});
            const d = await r.json();
            document.getElementById("out").textContent = JSON.stringify(d, null, 2);
        } catch (e) {
            document.getElementById("out").textContent = "ERROR: " + e.message;
        }
    });
    </script></body></html>''')


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

# Books we surface on the chart. Aligned with the Odds Board allowlist
# (_ALLOWED_BOOKS). Circa is not in The Odds API at any region; Polymarket
# uses 0-1 probability not American odds; Novig isn't legal in Rob's state.
_CHART_BOOKS = ["PIN", "DK", "FD", "MGM", "CAE", "HR", "BOL"]

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


@app.route("/api/odds/history-batch")
@firebase_auth_required
def api_odds_history_batch():
    """6-hour PIN history for every active game in a sport, batched.
    Powers the inline sparklines rendered in each game card on the Odds Board.

    Returns three series per market — ML home, Spread home, Total over —
    in one response so the page can paint all three sparklines at once
    without N+1 fetches.

    Response: { ok, sport, since_iso, events: {
        "<market_id>": {
            "ml":     { "side": "home", "series": [{ts, price}, ...] },
            "spread": { "side": "home", "series": [{ts, price, line}, ...] },
            "total":  { "side": "over", "series": [{ts, price, line}, ...] }
        }, ...
    } }

    Live-game freeze applied: post-event_start snapshots are dropped so
    the sparkline matches the frozen board cell. Anchor sweep: markets
    with no PIN data inside the 6h window get a flat baseline pinned to
    the window's left edge from the most recent pre-window row.
    """
    sb = get_supabase()
    if sb is None:
        return jsonify({"ok": True, "sport": request.args.get("sport"), "events": {}})

    sport_owls = (request.args.get("sport") or "").lower().strip()
    sport_code = _SPORT_PATH_TO_CODE.get(sport_owls)
    if not sport_code:
        return jsonify({"ok": True, "sport": sport_owls, "events": {}})

    now = datetime.now(timezone.utc)
    low = (now - timedelta(hours=6)).isoformat()
    high = (now + timedelta(days=2)).isoformat()

    try:
        markets = (
            sb.table("markets")
            .select("id,event_start")
            .eq("sport", sport_code)
            .eq("status", "active")
            .gte("event_start", low)
            .lte("event_start", high)
            .limit(500)
            .execute()
            .data
            or []
        )
    except Exception:
        return jsonify({"ok": True, "sport": sport_owls, "events": {}})

    if not markets:
        return jsonify({"ok": True, "sport": sport_owls, "events": {}})

    market_ids = [m["id"] for m in markets]
    six_h = (now - timedelta(hours=6)).isoformat()
    now_iso = now.isoformat()
    event_start_by_mid: dict[str, str] = {m["id"]: m.get("event_start", "") for m in markets}

    def _post_start(mid: str, captured_at: str) -> bool:
        es = event_start_by_mid.get(mid, "")
        return bool(es) and es <= now_iso and captured_at > es

    # The three series we render per game: ML home, Spread home, Total over.
    SERIES = (("moneyline", "home"), ("spread", "home"), ("total", "over"))
    SERIES_KEYS = {"moneyline": "ml", "spread": "spread", "total": "total"}
    wanted_combos = set(SERIES)

    # One Supabase fetch covers all 3 markets × all sides — we filter the
    # sides we want in Python. Smaller payload than 3 separate queries.
    try:
        snaps = (
            sb.table("book_snapshots")
            .select("market_id,market_type,side,price_american,line,captured_at")
            .in_("market_id", market_ids)
            .eq("book", "PIN")
            .in_("market_type", ["moneyline", "spread", "total"])
            .gte("captured_at", six_h)
            .order("captured_at", desc=False)
            .limit(50000)
            .execute()
            .data
            or []
        )
    except Exception:
        snaps = []
    snaps = [s for s in snaps
             if (s["market_type"], s["side"]) in wanted_combos
             and not _post_start(s["market_id"], s["captured_at"])]

    # Anchor sweep — for any (market, market_type, side) combo missing from
    # the fresh window, pull the most recent pre-window row and pin it to
    # the window's left edge so the chart starts with a flat baseline.
    present = {(s["market_id"], s["market_type"], s["side"]) for s in snaps}
    need_anchor = any(
        (mid, mt, side) not in present
        for mid in market_ids for mt, side in SERIES
    )
    if need_anchor:
        try:
            anchor_rows = (
                sb.table("book_snapshots")
                .select("market_id,market_type,side,price_american,line,captured_at")
                .in_("market_id", market_ids)
                .eq("book", "PIN")
                .in_("market_type", ["moneyline", "spread", "total"])
                .lt("captured_at", six_h)
                .order("captured_at", desc=True)
                .limit(50000)
                .execute()
                .data
                or []
            )
        except Exception:
            anchor_rows = []
        for r in anchor_rows:
            combo = (r["market_type"], r["side"])
            if combo not in wanted_combos:
                continue
            key = (r["market_id"], r["market_type"], r["side"])
            if key in present:
                continue
            if _post_start(r["market_id"], r["captured_at"]):
                continue
            present.add(key)
            r["captured_at"] = six_h
            snaps.append(r)

    out: dict[str, dict[str, dict]] = {}
    for s in snaps:
        mid = s["market_id"]
        key = SERIES_KEYS[s["market_type"]]
        slot = out.setdefault(mid, {}).setdefault(key, {"side": s["side"], "series": []})
        slot["series"].append({
            "ts":    s["captured_at"],
            "price": s["price_american"],
            "line":  s.get("line"),
        })

    # Sort each series by timestamp (anchor rows were appended out of order)
    for game in out.values():
        for slot in game.values():
            slot["series"].sort(key=lambda p: p["ts"])

    return jsonify({
        "ok":         True,
        "sport":      sport_owls,
        "since_iso":  six_h,
        "events":     out,
    })


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

    Books returned: PIN, DK, FD, MGM, CAE, HR, BOL.
    """
    sb = get_supabase()
    if sb is None:
        return jsonify({"ok": False, "error": "Supabase not configured"}), 503

    sport_owls = (request.args.get("sport") or "").lower().strip()
    sport_code = _SPORT_PATH_TO_CODE.get(sport_owls)
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
        evs, books, leagues, last_iso = _fetch_odds_from_snapshots(sport_path)
        out["api_odds_events_count"] = len(evs)
        out["api_odds_books"] = books
        out["api_odds_last_data_iso"] = last_iso
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
# Phase 4 — Sharp Bot (paper bets) admin tracker
# ---------------------------------------------------------------------------

@app.route("/api/sharp-bot")
@admin_required
def api_sharp_bot():
    """JSON for /sharp-bot. Returns:
      pending  — every paper_bets row with status='pending' (no age cap)
      settled  — rows graded in the last 30 days (won/lost/push)
      stats    — per-bot rollup over `settled`: graded count, hit rate
                 (excludes pushes from denominator), total units, ROI
                 per bet (units/graded). Hit rate / ROI are null when
                 there are no resolved bets yet."""
    sb = get_supabase()
    if sb is None:
        return jsonify({"ok": False, "error": "Supabase not configured"}), 503

    now = datetime.now(timezone.utc)
    cutoff_30d = (now - timedelta(days=30)).isoformat()

    cols = ("id,picked_at,bot,sport,event_name,event_start,"
            "market_type,side,entry_book,entry_price,entry_line,"
            "fair_prob,edge_pp,sharp_score,"
            "status,pnl_units,result_score,settled_at")
    try:
        pending = (sb.table("paper_bets").select(cols)
                   .eq("status", "pending")
                   .order("event_start", desc=False)
                   .limit(500).execute().data) or []
        settled = (sb.table("paper_bets").select(cols)
                   .neq("status", "pending")
                   .gte("settled_at", cutoff_30d)
                   .order("settled_at", desc=True)
                   .limit(500).execute().data) or []
    except Exception as e:
        return jsonify({"ok": False, "error": f"Supabase: {e}"}), 500

    stats: dict = {}
    for bot in ("steam", "early", "late"):
        stats[bot] = {"graded": 0, "won": 0, "lost": 0, "push": 0,
                      "units": 0.0, "hit_rate": None, "roi": None}
    for r in settled:
        bot = r.get("bot")
        if bot not in stats:
            continue
        s = stats[bot]
        s["graded"] += 1
        st = r.get("status")
        if st in ("won", "lost", "push"):
            s[st] += 1
        try:
            s["units"] += float(r.get("pnl_units") or 0)
        except (TypeError, ValueError):
            pass
    for s in stats.values():
        decided = s["won"] + s["lost"]
        if decided > 0:
            s["hit_rate"] = round(s["won"] / decided, 4)
        if s["graded"] > 0:
            s["roi"] = round(s["units"] / s["graded"], 4)
        s["units"] = round(s["units"], 3)

    return jsonify({
        "ok":           True,
        "now_iso":      now.isoformat(),
        "window_days":  30,
        "pending":      pending,
        "settled":      settled,
        "stats":        stats,
    })


# ---------------------------------------------------------------------------
# Handicapper Bot — picks tracker
# ---------------------------------------------------------------------------

@app.route("/api/handicapper")
@bot_required
def api_handicapper():
    """JSON for /handicapper. Returns pending picks (no age cap), settled
    picks from the last 30d, and rollup stats. Mirrors /api/sharp-bot's
    shape, but reads from bot_picks (the in-chat handicapper's table).

    Stats: hit_rate (excludes pushes), total units, ROI per pick (units /
    graded). Per-confidence-tier rollup so we can see if 'max' picks
    actually win at higher rates than 'medium'."""
    sb = get_supabase()
    if sb is None:
        return jsonify({"ok": False, "error": "Supabase not configured"}), 503

    # Pick Bot is a picks tracker, not a real-bet ledger. It grades
    # against ESPN final scores using the bot's recommended entry_price
    # and ignores whether the user actually placed the bet on
    # Polymarket. No pmm-sync wiring here.

    now = datetime.now(timezone.utc)
    cutoff_30d = (now - timedelta(days=30)).isoformat()
    # "Today" = MST calendar day, anchored to America/Phoenix (Arizona
    # — no DST, MST year-round). Don't use America/Denver: it flips to
    # MDT in summer and would slide the boundary an hour. Single-user
    # app, hardcoded TZ is fine.
    local_now = now.astimezone(ZoneInfo("America/Phoenix"))
    today_start_local = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    today_start_iso = today_start_local.astimezone(timezone.utc).isoformat()
    cutoff_7d = (now - timedelta(days=7)).isoformat()

    cols = ("id,picked_at,asked_by,query_text,sport,event_name,event_start,"
            "market_type,side,entry_book,entry_price,entry_line,"
            "units,confidence,fair_prob,edge_pp,sharp_score,"
            "analysis_md,reasons,"
            "status,pnl_units,result_score,settled_at,"
            "actual_fill_price,actual_fill_qty,actual_fill_line,actual_fill_pnl,"
            "polymarket_outcome,pmm_side,auto_linked")
    try:
        pending = (sb.table("bot_picks").select(cols)
                   .eq("status", "pending")
                   .order("event_start", desc=False)
                   .limit(500).execute().data) or []
        # Settled list returned to the page = TODAY only. Yesterday's
        # picks aren't displayed — they're in the rolling stats but not
        # visually cluttering the page. 30d data still pulled for stats.
        # Settled list = won/lost/push/void only. 'recommended' rows
        # (auto-logged on dossier view, never linked to a real bet)
        # stay out of pending AND settled — they're invisible to the
        # user but kept in the DB for regression / "what would the
        # bot have suggested" analysis.
        settled_30d = (sb.table("bot_picks").select(cols)
                       .in_("status", ["won", "lost", "push", "void"])
                       .gte("settled_at", cutoff_30d)
                       .order("settled_at", desc=True)
                       .limit(500).execute().data) or []
    except Exception as e:
        return jsonify({"ok": False, "error": f"Supabase: {e}"}), 500
    # Today's slate = picks whose game started today (US/Eastern). Same
    # mental model as the stat buckets above.
    settled = [r for r in settled_30d
               if (r.get("event_start") or "") >= today_start_iso]

    def _new_bucket():
        return {"graded": 0, "won": 0, "lost": 0, "push": 0,
                "pnl": 0.0, "hit_rate": None, "roi": None}

    overall_today  = _new_bucket()
    overall_week   = _new_bucket()
    overall_30d    = _new_bucket()
    by_conf: dict = {c: _new_bucket()
                     for c in ("low", "medium", "high", "whale")}
    # Bucket by EVENT_START, not settled_at. The bettor's "today" =
    # today's slate (picks for games happening today), not "what the
    # resolver happened to grade between UTC midnights". A pick on
    # tonight's game made + graded today belongs in TODAY regardless of
    # when the cron tick that updated the row fired.
    #
    # PnL totals use pnl_units (to-WIN sizing computed from the bot's
    # entry_price + units). The bot is a picks tracker, not a real-
    # bet ledger — it grades against ESPN final scores using the
    # price the bot recommended, ignoring whether the user actually
    # placed the bet on Polymarket at all.
    for r in settled_30d:
        st = r.get("status")
        if st not in ("won", "lost", "push"):
            continue
        try:
            pnl = float(r.get("pnl_units") or 0)
        except (TypeError, ValueError):
            pnl = 0.0
        event_start = r.get("event_start") or ""
        for bucket in (overall_30d, by_conf.get(r.get("confidence"))):
            if bucket is None:
                continue
            bucket["graded"] += 1
            bucket[st] += 1
            bucket["pnl"] += pnl
        if event_start >= cutoff_7d:
            overall_week["graded"] += 1
            overall_week[st] += 1
            overall_week["pnl"] += pnl
        if event_start >= today_start_iso:
            overall_today["graded"] += 1
            overall_today[st] += 1
            overall_today["pnl"] += pnl

    def _finalize(s: dict) -> None:
        decided = s["won"] + s["lost"]
        if decided > 0:
            s["hit_rate"] = round(s["won"] / decided, 4)
        if s["graded"] > 0:
            s["roi"] = round(s["pnl"] / s["graded"], 4)
        s["pnl"] = round(s["pnl"], 3)

    for s in (overall_today, overall_week, overall_30d, *by_conf.values()):
        _finalize(s)

    # Resolver heartbeat — last bot_picks_resolver run from the
    # resolver_runs table. Lets the page show "graded run Nm ago" so
    # the user can see at a glance whether grading is alive.
    resolver: dict | None = None
    try:
        rows = (sb.table("resolver_runs")
                .select("run_at,picks_seen,won,lost,push,unmatched,"
                        "not_final,took_ms,error")
                .eq("kind", "bot_picks")
                .order("run_at", desc=True)
                .limit(1).execute().data) or []
        if rows:
            resolver = rows[0]
    except Exception:
        resolver = None

    return jsonify({
        "ok":           True,
        "now_iso":      now.isoformat(),
        "window_days":  30,
        "pending":      pending,
        "settled":      settled,            # today only (display)
        "stats_today":  overall_today,
        "stats_week":   overall_week,
        "stats_30d":    overall_30d,
        "stats":        overall_30d,        # back-compat alias = 30d
        "stats_by_confidence": by_conf,
        "resolver":     resolver,
    })


@app.route("/api/handicapper/dossier")
@bot_required
def api_handicapper_dossier():
    """Build the live pre-game dossier for one game.

    Query params (one of `q` OR `market_id` required):
      q          — freeform team query, e.g. "Toronto vs Angels"
      market_id  — direct UUID lookup. Used by the click-to-pick game
                   cards on /handicapper to skip the fuzzy match.
      sport      — optional sport hint when using `q`.

    No caching — dossiers are 1:1 user-driven, low volume, and we want
    fresh data every time (line moved? injury just dropped?). Each call
    makes a handful of free public API hits (Supabase + ESPN + MLB Stats
    + Action Network). Typical latency 2-5s."""
    import handicapper_web
    sb = get_supabase()
    if sb is None:
        return jsonify({"ok": False, "error": "Supabase not configured"}), 503
    market_id = (request.args.get("market_id") or "").strip() or None
    q = (request.args.get("q") or "").strip() or None
    if not market_id and not q:
        return jsonify({"ok": False, "error": "missing q or market_id param"}), 400
    sport = request.args.get("sport") or None
    live  = (request.args.get("live") or "").lower() in ("1", "true", "yes")
    try:
        dossier = handicapper_web.build_dossier(
            sb, q, sport, market_id=market_id, live=live)
    except Exception as e:
        return jsonify({"ok": False, "error": f"dossier build failed: {e}"}), 500
    # Auto-log the bot's gates-cleared suggestions to bot_picks with
    # status='recommended' so PMM sync always has a row to link the
    # user's real bet to. Idempotent — no-op if a row already exists
    # for (market_id, market_type, side).
    if dossier.get("ok"):
        try:
            _auto_log_dossier_recs(sb, dossier, asked_by=getattr(g, "uid", None))
        except Exception:
            pass  # don't fail the dossier if logging breaks
    code = 200 if dossier.get("ok") else 404
    return jsonify(dossier), code


def _auto_log_dossier_recs(sb, dossier: dict, asked_by: str | None) -> None:
    """Persist the bot's gates-cleared suggestions to bot_picks with
    status='recommended'. PMM sync flips them to 'pending' when it
    finds a matching real bet; resolver leaves them alone otherwise.
    Forced-lean suggestions (gates_cleared=False) are NOT logged —
    they're noise the bot was forced to surface, not real picks."""
    market_id   = dossier.get("market_id")
    event_name  = dossier.get("event_name")
    event_start = dossier.get("event_start_utc")
    sport       = dossier.get("sport")
    suggestions = dossier.get("suggestions") or []
    if not (market_id and event_name and event_start and sport):
        return
    for s in suggestions:
        if not s.get("gates_cleared"):
            continue
        market_type = s.get("market_type")
        side        = s.get("side")
        fair_amer   = s.get("fair_american")
        units       = s.get("units")
        conf        = s.get("confidence")
        if not (market_type and side and fair_amer is not None and units and conf):
            continue
        # Dedup: skip if a pending or recommended row already covers
        # this combo (any time horizon — recommendations don't expire).
        try:
            existing = (sb.table("bot_picks")
                        .select("id")
                        .eq("market_id", market_id)
                        .eq("market_type", market_type)
                        .eq("side", side)
                        .in_("status", ["pending", "recommended"])
                        .limit(1).execute().data) or []
        except Exception:
            return
        if existing:
            continue
        row = {
            "asked_by":    asked_by or "auto",
            "query_text":  "auto-logged on dossier view",
            "market_id":   market_id,
            "sport":       sport,
            "event_name":  event_name,
            "event_start": event_start,
            "market_type": market_type,
            "side":        side,
            "entry_book":  "PMM",
            "entry_price": int(fair_amer),
            "entry_line":  s.get("pin_line"),
            "units":       int(units),
            "confidence":  conf,
            "fair_prob":   s.get("fair_prob"),
            "sharp_score": s.get("sharp_score"),
            "status":      "recommended",
        }
        try:
            sb.table("bot_picks").insert(row).execute()
        except Exception:
            continue


@app.route("/api/handicapper/games")
@bot_required
def api_handicapper_games():
    """List active games for a sport — pre-game window only. Powers the
    click-to-pick UI on /handicapper.

    Window: events starting in the next 48h (and within the last 90 min,
    so a late-asked-about live game still appears). Sorted by event_start.

    Lightweight: only event_name + start time + ids. The dossier (with
    odds, splits, injuries) is fetched on click via /api/handicapper/dossier."""
    sb = get_supabase()
    if sb is None:
        return jsonify({"ok": False, "error": "Supabase not configured"}), 503
    sport = (request.args.get("sport") or "").upper().strip()
    if not sport:
        return jsonify({"ok": False, "error": "missing sport param"}), 400

    now = datetime.now(timezone.utc)
    after  = (now - timedelta(minutes=90)).isoformat()
    before = (now + timedelta(hours=48)).isoformat()
    try:
        rows = (sb.table("markets")
                .select("id,sport,event_name,event_start,status")
                .eq("status", "active")
                .eq("sport", sport)
                .gte("event_start", after)
                .lte("event_start", before)
                .order("event_start")
                .limit(200).execute().data) or []
    except Exception as e:
        return jsonify({"ok": False, "error": f"Supabase: {e}"}), 500

    # Drop phantom markets — rows that no book has quoted in the last
    # 24h. Markets table is never closed-out (CLAUDE.md note), so stale
    # entries linger and previously showed up as "Tampa @ Montreal"
    # ghost games. Real games (incl. ones with no PIN coverage) have
    # SOMEONE quoting them; phantoms have nobody.
    market_ids = [m["id"] for m in rows]
    live_ids: set[str] = set()
    if market_ids:
        cutoff = (now - timedelta(hours=24)).isoformat()
        try:
            snap_rows = (sb.table("book_snapshots")
                         .select("market_id")
                         .in_("market_id", market_ids)
                         .gte("captured_at", cutoff)
                         .limit(5000).execute().data) or []
            live_ids = {r["market_id"] for r in snap_rows if r.get("market_id")}
        except Exception:
            # Filter query failed — fall through unfiltered rather than
            # black-holing the games tab.
            live_ids = set(market_ids)

    games = []
    for m in rows:
        if m["id"] not in live_ids:
            continue
        en = m.get("event_name") or ""
        away = home = ""
        if " @ " in en:
            parts = en.split(" @ ", 1)
            away, home = parts[0].strip(), parts[1].strip()
        games.append({
            "market_id":   m["id"],
            "event_name":  en,
            "event_start": m["event_start"],
            "sport":       m["sport"],
            "away":        away,
            "home":        home,
        })

    return jsonify({
        "ok":      True,
        "sport":   sport,
        "now_iso": now.isoformat(),
        "count":   len(games),
        "games":   games,
    })


@app.route("/api/handicapper/sport-counts")
@bot_required
def api_handicapper_sport_counts():
    """One-shot count of upcoming games per sport. Powers the
    /handicapper page's dynamic sport-tab ordering — sports with the
    most games go left, off-season sports drop to the right. Same
    pre-game window as /api/handicapper/games (last 90 min through
    next 48h).

    Returns: {ok: True, counts: {MLB: 15, NBA: 0, ...}}
    """
    sb = get_supabase()
    if sb is None:
        return jsonify({"ok": False, "error": "Supabase not configured"}), 503
    now = datetime.now(timezone.utc)
    after  = (now - timedelta(minutes=90)).isoformat()
    before = (now + timedelta(hours=48)).isoformat()
    try:
        rows = (sb.table("markets")
                .select("id,sport")
                .eq("status", "active")
                .gte("event_start", after)
                .lte("event_start", before)
                .limit(2000).execute().data) or []
    except Exception as e:
        return jsonify({"ok": False, "error": f"Supabase: {e}"}), 500

    # Same phantom-market filter as /api/handicapper/games so the tab
    # badges don't count rows nobody quotes.
    market_ids = [r["id"] for r in rows if r.get("id")]
    live_ids: set[str] = set()
    if market_ids:
        cutoff = (now - timedelta(hours=24)).isoformat()
        try:
            snap_rows = (sb.table("book_snapshots")
                         .select("market_id")
                         .in_("market_id", market_ids)
                         .gte("captured_at", cutoff)
                         .limit(20000).execute().data) or []
            live_ids = {r["market_id"] for r in snap_rows if r.get("market_id")}
        except Exception:
            live_ids = set(market_ids)

    counts: dict[str, int] = {}
    for r in rows:
        if r.get("id") not in live_ids:
            continue
        s = (r.get("sport") or "").upper()
        if not s:
            continue
        counts[s] = counts.get(s, 0) + 1
    return jsonify({"ok": True, "counts": counts, "now_iso": now.isoformat()})


# ─────────────── Polymarket position → bot_picks sync ───────────────
# Goal: the user's real Polymarket positions become the source of truth
# for "did I take this pick" + actual fill price + resolution. Two
# things happen per position:
#   1. If there's a matching `bot_picks` row (same market_id /
#      market_type / side) without actual_fill_* populated → update
#      it with the user's actual entry.
#   2. If no matching pick exists → auto-create a `bot_picks` row with
#      `auto_linked=true` so the user's off-script bets still appear
#      in pending / settled stats.
#
# Resolution: when PMM settles a position (closed, realized P&L
# populated), grade the linked bot_pick from PMM rather than waiting
# on ESPN. ESPN resolver still handles unlinked picks (Leans the user
# didn't actually bet).
#
# Dry-run mode (default) returns the planned actions as JSON without
# touching the DB — used to validate the mapping logic before flipping
# to writes.

def _pmm_find_market(extracted: dict, sb,
                     debug: dict | None = None) -> tuple | None:
    """Wrapper around `_clv_find_market` that filters out phantom
    markets and uses an ET-based date window (PMM slugs use ET dates,
    so an NHL game starting after 8 PM ET spills into next-day UTC
    and a strict UTC window would miss it).

    `debug` (if passed) gets populated with markets_in_window /
    sample_event_names / markets_with_snapshots so the sync diagnostic
    can show why a position was skipped.

    Returns (market_id, away, home, user_side, event_start_iso) or None.
    """
    sport     = extracted["sport"]
    bet_date  = extracted["bet_date"]
    team_name = (extracted.get("team_name") or "").lower().strip()
    if not team_name:
        if debug is not None:
            debug["reason"] = "team_name empty"
        return None

    # ET-based window: bet_date 00:00 ET = previous day 04:00 UTC
    # (DST-agnostic 4h offset; close enough for matching purposes).
    # Adds a ~28h window covering all games the slug could refer to.
    try:
        d = datetime.strptime(bet_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        if debug is not None:
            debug["reason"] = f"bad bet_date {bet_date}"
        return None
    after  = (d + timedelta(hours=4)).isoformat()           # 04:00 UTC same day
    before = (d + timedelta(days=1, hours=8)).isoformat()   # 08:00 UTC next day
    try:
        markets = (sb.table("markets")
                   .select("id,event_name,event_start")
                   .eq("sport", sport)
                   .gte("event_start", after)
                   .lte("event_start", before)
                   .limit(50).execute().data) or []
    except Exception as e:
        if debug is not None:
            debug["reason"] = f"markets query: {e}"
        return None
    if debug is not None:
        debug["window"] = {"after": after, "before": before}
        debug["markets_in_window"] = len(markets)
        debug["sample_event_names"] = [m.get("event_name") for m in markets[:8]]
    if not markets:
        if debug is not None:
            debug["reason"] = "no markets in date window"
        return None

    # No phantom filter for sync — the markets table is the source of
    # truth for "what games exist". An absent snapshot just means the
    # cron hasn't ingested odds for the game yet (off-season Odds API
    # gaps, late-added games, etc.), NOT that the game is fake. The
    # CLV path used to filter these because CLV needs snapshots for
    # the math; sync just needs the market_id. Diagnostic info still
    # emitted so the response shows snapshot coverage.
    if debug is not None:
        try:
            market_ids = [m["id"] for m in markets]
            cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
            snap = (sb.table("book_snapshots")
                    .select("market_id")
                    .in_("market_id", market_ids)
                    .gte("captured_at", cutoff)
                    .limit(5000).execute().data) or []
            live_ids = {r["market_id"] for r in snap if r.get("market_id")}
            debug["markets_with_snapshots"] = len(live_ids)
        except Exception:
            debug["markets_with_snapshots"] = -1

    try:
        from rapidfuzz import fuzz
    except ImportError:
        if debug is not None:
            debug["reason"] = "rapidfuzz not installed"
        return None

    # Extract slug team codes for fallback matching on TOT/SPR bets
    # whose raw_outcome is "Over"/"Under" / "+1.5" rather than a team
    # name. Slug format: `<prefix>-<sport>-<away_code>-<home_code>-<date>`.
    slug_codes = extracted.get("slug_codes") or {}
    away_code = (slug_codes.get("away") or "").lower()
    home_code = (slug_codes.get("home") or "").lower()

    def _code_matches_event(code: str, side_name: str) -> bool:
        """Heuristic: PMM uses standard team abbreviations (stl, sd, tb,
        lad, nyy, etc.) that aren't trivially derivable from the
        full event name. Use _TEAM_CODE_MAP for known mappings and
        fall back to substring containment for the easy cases."""
        if not code or not side_name:
            return False
        side_low = side_name.lower()
        full = _TEAM_CODE_MAP.get(code)
        if full and full.lower() in side_low:
            return True
        # Last-resort: hyphen-collapsed substring (catches "sd" → "san diego"
        # when the abbreviation forms by initials of multi-word names).
        if code in side_low.replace(" ", "").replace(".", ""):
            return True
        return False

    best = None
    best_score = 0
    slug_code_winner = None  # exact match from slug codes — beats fuzz
    score_table: list[dict] = []
    for m in markets:
        ev = m.get("event_name") or ""
        if " @ " not in ev:
            continue
        away, home = ev.split(" @ ", 1)

        s_a = s_h = 0
        if team_name and team_name not in ("over", "under"):
            s_a = fuzz.partial_ratio(team_name, away.lower())
            s_h = fuzz.partial_ratio(team_name, home.lower())
            max_s = max(s_a, s_h)
            score_table.append({"event": ev, "s_away": s_a, "s_home": s_h})
            if max_s > best_score and max_s >= 75:
                best_score = max_s
                user_side = "away" if s_a >= s_h else "home"
                best = (m["id"], away, home, user_side, m["event_start"])

        # Slug-code path: an event whose BOTH team codes match the
        # slug. Deterministic — beats fuzz unconditionally. Resolves
        # ambiguities like team_name="Canadiens" tying 100% across
        # both Buffalo@MTL and Tampa@MTL: slug codes (buf, mon) only
        # match Buffalo@MTL.
        if away_code and home_code and not slug_code_winner:
            if _code_matches_event(away_code, away) and _code_matches_event(home_code, home):
                # Pick side from fuzz when available — for ML bets the
                # team_name disambiguates which side the user took.
                # For TOT/SPR, side gets resolved later in the intent
                # function from the Over/Under outcome direction.
                code_side = "home"
                if s_a or s_h:
                    code_side = "away" if s_a >= s_h else "home"
                slug_code_winner = (m["id"], away, home, code_side, m["event_start"])
                score_table.append({"event": ev, "slug_match": True})

    if slug_code_winner:
        best = slug_code_winner
        best_score = 100

    if debug is not None:
        debug["fuzz_scores"] = score_table
        debug["best_score"] = best_score
        debug["slug_code_match"] = slug_code_winner is not None
        if not best:
            debug["reason"] = (f"no candidate scored >= 75 against "
                               f"team_name='{team_name}' and no slug-code "
                               f"match for ({away_code}, {home_code})")
    return best


def _pmm_resolve_user_side(market_type: str, matched_side: str,
                           pmm_side: str, entry_share: float,
                           extracted: dict) -> str | None:
    """Decide which side of the market (home/away or over/under) the
    user actually bet on, given the raw PMM data. Verified empirically
    against the user's PMM app screenshots (May 2026):

    For ML/SPR:
      - pmm_side=YES  → user bet the team named in polymarket_outcome.
        Use matched_side (which was derived from fuzzy-matching the
        team name against event_name's away/home).
      - pmm_side=NO + entry_share < 0.50 → standard binary "bet
        against named outcome" pattern. The named outcome is the
        favorite (high price); user paid underdog price for NO. Flip
        matched_side to get the OTHER team.
      - pmm_side=NO + entry_share ≥ 0.50 → SDK anomaly where bet on
        the favored team gets recorded with negative netPosition
        (likely BUY_SHORT routing per CLAUDE.md gotcha #23). User
        actually bet the named team — DO NOT flip.

    For TOT, raw_outcome contains "Over"/"Under" directly. Same flip
    rule: pmm_side=NO flips over↔under only when share < 0.50.
    """
    if market_type == "total":
        raw_outcome_low = (extracted.get("raw_outcome") or "").lower()
        if "over" in raw_outcome_low:
            user_side = "over"
        elif "under" in raw_outcome_low:
            user_side = "under"
        else:
            return None
        if pmm_side == "NO" and entry_share < 0.50:
            user_side = "under" if user_side == "over" else "over"
        return user_side

    # ML / SPR — start from fuzzy-matched team side
    if pmm_side == "NO" and entry_share < 0.50:
        return "away" if matched_side == "home" else "home"
    return matched_side


def _pmm_position_to_intent(slug: str, pos: dict, sb) -> dict:
    """Map one Polymarket position into a sync intent dict."""
    meta = pos.get("marketMetadata") or {}
    extracted = _clv_extract_match_info(meta)
    if not extracted:
        return {"action": "skip", "reason": "non-sport / unparseable meta", "slug": slug}

    match_debug: dict = {}
    matched = _pmm_find_market(extracted, sb, match_debug)
    if not matched:
        return {"action": "skip",
                "reason": match_debug.get("reason") or "no markets row for this game",
                "slug": slug, "extracted": extracted, "match_debug": match_debug}

    market_id, away, home, user_side, event_start = matched
    market_type = extracted["market_type"]

    # PMM `netPosition` sign tells us YES vs NO on the contract.
    net = _safe_float(pos.get("netPosition")) or 0
    qty = abs(net)
    cost = _safe_float(pos.get("cost"))
    if qty <= 0 or cost is None:
        return {"action": "skip", "reason": "no fill yet (qty=0 or cost missing)", "slug": slug}
    pmm_side = "YES" if net >= 0 else "NO"

    entry_share_price = cost / qty
    actual_fill_amer = _prob_to_amer_py(entry_share_price)

    # Side resolution — see _pmm_resolve_user_side for the share-price
    # heuristic that handles PMM's BUY_SHORT routing quirk on favored
    # teams (verified against the user's app screenshots).
    user_side = _pmm_resolve_user_side(market_type, user_side, pmm_side,
                                       entry_share_price, extracted)
    if user_side is None:
        return {"action": "skip",
                "reason": f"can't infer side from outcome={extracted.get('raw_outcome')}",
                "slug": slug}

    return {
        "action":             "sync",
        "slug":               slug,
        "polymarket_outcome": extracted.get("raw_outcome") or (meta.get("outcome") or ""),
        "market_id":          market_id,
        "market_type":        market_type,
        "side":               user_side,
        "actual_fill_price":  actual_fill_amer,
        "actual_fill_qty":    qty,
        "actual_fill_share":  round(entry_share_price, 4),
        "pmm_side":           pmm_side,
        "entry_line":         extracted.get("point"),
        "event_name":         f"{away} @ {home}",
        "event_start":        event_start,
        "sport":              extracted["sport"],
    }


def _pmm_pnl_units(status: str, entry_price: int, units: int) -> float:
    """To-WIN sizing math, mirrored from kahla-scanner's
    bot_picks_resolver._pnl_units. Win = +units regardless of price;
    loss = units × (stake/100 ratio at the entry price). Keep these
    two implementations in sync — both reference the same convention
    documented in CLAUDE.md."""
    if status in ("push", "void"):
        return 0.0
    if status == "won":
        return float(units)
    p = int(entry_price)
    if p > 0:
        return -units * (100.0 / p)
    return -units * (abs(p) / 100.0)


def _pmm_settled_to_intent(act: dict, sb,
                           match_debug: dict | None = None) -> dict:
    """Map a Polymarket POSITION_RESOLUTION activity into a settled-side
    sync intent. Returns the same shape as `_pmm_position_to_intent`
    plus resolution fields (won, settled_at). Caller decides whether
    to update an existing bot_picks row or auto-create a new one with
    status=won/lost and pnl_units pre-computed."""
    detail = act.get("positionResolution") or {}
    if not detail:
        return {"action": "skip", "reason": "no resolution detail"}
    before = detail.get("beforePosition") or {}
    meta = before.get("marketMetadata") or {}
    extracted = _clv_extract_match_info(meta)
    if not extracted:
        return {"action": "skip", "reason": "non-sport / unparseable meta"}

    _debug: dict = match_debug if match_debug is not None else {}
    matched = _pmm_find_market(extracted, sb, _debug)
    if not matched:
        return {"action": "skip",
                "reason": _debug.get("reason") or "no markets row",
                "extracted": extracted, "match_debug": _debug}

    market_id, away, home, user_side, event_start = matched
    market_type = extracted["market_type"]

    net = _safe_float(before.get("netPosition")) or 0
    qty = abs(net)
    cost = _safe_float(before.get("cost"))
    if qty <= 0 or cost is None:
        return {"action": "skip", "reason": "no fill data on settled position"}
    pmm_side = "YES" if net >= 0 else "NO"

    entry_share = cost / qty
    actual_fill_amer = _prob_to_amer_py(entry_share)

    # Side determination — see _pmm_resolve_user_side for the rules.
    # Verified empirically against the user's PMM app screenshots:
    # the SDK reports negative netPosition (pmm_side=NO) for SOME
    # bets on the favored team's outcome (likely BUY_SHORT routing).
    # The share price disambiguates: share ≥ 0.50 = paid favorite
    # price = bet the named team regardless of pmm_side; share < 0.50
    # = paid underdog price = standard binary "bet against named" flip.
    user_side = _pmm_resolve_user_side(market_type, user_side, pmm_side,
                                       entry_share, extracted)
    if user_side is None:
        return {"action": "skip",
                "reason": f"can't infer side from outcome={extracted.get('raw_outcome')}"}

    # Which side resolved? POSITION_RESOLUTION_SIDE_YES means the YES
    # contract paid out; user with held YES won, user with held NO lost
    # (and vice versa).
    res_side = (detail.get("side") or "").replace("POSITION_RESOLUTION_SIDE_", "")
    held_yes = net > 0
    yes_won = res_side in ("YES", "LONG")
    no_won  = res_side in ("NO", "SHORT")
    if not (yes_won or no_won):
        # Push / void resolution (e.g. canceled market). Treat as void.
        outcome_status = "void"
    elif (held_yes and yes_won) or ((not held_yes) and no_won):
        outcome_status = "won"
    else:
        outcome_status = "lost"

    # PMM's real dollar PnL on the actual fill — separate from the
    # bot-recommendation pnl_units. 1 contract pays $1 at resolution.
    if outcome_status == "won":
        actual_fill_pnl = round((1.0 - entry_share) * qty, 2)
    elif outcome_status == "lost":
        actual_fill_pnl = round(-entry_share * qty, 2)
    else:
        actual_fill_pnl = 0.0

    timestamp = detail.get("updateTime") or detail.get("timestamp") or ""

    return {
        "action":             "settled",
        "slug":               meta.get("slug") or extracted.get("raw_outcome", ""),
        "polymarket_outcome": extracted.get("raw_outcome") or "",
        "market_id":          market_id,
        "market_type":        market_type,
        "side":               user_side,
        "actual_fill_price":  actual_fill_amer,
        "actual_fill_qty":    qty,
        "actual_fill_share":  round(entry_share, 4),
        "actual_fill_pnl":    actual_fill_pnl,
        "pmm_side":           pmm_side,
        "entry_line":         extracted.get("point"),
        "event_name":         f"{away} @ {home}",
        "event_start":        event_start,
        "sport":              extracted["sport"],
        "outcome_status":     outcome_status,
        "settled_at":         timestamp,
    }


def _pmm_trade_close_to_intent(act: dict, sb,
                                match_debug: dict | None = None) -> dict:
    """Map an ACTIVITY_TYPE_TRADE that closes (or partially closes) a
    position into a settled-side sync intent. Catches the cases the
    POSITION_RESOLUTION path misses — manual sells before resolution
    AND auto-redemptions that fire as a TRADE rather than a separate
    resolution event."""
    detail = act.get("trade") or {}
    if not detail:
        return {"action": "skip", "reason": "no trade detail"}

    before = detail.get("beforePosition") or {}
    after = detail.get("afterPosition") or {}
    bq = abs(_safe_float(before.get("netPosition")) or 0)
    aq = abs(_safe_float(after.get("netPosition")) or 0)
    sdk_rpnl = _safe_float(detail.get("realizedPnl"))
    is_close = (sdk_rpnl is not None) or (bq > aq)
    if not is_close:
        return {"action": "skip", "reason": "not a closing trade"}

    meta = before.get("marketMetadata") or after.get("marketMetadata") or {}
    extracted = _clv_extract_match_info(meta)
    if not extracted:
        return {"action": "skip", "reason": "non-sport / unparseable meta"}

    _debug: dict = match_debug if match_debug is not None else {}
    matched = _pmm_find_market(extracted, sb, _debug)
    if not matched:
        return {"action": "skip",
                "reason": _debug.get("reason") or "no markets row",
                "extracted": extracted, "match_debug": _debug}

    market_id, away, home, user_side, event_start = matched
    market_type = extracted["market_type"]

    cost_basis = _safe_float(before.get("cost"))
    if cost_basis is None or bq <= 0:
        return {"action": "skip", "reason": "no entry cost basis"}

    entry_share = cost_basis / bq
    actual_fill_amer = _prob_to_amer_py(entry_share)

    # Same side-resolution logic as POSITION_RESOLUTION — derive YES/NO
    # from the BEFORE position's signed netPosition, then apply the
    # share-price heuristic via _pmm_resolve_user_side.
    net = _safe_float(before.get("netPosition")) or 0
    pmm_side = "YES" if net >= 0 else "NO"
    user_side = _pmm_resolve_user_side(market_type, user_side, pmm_side,
                                       entry_share, extracted)
    if user_side is None:
        return {"action": "skip",
                "reason": f"can't infer side from outcome={extracted.get('raw_outcome')}"}

    sold_qty = bq - aq
    sell_revenue = _safe_float(detail.get("cost"))
    if sell_revenue is None or sold_qty <= 0:
        return {"action": "skip", "reason": "no sell revenue/qty"}

    cost_basis_for_sold = entry_share * sold_qty
    actual_fill_pnl = round(sell_revenue - cost_basis_for_sold, 2)
    # Won/lost from realized PnL sign. Sells at break-even (within 1¢)
    # are treated as push — rare but covers the case where the user
    # closed out at exactly cost basis to free capital.
    if actual_fill_pnl > 0.01:
        outcome_status = "won"
    elif actual_fill_pnl < -0.01:
        outcome_status = "lost"
    else:
        outcome_status = "push"

    timestamp = detail.get("updateTime") or detail.get("timestamp") or ""

    return {
        "action":             "settled",
        "slug":               meta.get("slug") or extracted.get("raw_outcome", ""),
        "polymarket_outcome": extracted.get("raw_outcome") or "",
        "market_id":          market_id,
        "market_type":        market_type,
        "side":               user_side,
        "actual_fill_price":  actual_fill_amer,
        "actual_fill_qty":    sold_qty,
        "actual_fill_share":  round(entry_share, 4),
        "actual_fill_pnl":    actual_fill_pnl,
        "pmm_side":           pmm_side,
        "entry_line":         extracted.get("point"),
        "event_name":         f"{away} @ {home}",
        "event_start":        event_start,
        "sport":              extracted["sport"],
        "outcome_status":     outcome_status,
        "settled_at":         timestamp,
        "source":             "trade_close",
    }


def _pmm_units_for_qty(qty: float) -> tuple[int, str]:
    """Map an actual fill quantity to the closest (units, confidence)
    pair from our 1/3/5/10 tier scheme. 1 contract = 1 unit per the
    user's convention. Rounds DOWN to the nearest tier so 8 contracts
    becomes 5u not 10u — don't auto-inflate the user's confidence."""
    q = max(1, int(qty))   # floor not round
    tiers = [(10, "whale"), (5, "high"), (3, "medium"), (1, "low")]
    for u, conf in tiers:
        if q >= u:
            return u, conf
    return 1, "low"


def _pmm_sync_run(dry_run: bool = True) -> dict:
    """Pull current Polymarket positions and reconcile against bot_picks.
    Default dry-run returns the planned actions without writing.
    `dry_run=False` performs the updates / inserts."""
    sb = get_supabase()
    if sb is None:
        return {"ok": False, "error": "Supabase not configured"}
    try:
        client = get_client()
        positions = fetch_positions(client)
    except Exception as e:
        return {"ok": False, "error": f"PMM fetch: {e}"}

    summary: dict = {
        "ok":              True,
        "dry_run":         dry_run,
        "total_positions": len(positions),
        "linked":          0,
        "auto_created":    0,
        "already_linked":  0,
        "skipped":         0,
        "errors":          [],
    }
    actions: list[dict] = []

    for slug, pos in positions:
        intent = _pmm_position_to_intent(slug, pos, sb)
        if intent.get("action") != "sync":
            summary["skipped"] += 1
            actions.append(intent)
            continue

        # Look up an existing pick for this game/side. Accept ANY
        # status — pending/recommended are the common paths (sync flips
        # to pending on link), but already-graded rows (won/lost/push,
        # via the ESPN resolver) still need their actual_fill attached
        # so they render as BET · REC instead of REC ONLY. Without this
        # filter expansion, settled-but-still-unredeemed PMM positions
        # auto-create duplicates instead of linking.
        try:
            existing = (sb.table("bot_picks")
                        .select("id,actual_fill_price,units,confidence,asked_by,status")
                        .eq("market_id", intent["market_id"])
                        .eq("market_type", intent["market_type"])
                        .eq("side", intent["side"])
                        .order("picked_at", desc=True)
                        .limit(1).execute().data) or []
        except Exception as e:
            summary["errors"].append(f"lookup {slug}: {e}")
            actions.append({**intent, "action": "error", "error": str(e)})
            continue

        if existing:
            pick = existing[0]
            already_linked = pick.get("actual_fill_price") is not None
            if already_linked:
                summary["already_linked"] += 1
                actions.append({**intent, "action": "already_linked", "pick_id": pick["id"]})
                continue

            # Preserve the existing grade if the row is already settled
            # — don't flip a 'lost' row back to 'pending' just because
            # PMM still shows it as unredeemed. Only flip status when
            # the row is in recommended/pending.
            cur_status = pick.get("status") or "pending"
            update_fields = {
                "actual_fill_price":  intent["actual_fill_price"],
                "actual_fill_qty":    intent["actual_fill_qty"],
                "actual_fill_line":   intent.get("entry_line"),
                "actual_fill_at":     datetime.now(timezone.utc).isoformat(),
                "polymarket_slug":    intent["slug"],
                "polymarket_outcome": intent["polymarket_outcome"],
                "pmm_side":           intent["pmm_side"],
            }
            if cur_status in ("pending", "recommended"):
                update_fields["status"] = "pending"

            actions.append({**intent, "action": "link", "pick_id": pick["id"],
                            "prev_status": cur_status})
            if not dry_run:
                try:
                    sb.table("bot_picks").update(update_fields).eq("id", pick["id"]).execute()
                except Exception as e:
                    summary["errors"].append(f"link {slug}: {e}")
            summary["linked"] += 1
        else:
            # Auto-create. Recommendation = actual fill (no bot read
            # exists, so the "recommendation" mirrors what the user did).
            units, conf = _pmm_units_for_qty(intent["actual_fill_qty"])
            insert_row = {
                "asked_by":           "pmm_sync",
                "query_text":         "auto-linked from Polymarket position",
                "market_id":          intent["market_id"],
                "sport":              intent["sport"],
                "event_name":         intent["event_name"],
                "event_start":        intent["event_start"],
                "market_type":        intent["market_type"],
                "side":               intent["side"],
                "entry_book":         "PMM",
                "entry_price":        intent["actual_fill_price"],
                "entry_line":         intent.get("entry_line"),
                "units":              units,
                "confidence":         conf,
                "auto_linked":        True,
                "actual_fill_price":  intent["actual_fill_price"],
                "actual_fill_qty":    intent["actual_fill_qty"],
                "actual_fill_line":   intent.get("entry_line"),
                "actual_fill_at":     datetime.now(timezone.utc).isoformat(),
                "polymarket_slug":    intent["slug"],
                "polymarket_outcome": intent["polymarket_outcome"],
                "pmm_side":           intent["pmm_side"],
                "status":             "pending",
            }
            actions.append({**intent, "action": "auto_create", "row_preview": {
                "units": units, "confidence": conf,
                "entry_book": "PMM", "entry_price": intent["actual_fill_price"]}})
            if not dry_run:
                try:
                    sb.table("bot_picks").insert(insert_row).execute()
                except Exception as e:
                    summary["errors"].append(f"insert {slug}: {e}")
            summary["auto_created"] += 1

    # ─── Settled side: backfill POSITION_RESOLUTION activities ───
    # Open positions disappear from `positions()` once they resolve, so
    # the sync above misses any bet that already settled. We also scan
    # recent activities for POSITION_RESOLUTION events. Bounded to the
    # last 48h — that's when the Pick Bot went live; anything older
    # isn't worth backfilling.
    try:
        activities = fetch_activities(client)
    except Exception as e:
        summary["errors"].append(f"activities fetch: {e}")
        activities = []
    # 14 days back covers our 30-day stats window with margin. The
    # earlier 48h cutoff silently lost actual_fill_pnl on any pick whose
    # PMM activity feed entry rotated out of the window before the next
    # auto-trigger run linked it — bot stats then read NULL → contribute
    # $0, understating real losses (and overstating real wins on the
    # next refresh).
    settled_cutoff = (datetime.now(timezone.utc) - timedelta(days=14))
    summary["settled_linked"]       = 0
    summary["settled_auto_created"] = 0
    summary["settled_already_done"] = 0
    summary["settled_skipped"]      = 0

    # Diagnostic: count activity types so we can see what we're
    # actually working with. Helps debug "sync didn't link" reports.
    summary["activity_types_seen"] = {}
    summary["settled_skip_reasons"] = {}
    for act in activities:
        t = act.get("type", "unknown")
        summary["activity_types_seen"][t] = summary["activity_types_seen"].get(t, 0) + 1

    for act in activities:
        act_type = act.get("type")
        # Iterate BOTH POSITION_RESOLUTION (clean automatic resolves)
        # AND TRADE (sells before resolution + auto-redemptions that
        # fire as a closing trade). The dashboard's compute_summary
        # treats them the same way; sync needs to do the same.
        if act_type == "ACTIVITY_TYPE_POSITION_RESOLUTION":
            ts = (act.get("positionResolution") or {}).get("updateTime") or ""
            intent_fn = _pmm_settled_to_intent
        elif act_type == "ACTIVITY_TYPE_TRADE":
            ts = (act.get("trade") or {}).get("updateTime") or ""
            intent_fn = _pmm_trade_close_to_intent
        else:
            continue

        try:
            ts_dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            if ts_dt < settled_cutoff:
                continue
        except Exception:
            pass

        intent = intent_fn(act, sb)
        if intent.get("action") != "settled":
            summary["settled_skipped"] += 1
            reason = intent.get("reason") or "unknown"
            summary["settled_skip_reasons"][reason] = (
                summary["settled_skip_reasons"].get(reason, 0) + 1)
            actions.append(intent)
            continue

        try:
            existing = (sb.table("bot_picks")
                        .select("id,status,actual_fill_price,actual_fill_pnl,pnl_units,settled_at")
                        .eq("market_id", intent["market_id"])
                        .eq("market_type", intent["market_type"])
                        .eq("side", intent["side"])
                        .order("picked_at", desc=True)
                        .limit(1).execute().data) or []
        except Exception as e:
            summary["errors"].append(f"settled lookup: {e}")
            continue

        status = intent["outcome_status"]
        if existing:
            pick = existing[0]
            cur_status = pick.get("status")
            if cur_status in ("won", "lost", "push", "void") and pick.get("settled_at"):
                # Already graded by resolver (ESPN). Don't overwrite —
                # the resolver's grade is canonical for the bot's units.
                # Re-attach actual_fill_* when actual_fill_pnl is NULL,
                # even if actual_fill_price was set in a prior partial
                # attach (or by an unrelated path). Previously gated on
                # actual_fill_price alone, which left rows in a stuck
                # "has fill price but no real PnL" state — those rows
                # then contribute $0 to the bot stats forever.
                if pick.get("actual_fill_pnl") is None:
                    update_only_fill = {
                        "actual_fill_price":  intent["actual_fill_price"],
                        "actual_fill_qty":    intent["actual_fill_qty"],
                        "actual_fill_line":   intent.get("entry_line"),
                        "actual_fill_at":     intent["settled_at"] or pick.get("settled_at"),
                        "actual_fill_pnl":    intent["actual_fill_pnl"],
                        "polymarket_slug":    intent["slug"],
                        "polymarket_outcome": intent["polymarket_outcome"],
                        "pmm_side":           intent["pmm_side"],
                    }
                    actions.append({**intent, "action": "settled_attach_fill",
                                    "pick_id": pick["id"]})
                    if not dry_run:
                        try:
                            sb.table("bot_picks").update(update_only_fill).eq("id", pick["id"]).execute()
                        except Exception as e:
                            summary["errors"].append(f"settled fill attach: {e}")
                    summary["settled_linked"] += 1
                else:
                    summary["settled_already_done"] += 1
                    actions.append({**intent, "action": "settled_already_done",
                                    "pick_id": pick["id"]})
                continue
            # Row exists in pending/recommended state → grade it.
            units = int(pick.get("units") or 1) if isinstance(pick, dict) else 1
            entry_price = int(pick.get("entry_price") or intent["actual_fill_price"] or 0)
            # Need to refetch units + entry_price from DB. The above
            # select doesn't include them — extend the select. Quick
            # second fetch:
            try:
                full = (sb.table("bot_picks").select("units,entry_price")
                        .eq("id", pick["id"]).single().execute().data) or {}
                units = int(full.get("units") or units)
                entry_price = int(full.get("entry_price") or entry_price)
            except Exception:
                pass
            pnl_units = _pmm_pnl_units(status, entry_price, units)
            update = {
                "status":             status,
                "pnl_units":          round(pnl_units, 4),
                "settled_at":         intent["settled_at"] or datetime.now(timezone.utc).isoformat(),
                "actual_fill_price":  intent["actual_fill_price"],
                "actual_fill_qty":    intent["actual_fill_qty"],
                "actual_fill_line":   intent.get("entry_line"),
                "actual_fill_at":     intent["settled_at"],
                "actual_fill_pnl":    intent["actual_fill_pnl"],
                "polymarket_slug":    intent["slug"],
                "polymarket_outcome": intent["polymarket_outcome"],
                "pmm_side":           intent["pmm_side"],
            }
            actions.append({**intent, "action": "settled_link",
                            "pick_id": pick["id"], "prev_status": cur_status,
                            "pnl_units": round(pnl_units, 4)})
            if not dry_run:
                try:
                    sb.table("bot_picks").update(update).eq("id", pick["id"]).execute()
                except Exception as e:
                    summary["errors"].append(f"settled link: {e}")
            summary["settled_linked"] += 1
        else:
            # Auto-create a settled row from scratch.
            units, conf = _pmm_units_for_qty(intent["actual_fill_qty"])
            pnl_units = _pmm_pnl_units(status, intent["actual_fill_price"], units)
            insert_row = {
                "asked_by":           "pmm_sync",
                "query_text":         "auto-linked from settled Polymarket position",
                "market_id":          intent["market_id"],
                "sport":              intent["sport"],
                "event_name":         intent["event_name"],
                "event_start":        intent["event_start"],
                "market_type":        intent["market_type"],
                "side":               intent["side"],
                "entry_book":         "PMM",
                "entry_price":        intent["actual_fill_price"],
                "entry_line":         intent.get("entry_line"),
                "units":              units,
                "confidence":         conf,
                "status":             status,
                "pnl_units":          round(pnl_units, 4),
                "settled_at":         intent["settled_at"] or datetime.now(timezone.utc).isoformat(),
                "auto_linked":        True,
                "actual_fill_price":  intent["actual_fill_price"],
                "actual_fill_qty":    intent["actual_fill_qty"],
                "actual_fill_line":   intent.get("entry_line"),
                "actual_fill_at":     intent["settled_at"],
                "actual_fill_pnl":    intent["actual_fill_pnl"],
                "polymarket_slug":    intent["slug"],
                "polymarket_outcome": intent["polymarket_outcome"],
                "pmm_side":           intent["pmm_side"],
            }
            actions.append({**intent, "action": "settled_auto_create",
                            "row_preview": {"units": units, "confidence": conf,
                                            "pnl_units": round(pnl_units, 4),
                                            "status": status}})
            if not dry_run:
                try:
                    sb.table("bot_picks").insert(insert_row).execute()
                except Exception as e:
                    summary["errors"].append(f"settled insert: {e}")
            summary["settled_auto_created"] += 1

    summary["actions"] = actions
    return summary


# Module-level cache so /api/handicapper's 60s page-refresh doesn't
# re-hit Polymarket every time. Sync runs at most every 90s.
_PMM_SYNC_LAST_TS: float = 0.0
_PMM_SYNC_TTL_SEC = 90


def _pmm_sync_autotrigger() -> None:
    """Run the sync in WRITE mode if at least _PMM_SYNC_TTL_SEC has
    elapsed since the last attempt. Called inline from /api/handicapper
    so the user's bets show up on the next page refresh after placing
    them on Polymarket — no manual URL-hitting required. Failures are
    swallowed; the page must keep rendering even if PMM is down."""
    global _PMM_SYNC_LAST_TS
    import time as _time
    if (_time.time() - _PMM_SYNC_LAST_TS) < _PMM_SYNC_TTL_SEC:
        return
    _PMM_SYNC_LAST_TS = _time.time()
    try:
        _pmm_sync_run(dry_run=False)
    except Exception:
        pass


@app.route("/api/handicapper/pmm-sync")
@admin_required
def api_pmm_sync():
    """Reconcile Polymarket positions against bot_picks. Admin only.

    Query params:
      dry=1 (default) — return the planned actions without writing.
      dry=0           — actually update / insert rows.

    Response shape (dry or not):
      {ok, dry_run, total_positions, linked, auto_created, already_linked,
       skipped, errors, actions: [{action, ...intent}]}
    """
    dry = request.args.get("dry", "1") != "0"
    return jsonify(_pmm_sync_run(dry_run=dry))


@app.route("/pmm-sync")
def pmm_sync_page():
    """Auth'd browser-friendly wrapper for /api/handicapper/pmm-sync.
    Always defaults to dry=1 in the URL so you don't accidentally
    write by hitting the bare path. Add ?dry=0 explicitly to write."""
    dry = request.args.get("dry", "1")
    return ('''<!DOCTYPE html><html><head>
    <script src="https://www.gstatic.com/firebasejs/10.12.0/firebase-app-compat.js"></script>
    <script src="https://www.gstatic.com/firebasejs/10.12.0/firebase-auth-compat.js"></script>
    <script>firebase.initializeApp({apiKey:"AIzaSyDQbjlc7VIYmFjbhq119Cl1-JhuXwKq0fY",authDomain:"kahla-house.firebaseapp.com",projectId:"kahla-house"});</script>
    </head><body style="background:#0b0e13;color:#e2e8f0;font-family:monospace;padding:16px;font-size:11px">
    <h2 style="color:#f59e0b">PMM sync — dry=''' + dry + '''</h2>
    <p style="color:#8890a8;font-size:12px">
      <a href="/pmm-sync?dry=1" style="color:#4a7cff">dry=1 (preview)</a> ·
      <a href="/pmm-sync?dry=0" style="color:#ef4444">dry=0 (WRITES — link/create rows)</a>
    </p>
    <pre id="out" style="white-space:pre-wrap;word-break:break-word">Loading...</pre>
    <script>
    firebase.auth().onAuthStateChanged(async u => {
        if (!u) { document.getElementById("out").textContent = "Not logged in. Go to / first."; return; }
        try {
            const t = await u.getIdToken();
            const r = await fetch("/api/handicapper/pmm-sync?dry=''' + dry + '''", {headers:{"Authorization":"Bearer "+t}});
            const d = await r.json();
            document.getElementById("out").textContent = JSON.stringify(d, null, 2);
        } catch (e) {
            document.getElementById("out").textContent = "ERROR: " + e.message;
        }
    });
    </script></body></html>''')


@app.route("/api/handicapper/pick", methods=["POST"])
@bot_required
def api_handicapper_pick():
    """Log a pick to bot_picks. Body matches the bot_picks columns —
    see kahla-scanner/scripts/handicapper_log_pick.py for the same
    shape used by the CLI logger.

    Request JSON:
      market_id, market_type ('moneyline'|'spread'|'total'),
      side ('home'|'away'|'over'|'under'), book, price (int),
      line (number, null for ML), units (1|3|5),
      confidence ('low'|'medium'|'high'|'max'),
      fair_prob, edge_pp, sharp_score, analysis_md, reasons (list),
      query_text, signal_blob (object).

    Idempotent on (market_id, market_type, side) within 7 days unless
    `allow_duplicate=true` is passed."""
    sb = get_supabase()
    if sb is None:
        return jsonify({"ok": False, "error": "Supabase not configured"}), 503

    body = request.get_json(force=True, silent=True) or {}
    required = ["market_id", "market_type", "side", "book", "price",
                "units", "confidence"]
    missing = [k for k in required if body.get(k) is None]
    if missing:
        return jsonify({"ok": False, "error": f"missing: {missing}"}), 400

    if body["market_type"] not in ("moneyline", "spread", "total"):
        return jsonify({"ok": False, "error": "bad market_type"}), 400
    if body["side"] not in ("home", "away", "over", "under"):
        return jsonify({"ok": False, "error": "bad side"}), 400
    if body["confidence"] not in ("low", "medium", "high", "whale"):
        return jsonify({"ok": False, "error": "bad confidence"}), 400
    if body["units"] not in (1, 3, 5, 10):
        return jsonify({"ok": False, "error": "units must be 1/3/5/10"}), 400

    line_val = body.get("line")
    if body["market_type"] in ("spread", "total"):
        if line_val is None:
            return jsonify({"ok": False, "error": "line required for spread/total"}), 400

    try:
        m = (sb.table("markets")
             .select("id,sport,event_name,event_start,status")
             .eq("id", body["market_id"])
             .single().execute().data)
    except Exception as e:
        return jsonify({"ok": False, "error": f"market lookup: {e}"}), 404
    if not m:
        return jsonify({"ok": False, "error": "market not found"}), 404

    if not body.get("allow_duplicate"):
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=168)).isoformat()
        try:
            existing = (sb.table("bot_picks").select("id")
                        .eq("market_id", body["market_id"])
                        .eq("market_type", body["market_type"])
                        .eq("side", body["side"])
                        .gte("picked_at", cutoff)
                        .limit(1).execute().data) or []
        except Exception:
            existing = []
        if existing:
            return jsonify({"ok": True, "skipped": True,
                            "reason": "already logged within 7 days",
                            "existing_id": existing[0]["id"]}), 200

    row = {
        "asked_by":    g.uid,
        "query_text":  body.get("query_text"),
        "market_id":   body["market_id"],
        "sport":       m["sport"],
        "event_name":  m["event_name"],
        "event_start": m["event_start"],
        "market_type": body["market_type"],
        "side":        body["side"],
        "entry_book":  body["book"],
        "entry_price": int(body["price"]),
        "entry_line":  float(line_val) if line_val is not None else None,
        "units":       int(body["units"]),
        "confidence":  body["confidence"],
        "fair_prob":   body.get("fair_prob"),
        "edge_pp":     body.get("edge_pp"),
        "sharp_score": body.get("sharp_score"),
        "analysis_md": body.get("analysis_md"),
        "reasons":     body.get("reasons"),
        "signal_blob": body.get("signal_blob"),
    }
    try:
        res = sb.table("bot_picks").insert(row).execute()
    except Exception as e:
        return jsonify({"ok": False, "error": f"insert: {e}"}), 500
    pick_id = (res.data or [{}])[0].get("id")
    return jsonify({"ok": True, "id": pick_id, "skipped": False}), 201


@app.route("/api/handicapper/pick/<int:pick_id>", methods=["DELETE"])
@bot_required
def api_handicapper_pick_delete(pick_id: int):
    """Delete a pick. Authorization:
      • admin can delete any pick
      • bot_access users can only delete picks they themselves logged
        (asked_by == current uid)
    Use case: accidentally logged pick / user changed their mind /
    pick logged for the wrong side. Hard delete (no soft-delete column)
    since these are personal-tracking rows, not audit data."""
    sb = get_supabase()
    if sb is None:
        return jsonify({"ok": False, "error": "Supabase not configured"}), 503

    try:
        row = (sb.table("bot_picks").select("id,asked_by")
               .eq("id", pick_id).single().execute().data)
    except Exception:
        row = None
    if not row:
        return jsonify({"ok": False, "error": "pick not found"}), 404

    is_admin = g.user_data.get("role") == "admin"
    is_owner = row.get("asked_by") == g.uid
    if not (is_admin or is_owner):
        return jsonify({"ok": False, "error": "not your pick"}), 403

    try:
        sb.table("bot_picks").delete().eq("id", pick_id).execute()
    except Exception as e:
        return jsonify({"ok": False, "error": f"delete failed: {e}"}), 500
    return jsonify({"ok": True, "id": pick_id})


@app.route("/api/handicapper/pick/<int:pick_id>/settle", methods=["POST"])
@bot_required
def api_handicapper_pick_settle(pick_id: int):
    """Manually settle a pending pick. Use case: UFC fights (no auto-grade
    for SPR/TOT method-of-victory bets), or any pick where the resolver
    can't reach ESPN reliably (rare). Body: {status: 'won'|'lost'|'push'}.
    Computes pnl_units via the same to-WIN math the resolver uses.

    Authorization: admin OR owner of the row (asked_by). Same gate as
    the delete endpoint."""
    body = request.get_json(silent=True) or {}
    new_status = (body.get("status") or "").strip()
    if new_status not in ("won", "lost", "push"):
        return jsonify({"ok": False, "error": "status must be won/lost/push"}), 400

    sb = get_supabase()
    if sb is None:
        return jsonify({"ok": False, "error": "Supabase not configured"}), 503

    try:
        row = (sb.table("bot_picks")
               .select("id,asked_by,status,entry_price,units")
               .eq("id", pick_id).single().execute().data)
    except Exception:
        row = None
    if not row:
        return jsonify({"ok": False, "error": "pick not found"}), 404

    is_admin = g.user_data.get("role") == "admin"
    is_owner = row.get("asked_by") == g.uid
    if not (is_admin or is_owner):
        return jsonify({"ok": False, "error": "not your pick"}), 403

    # PnL math mirrors kahla-scanner/scripts/bot_picks_resolver.py
    # _pnl_units (to-WIN sizing). Keep in sync.
    units = row.get("units") or 1
    entry_price = int(row.get("entry_price") or 0)
    if new_status == "push":
        pnl = 0.0
    elif new_status == "won":
        pnl = float(units)
    else:  # lost
        if entry_price > 0:
            pnl = -units * (100.0 / entry_price)
        elif entry_price < 0:
            pnl = -units * (abs(entry_price) / 100.0)
        else:
            pnl = -float(units)

    try:
        sb.table("bot_picks").update({
            "status":     new_status,
            "pnl_units":  pnl,
            "settled_at": datetime.now(timezone.utc).isoformat(),
            "result_score": {"manual": True},
        }).eq("id", pick_id).execute()
    except Exception as e:
        return jsonify({"ok": False, "error": f"update failed: {e}"}), 500
    return jsonify({"ok": True, "id": pick_id, "status": new_status,
                    "pnl_units": round(pnl, 3)})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    debug = os.getenv("FLASK_DEBUG", "0") == "1"
    app.run(host="0.0.0.0", port=port, debug=debug)
