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
import requests as _http  # module-level HTTP client — ESPN scoreboard + Kalshi
                          # fetches use it. (Was accidentally dropped with the
                          # Odds Board removal, which NameError'd every _http.get
                          # → empty ESPN scores. Restored.)
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import firebase_admin
from firebase_admin import auth as fb_auth, credentials, firestore

from dotenv import load_dotenv
from flask import (
    Flask, render_template, redirect, request,
    jsonify, g, make_response, send_file,
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


def book_club_required(f):
    """Require Firebase auth + book_club_access (admin always implicit).
    Used for the Book Club page + API. Independent of bot_access — the
    Book Club is a separate, non-betting surface gated by its own pill."""
    @functools.wraps(f)
    @firebase_auth_required
    def wrapper(*args, **kwargs):
        role = g.user_data.get("role")
        if (role != "admin"
                and not g.user_data.get("book_club_access")
                and not g.user_data.get("book_club_manager")):
            return jsonify({"ok": False, "error": "Book Club access required"}), 403
        return f(*args, **kwargs)
    return wrapper


def grocery_required(f):
    """Require Firebase auth + grocery_access (admin always implicit).
    Gates the family Grocery list page + its API. Independent of every other
    capability — its own pill in User Management."""
    @functools.wraps(f)
    @firebase_auth_required
    def wrapper(*args, **kwargs):
        role = g.user_data.get("role")
        if role != "admin" and not g.user_data.get("grocery_access"):
            return jsonify({"ok": False, "error": "Grocery access required"}), 403
        return f(*args, **kwargs)
    return wrapper


def odds_required(f):
    """Require Firebase auth + odds_access (admin always implicit).
    Gates the Odds Board page + its data endpoints. The `viewer` role no
    longer implies Odds access — it's an explicit per-user pill now."""
    @functools.wraps(f)
    @firebase_auth_required
    def wrapper(*args, **kwargs):
        role = g.user_data.get("role")
        if role != "admin" and not g.user_data.get("odds_access"):
            return jsonify({"ok": False, "error": "Odds access required"}), 403
        return f(*args, **kwargs)
    return wrapper


# ---------------------------------------------------------------------------
# Page routes (serve templates — auth handled client-side by Firebase JS SDK)
# ---------------------------------------------------------------------------

@app.route("/")
def landing():
    return render_template("index.html")


_APPLE_TOUCH_ICON_B64 = (
    "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"
    "eaTsGKcAAAAASUVORK5CYII="
)


# ── Home Screen / favicon icon ──────────────────────────────────────────
# iOS uses <link rel="apple-touch-icon"> (in each template head) when you
# "Add to Home Screen", and also probes /apple-touch-icon*.png at the root.
# We serve one branded "KH" PNG (purple→blue gradient) for every variant.
# Inlined as base64 so it always ships with the function (vercel.json
# routes everything to app.py — there's no static-file handler).
import base64 as _b64
_APPLE_TOUCH_ICON_PNG = _b64.b64decode("".join(_APPLE_TOUCH_ICON_B64.split()))


@app.route("/apple-touch-icon.png")
@app.route("/apple-touch-icon-precomposed.png")
@app.route("/apple-touch-icon-120x120.png")
@app.route("/apple-touch-icon-152x152.png")
@app.route("/apple-touch-icon-167x167.png")
@app.route("/apple-touch-icon-180x180.png")
@app.route("/favicon.ico")
def apple_touch_icon():
    resp = make_response(_APPLE_TOUCH_ICON_PNG)
    resp.headers["Content-Type"] = "image/png"
    resp.headers["Cache-Control"] = "public, max-age=604800"
    return resp


_WELL_RED_LOGO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "well-red-logo.png")


@app.route("/book-club-logo.png")
def book_club_logo():
    """Well Red Book Club logo — served from disk (ships with the function,
    same as templates). Used as the page hero, favicon, and app-card icon."""
    resp = make_response(send_file(_WELL_RED_LOGO, mimetype="image/png"))
    resp.headers["Cache-Control"] = "public, max-age=604800"
    return resp



@app.route("/odds")
def odds_page():
    # Odds Board retired June 2026 — its data (Pinnacle/book_snapshots) was cut
    # at the exchange cutover and the page is gone. Redirect old links home.
    return redirect("/")


@app.route("/dashboard")
def dashboard():
    """Polymarket P&L dashboard — admin only (client-side gated)."""
    return render_template("dashboard.html")


@app.route("/handicapper")
def handicapper_page():
    """Handicapper Bot — admin + bot_access gated (client-side via /api/me)."""
    return render_template("handicapper.html")


@app.route("/games")
def games_page():
    """Card-game scoring sheets — any approved user (client-side gated via /api/me)."""
    resp = make_response(render_template("games.html"))
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp


@app.route("/book-club")
def book_club_page():
    """Book Club — admin + book_club_access gated (client-side via /api/me).
    A separate, non-betting surface (shared reading list, ratings, meetings,
    and next-read voting). Server gate is @book_club_required on the API."""
    resp = make_response(render_template("book_club.html"))
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp


@app.route("/grocery")
def grocery_page():
    """Family grocery list — admin + grocery_access gated (client-side via
    /api/me; server gate is @grocery_required on the API)."""
    resp = make_response(render_template("grocery.html"))
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp


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


# Polymarket renamed the maker-reward activity (May 2026): the old
# ACTIVITY_TYPE_TRANSFER was split into ACTIVITY_TYPE_LIQUIDITY_PROGRAM
# (maker / liquidity-provider rewards) and ACTIVITY_TYPE_TAKER_FEE_REBATE
# (fee rebates). All three are Polymarket paying you — income that rolls
# into the Maker Rewards card + total P&L. These are the human labels
# _activity_type_label() emits, which is what each parsed row's "type"
# field holds. TRANSFER kept for back-compat with older accounts.
_REWARD_TYPE_LABELS = {"Transfer", "Liquidity Program", "Taker Fee Rebate"}


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
        is_maker = act["type"] in _REWARD_TYPE_LABELS and has_pnl

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
        # Maker rewards + fee rebates (May 2026 rename of TRANSFER). Both
        # nest their payload under the accountBalanceChange key.
        "ACTIVITY_TYPE_LIQUIDITY_PROGRAM": "accountBalanceChange",
        "ACTIVITY_TYPE_TAKER_FEE_REBATE": "accountBalanceChange",
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
            # Maker rewards (legacy type) — count as P&L income
            amount = _safe_float(detail.get("amount"))
            market = "Maker Reward"
            pnl = amount
            is_close = False

        elif act_type in ("ACTIVITY_TYPE_LIQUIDITY_PROGRAM",
                          "ACTIVITY_TYPE_TAKER_FEE_REBATE"):
            # Maker / liquidity-provider rewards + taker fee rebates — the
            # May 2026 replacement for ACTIVITY_TYPE_TRANSFER. Payload is
            # under the accountBalanceChange key (mapped above). Income.
            amount = _safe_float(detail.get("amount"))
            market = ("Maker Reward"
                      if act_type == "ACTIVITY_TYPE_LIQUIDITY_PROGRAM"
                      else "Fee Rebate")
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
        return [], [], [], None

    sport_code = _SPORT_PATH_TO_CODE.get(sport_path)
    if not sport_code:
        return [], [], [], None

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
        return [], [], [], None

    if not markets:
        return [], [], [], None

    market_ids = [m["id"] for m in markets]

    # Snapshot freshness window: 18h. Was 90 min back when the cron ran
    # always-on at 30-min cadence and any market missing a row inside
    # that window was clearly dead. Now that cadence is adaptive (5/15/
    # 30-min near games, SKIP outside 18h) a healthy game with PIN
    # sitting on its line might not get a new row for hours — its only
    # snapshot would be many cycles old. 18h matches the cron's "skip
    # beyond this" cap, so any game we're polling for has a snap within
    # this window. Beyond 18h, the cron isn't writing anyway.
    fresh_cutoff = (now - timedelta(hours=18)).isoformat()
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
        return [], [], [], None

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
        "book_club_access": bool(g.user_data.get("book_club_access")) or bool(g.user_data.get("book_club_manager")) or role == "admin",
        "book_club_manager": bool(g.user_data.get("book_club_manager")) or role == "admin",
        "games_access": bool(g.user_data.get("games_access")) or role == "admin",
        "odds_access": bool(g.user_data.get("odds_access")) or role == "admin",
        "grocery_access": bool(g.user_data.get("grocery_access")) or role == "admin",
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
# API routes — Book Club (Firestore-backed, shared across members)
# ---------------------------------------------------------------------------
# Two collections:
#   book_club_books/{id}  — the shared shelf (reading / upcoming / finished),
#                           each carrying optional meeting info + a per-member
#                           ratings map.
#   book_club_polls/{id}  — next-read votes. An admin posts 2-3 candidate
#                           books; members cast one vote each; admin closes.
# Writes go through the Admin SDK (bypasses firestore.rules); the
# @book_club_required decorator is the real access gate. Poll create/close/
# delete is admin-only (checked inline); voting + the shelf are open to any
# book_club member.

_BOOK_CLUB_BOOKS = "book_club_books"
_BOOK_CLUB_POLLS = "book_club_polls"
_BOOK_CLUB_AVAIL = "book_club_availability"
_BOOK_STATUSES = {"reading", "upcoming", "finished"}
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# Display name for the club, used in invite emails (subject/header/footer +
# default sender name). Override via env without a code change if it changes.
BOOK_CLUB_NAME = os.getenv("BOOK_CLUB_NAME", "Well Red Book Club")


def _bc_display_name():
    return g.user_data.get("displayName") or g.user_data.get("email") or "Member"


def _is_club_manager():
    """Club managers (officers) can run votes + manage the shelf without
    being platform admins. Admins are managers implicitly."""
    return g.user_data.get("role") == "admin" or bool(g.user_data.get("book_club_manager"))


def _bc_iso(ts):
    """Serialize a Firestore timestamp / datetime to ISO, else passthrough."""
    return ts.isoformat() if hasattr(ts, "isoformat") else (ts or "")


def _serialize_book(doc_id, data):
    """Shape a book doc for the API and compute the average rating."""
    ratings_map = data.get("ratings") or {}
    ratings = []
    total = 0.0
    for uid, r in ratings_map.items():
        if not isinstance(r, dict):
            continue
        try:
            val = float(r.get("rating") or 0)
        except (TypeError, ValueError):
            val = 0.0
        ratings.append({
            "uid": uid,
            "name": r.get("name") or "Member",
            "rating": val,
            "review": r.get("review") or "",
        })
        total += val
    avg = round(total / len(ratings), 2) if ratings else None
    return {
        "id": doc_id,
        "title": data.get("title") or "",
        "author": data.get("author") or "",
        "cover_url": data.get("cover_url") or "",
        "status": data.get("status") if data.get("status") in _BOOK_STATUSES else "upcoming",
        "meeting_date": data.get("meeting_date") or "",
        "meeting_time": data.get("meeting_time") or "",
        "meeting_location": data.get("meeting_location") or "",
        "notes": data.get("notes") or "",
        "added_by": data.get("added_by") or "",
        "added_by_name": data.get("added_by_name") or "",
        "created_at": _bc_iso(data.get("created_at")),
        "ratings": ratings,
        "avg_rating": avg,
        "rating_count": len(ratings),
    }


def _serialize_poll(doc_id, data, my_uid):
    """Shape a poll doc + tally votes. Reveals who voted for what only as
    counts; surfaces the caller's own current vote separately."""
    options = data.get("options") or []
    votes = data.get("votes") or {}
    tally = {}
    for opt in options:
        tally[opt.get("id")] = 0
    for _voter, opt_id in votes.items():
        if opt_id in tally:
            tally[opt_id] += 1
    total = sum(tally.values())
    out_opts = []
    for opt in options:
        oid = opt.get("id")
        out_opts.append({
            "id": oid,
            "title": opt.get("title") or "",
            "author": opt.get("author") or "",
            "cover_url": opt.get("cover_url") or "",
            "votes": tally.get(oid, 0),
        })
    winner = None
    if out_opts:
        top = max(out_opts, key=lambda o: o["votes"])
        # Only call a winner if there's at least one vote and no tie at the top.
        if top["votes"] > 0 and sum(1 for o in out_opts if o["votes"] == top["votes"]) == 1:
            winner = top["id"]
    return {
        "id": doc_id,
        "question": data.get("question") or "Next read",
        "status": "closed" if data.get("status") == "closed" else "open",
        "options": out_opts,
        "total_votes": total,
        "my_vote": votes.get(my_uid),
        "winner": winner,
        "created_by": data.get("created_by") or "",
        "created_by_name": data.get("created_by_name") or "",
        "created_at": _bc_iso(data.get("created_at")),
    }


# --- Meeting-invite email (Resend) ------------------------------------------
# When a manager sets a book's date + location (the book itself is the third
# piece), an invite emails to every approved book-club member. Auto-fires on
# add/edit, deduped by a signature of (title|date|time|location) stored on the
# book, so unrelated edits don't re-blast but a real date/location change does.
# Requires RESEND_API_KEY (+ a verified sender domain) in Vercel — no-ops
# gracefully when unset.

def _book_club_recipients(db):
    """Approved users who can reach the club (admin / book_club_access /
    book_club_manager), deduped by email."""
    out, seen = [], set()
    for u in db.collection("users").stream():
        ud = u.to_dict() or {}
        if not ud.get("approved"):
            continue
        if not (ud.get("role") == "admin" or ud.get("book_club_access") or ud.get("book_club_manager")):
            continue
        email = (ud.get("email") or "").strip()
        if not email or email.lower() in seen:
            continue
        seen.add(email.lower())
        out.append({"email": email, "name": ud.get("displayName") or "there"})
    return out


def _meeting_ready(d):
    return bool((d.get("title") or "").strip()
                and (d.get("meeting_date") or "").strip()
                and (d.get("meeting_location") or "").strip())


def _meeting_signature(d):
    return "|".join([(d.get("title") or "").strip(),
                     (d.get("meeting_date") or "").strip(),
                     (d.get("meeting_time") or "").strip(),
                     (d.get("meeting_location") or "").strip()])


def _send_email_resend(api_key, to_email, subject, html):
    from_addr = os.getenv("BOOK_CLUB_FROM_EMAIL",
                          BOOK_CLUB_NAME + " <bookclub@thekahlahouse.com>").strip()
    resp = _http.post(
        "https://api.resend.com/emails",
        headers={"Authorization": "Bearer " + api_key, "Content-Type": "application/json"},
        json={"from": from_addr, "to": [to_email], "subject": subject, "html": html},
        timeout=8,
    )
    return resp.status_code in (200, 201)


def _build_invite_email(d):
    """Returns (subject, html_template). html_template has a {{NAME}}
    placeholder for per-recipient personalization."""
    from urllib.parse import quote_plus

    def esc(s):
        return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    title = (d.get("title") or "Book Club").strip()
    author = (d.get("author") or "").strip()
    loc = (d.get("meeting_location") or "").strip()
    notes = (d.get("notes") or "").strip()
    cover = (d.get("cover_url") or "").strip()
    time_str = (d.get("meeting_time") or "17:00").strip()
    try:
        start = datetime.strptime(d["meeting_date"] + " " + time_str, "%Y-%m-%d %H:%M")
    except Exception:
        start = datetime.strptime(d["meeting_date"], "%Y-%m-%d")
    end = start + timedelta(hours=2)
    pretty_date = start.strftime("%A, %B ") + str(start.day) + start.strftime(", %Y")
    h12 = start.hour % 12 or 12
    pretty_time = "%d:%02d %s" % (h12, start.minute, "AM" if start.hour < 12 else "PM")

    cal_text = "Book Club: " + title
    cal_details = BOOK_CLUB_NAME + " — " + title + (" by " + author if author else "")
    g_dates = start.strftime("%Y%m%dT%H%M%S") + "/" + end.strftime("%Y%m%dT%H%M%S")
    google = ("https://calendar.google.com/calendar/render?action=TEMPLATE&text="
              + quote_plus(cal_text) + "&dates=" + g_dates + "&ctz=America/Phoenix&details="
              + quote_plus(cal_details) + "&location=" + quote_plus(loc))
    outlook = ("https://outlook.live.com/calendar/0/deeplink/compose?path=/calendar/action/compose&rru=addevent&subject="
               + quote_plus(cal_text) + "&startdt=" + start.strftime("%Y-%m-%dT%H:%M:%S") + "-07:00"
               + "&enddt=" + end.strftime("%Y-%m-%dT%H:%M:%S") + "-07:00&location="
               + quote_plus(loc) + "&body=" + quote_plus(cal_details))

    club = esc(BOOK_CLUB_NAME)
    subject = "\U0001F4DA %s: %s — %s at %s" % (BOOK_CLUB_NAME, title, pretty_date, pretty_time)
    # Only embed an http(s) cover — data-URI covers (phone uploads) bloat the
    # email and are blocked by most clients, so skip them in the invite.
    cover_html = ('<img src="%s" alt="" width="84" style="border-radius:6px;float:left;margin:0 16px 8px 0">' % esc(cover)) if cover.startswith(("http://", "https://")) else ""
    author_html = ('<div style="color:#64748b">by %s</div>' % esc(author)) if author else ""
    notes_html = ('<p style="color:#475569;margin:14px 0 0;clear:both">%s</p>' % esc(notes)) if notes else ""
    html = ("""
<div style="font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;max-width:560px;margin:0 auto;color:#0f172a">
  <div style="background:linear-gradient(135deg,#06b6d4,#22c55e);color:#fff;padding:20px 24px;border-radius:12px 12px 0 0">
    <div style="font-size:13px;letter-spacing:.12em;text-transform:uppercase;opacity:.85">%s</div>
    <div style="font-size:22px;font-weight:700;margin-top:4px">You're invited!</div>
  </div>
  <div style="border:1px solid #e2e8f0;border-top:none;border-radius:0 0 12px 12px;padding:24px">
    <p style="margin:0 0 16px">Hi {{NAME}}, here are the details for our next meeting:</p>
    %s
    <div style="font-size:18px;font-weight:700">%s</div>
    %s
    <table style="margin:16px 0;font-size:15px;line-height:1.9;clear:both">
      <tr><td style="color:#64748b;padding-right:14px">\U0001F4C5 Date</td><td style="font-weight:600">%s</td></tr>
      <tr><td style="color:#64748b;padding-right:14px">\U0001F554 Time</td><td style="font-weight:600">%s</td></tr>
      <tr><td style="color:#64748b;padding-right:14px">\U0001F4CD Where</td><td style="font-weight:600">%s</td></tr>
    </table>
    %s
    <div style="margin-top:20px">
      <a href="%s" style="display:inline-block;background:#06b6d4;color:#fff;text-decoration:none;padding:10px 16px;border-radius:8px;font-weight:600;margin:0 8px 8px 0">Add to Google Calendar</a>
      <a href="%s" style="display:inline-block;background:#1f2937;color:#fff;text-decoration:none;padding:10px 16px;border-radius:8px;font-weight:600">Add to Outlook</a>
    </div>
    <p style="color:#94a3b8;font-size:12px;margin-top:24px">See you there! — %s · thekahlahouse.com/book-club</p>
  </div>
</div>""" % (club, cover_html, esc(title), author_html, esc(pretty_date), esc(pretty_time), esc(loc), notes_html, google, outlook, club))
    return subject, html


def _maybe_send_meeting_invite(db, book_id, data):
    """Auto-send a meeting invite when a book has title+date+location AND that
    combination changed since the last send. Never raises — returns a status
    dict the caller surfaces in the API response."""
    if not _meeting_ready(data):
        return {"sent": False, "reason": "incomplete"}
    sig = _meeting_signature(data)
    if data.get("invite_signature") == sig:
        return {"sent": False, "reason": "unchanged"}
    api_key = os.getenv("RESEND_API_KEY", "").strip()
    if not api_key:
        return {"sent": False, "reason": "email_not_configured"}
    try:
        recipients = _book_club_recipients(db)
    except Exception:
        recipients = []
    if not recipients:
        return {"sent": False, "reason": "no_recipients"}
    subject, html_tpl = _build_invite_email(data)
    sent = 0
    for r in recipients:
        name = (r["name"] or "there").replace("<", "").replace(">", "")
        try:
            if _send_email_resend(api_key, r["email"], subject, html_tpl.replace("{{NAME}}", name)):
                sent += 1
        except Exception:
            pass
    if sent > 0:
        try:
            db.collection(_BOOK_CLUB_BOOKS).document(book_id).update({
                "invite_signature": sig,
                "invite_sent_at": firestore.SERVER_TIMESTAMP,
                "invite_sent_count": sent,
            })
        except Exception:
            pass
    return {"sent": sent > 0, "count": sent, "recipients": len(recipients)}


@app.route("/api/book-club")
@book_club_required
def api_book_club_list():
    """All books + all polls in one payload (page fetches/refreshes once)."""
    db = get_db()
    try:
        books = [_serialize_book(d.id, d.to_dict() or {})
                 for d in db.collection(_BOOK_CLUB_BOOKS).stream()]
        polls = [_serialize_poll(d.id, d.to_dict() or {}, g.uid)
                 for d in db.collection(_BOOK_CLUB_POLLS).stream()]
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({
        "ok": True,
        "books": books,
        "polls": polls,
        "me": {
            "uid": g.uid,
            "name": _bc_display_name(),
            "role": g.user_data.get("role"),
            "is_admin": g.user_data.get("role") == "admin",
        },
    })


@app.route("/api/book-club/book", methods=["POST"])
@book_club_required
def api_book_club_add():
    if not _is_club_manager():
        return jsonify({"ok": False, "error": "Only a club manager can add books"}), 403
    body = request.get_json(force=True, silent=True) or {}
    title = (body.get("title") or "").strip()
    if not title:
        return jsonify({"ok": False, "error": "Title is required"}), 400
    status = (body.get("status") or "upcoming").strip()
    if status not in _BOOK_STATUSES:
        status = "upcoming"
    doc = {
        "title": title,
        "author": (body.get("author") or "").strip(),
        "cover_url": (body.get("cover_url") or "").strip(),
        "status": status,
        "meeting_date": (body.get("meeting_date") or "").strip(),
        "meeting_time": (body.get("meeting_time") or "").strip(),
        "meeting_location": (body.get("meeting_location") or "").strip(),
        "notes": (body.get("notes") or "").strip(),
        "added_by": g.uid,
        "added_by_name": _bc_display_name(),
        "created_at": firestore.SERVER_TIMESTAMP,
        "ratings": {},
    }
    db = get_db()
    try:
        ref = db.collection(_BOOK_CLUB_BOOKS).document()
        ref.set(doc)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    invite = _maybe_send_meeting_invite(db, ref.id, doc)
    return jsonify({"ok": True, "id": ref.id, "invite": invite}), 201


@app.route("/api/book-club/book/<book_id>", methods=["PATCH"])
@book_club_required
def api_book_club_update(book_id):
    if not _is_club_manager():
        return jsonify({"ok": False, "error": "Only a club manager can edit books"}), 403
    body = request.get_json(force=True, silent=True) or {}
    allowed = {"title", "author", "cover_url", "status", "meeting_date",
               "meeting_time", "meeting_location", "notes"}
    updates = {}
    for k in allowed:
        if k not in body:
            continue
        v = body[k]
        if isinstance(v, str):
            v = v.strip()
        if k == "status" and v not in _BOOK_STATUSES:
            continue
        updates[k] = v
    if not updates:
        return jsonify({"ok": False, "error": "No valid fields to update"}), 400
    db = get_db()
    try:
        ref = db.collection(_BOOK_CLUB_BOOKS).document(book_id)
        if not ref.get().exists:
            return jsonify({"ok": False, "error": "Book not found"}), 404
        ref.update(updates)
        fresh = ref.get().to_dict() or {}
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    invite = _maybe_send_meeting_invite(db, book_id, fresh)
    return jsonify({"ok": True, "updated": list(updates.keys()), "invite": invite})


@app.route("/api/book-club/book/<book_id>", methods=["DELETE"])
@book_club_required
def api_book_club_delete(book_id):
    db = get_db()
    try:
        if not _is_club_manager():
            return jsonify({"ok": False, "error": "Only a club manager can remove books"}), 403
        ref = db.collection(_BOOK_CLUB_BOOKS).document(book_id)
        if not ref.get().exists:
            return jsonify({"ok": False, "error": "Book not found"}), 404
        ref.delete()
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True})


@app.route("/api/book-club/book/<book_id>/rating", methods=["POST"])
@book_club_required
def api_book_club_rate(book_id):
    """Upsert the caller's own rating + review for a book (one per member)."""
    body = request.get_json(force=True, silent=True) or {}
    try:
        rating = int(body.get("rating"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "rating must be an integer 1-5"}), 400
    if rating < 1 or rating > 5:
        return jsonify({"ok": False, "error": "rating must be between 1 and 5"}), 400
    review = (body.get("review") or "").strip()
    db = get_db()
    try:
        ref = db.collection(_BOOK_CLUB_BOOKS).document(book_id)
        if not ref.get().exists:
            return jsonify({"ok": False, "error": "Book not found"}), 404
        ref.update({
            "ratings." + g.uid: {
                "rating": rating,
                "review": review,
                "name": _bc_display_name(),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True})


@app.route("/api/book-club/poll", methods=["POST"])
@book_club_required
def api_book_club_poll_create():
    """Club managers (or admins): open a next-read vote with 2-3 candidate books."""
    if not _is_club_manager():
        return jsonify({"ok": False, "error": "Only a club manager can start a vote"}), 403
    body = request.get_json(force=True, silent=True) or {}
    raw_options = body.get("options") or []
    options = []
    for i, o in enumerate(raw_options):
        if not isinstance(o, dict):
            continue
        title = (o.get("title") or "").strip()
        if not title:
            continue
        options.append({
            "id": "opt" + str(i),
            "title": title,
            "author": (o.get("author") or "").strip(),
            "cover_url": (o.get("cover_url") or "").strip(),
        })
    if len(options) < 2:
        return jsonify({"ok": False, "error": "A vote needs at least 2 books"}), 400
    if len(options) > 3:
        options = options[:3]
    doc = {
        "question": (body.get("question") or "Next read").strip() or "Next read",
        "status": "open",
        "options": options,
        "votes": {},
        "created_by": g.uid,
        "created_by_name": _bc_display_name(),
        "created_at": firestore.SERVER_TIMESTAMP,
    }
    db = get_db()
    try:
        ref = db.collection(_BOOK_CLUB_POLLS).document()
        ref.set(doc)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True, "id": ref.id}), 201


@app.route("/api/book-club/poll/<poll_id>/vote", methods=["POST"])
@book_club_required
def api_book_club_poll_vote(poll_id):
    """Any member: cast (or change) a single vote on an open poll."""
    body = request.get_json(force=True, silent=True) or {}
    option_id = (body.get("option_id") or "").strip()
    if not option_id:
        return jsonify({"ok": False, "error": "option_id is required"}), 400
    db = get_db()
    try:
        ref = db.collection(_BOOK_CLUB_POLLS).document(poll_id)
        snap = ref.get()
        if not snap.exists:
            return jsonify({"ok": False, "error": "Vote not found"}), 404
        data = snap.to_dict() or {}
        if data.get("status") == "closed":
            return jsonify({"ok": False, "error": "This vote is closed"}), 409
        valid_ids = {o.get("id") for o in (data.get("options") or [])}
        if option_id not in valid_ids:
            return jsonify({"ok": False, "error": "Invalid option"}), 400
        ref.update({"votes." + g.uid: option_id})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True})


@app.route("/api/book-club/poll/<poll_id>/close", methods=["POST"])
@book_club_required
def api_book_club_poll_close(poll_id):
    """Club managers (or admins): close a poll. Optionally add the winning
    book to the shelf (status='upcoming') when body has add_winner=true."""
    if not _is_club_manager():
        return jsonify({"ok": False, "error": "Only a club manager can close a vote"}), 403
    body = request.get_json(force=True, silent=True) or {}
    db = get_db()
    try:
        ref = db.collection(_BOOK_CLUB_POLLS).document(poll_id)
        snap = ref.get()
        if not snap.exists:
            return jsonify({"ok": False, "error": "Vote not found"}), 404
        data = snap.to_dict() or {}
        poll = _serialize_poll(poll_id, data, g.uid)
        ref.update({"status": "closed", "closed_at": firestore.SERVER_TIMESTAMP})
        added_id = None
        if body.get("add_winner") and poll.get("winner"):
            win = next((o for o in poll["options"] if o["id"] == poll["winner"]), None)
            if win:
                book_ref = db.collection(_BOOK_CLUB_BOOKS).document()
                book_ref.set({
                    "title": win["title"],
                    "author": win["author"],
                    "cover_url": win["cover_url"],
                    "status": "upcoming",
                    "meeting_date": "", "meeting_time": "", "meeting_location": "",
                    "notes": "Won the club vote",
                    "added_by": g.uid,
                    "added_by_name": _bc_display_name(),
                    "created_at": firestore.SERVER_TIMESTAMP,
                    "ratings": {},
                })
                added_id = book_ref.id
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True, "winner": poll.get("winner"), "added_book_id": added_id})


@app.route("/api/book-club/poll/<poll_id>", methods=["DELETE"])
@book_club_required
def api_book_club_poll_delete(poll_id):
    """Club managers (or admins): delete a poll."""
    if not _is_club_manager():
        return jsonify({"ok": False, "error": "Only a club manager can delete a vote"}), 403
    db = get_db()
    try:
        ref = db.collection(_BOOK_CLUB_POLLS).document(poll_id)
        if not ref.get().exists:
            return jsonify({"ok": False, "error": "Vote not found"}), 404
        ref.delete()
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True})


@app.route("/api/book-club/availability")
@book_club_required
def api_book_club_availability_get():
    """Everyone's days-off + the club roster, so the page can suggest days
    when all members are free. `busy` is a map keyed by date ('YYYY-MM-DD')
    → {allDay:true}. Availability is day-level only (no time-of-day): a
    member who hasn't marked a date is assumed available that day."""
    db = get_db()
    try:
        members = []
        for d in db.collection(_BOOK_CLUB_AVAIL).stream():
            data = d.to_dict() or {}
            members.append({
                "uid": d.id,
                "name": data.get("name") or "Member",
                "busy": data.get("busy") or {},
                "updated_at": _bc_iso(data.get("updated_at")),
            })
        # Roster = approved users who can reach the club (admin or the pill).
        roster = []
        for u in db.collection("users").stream():
            ud = u.to_dict() or {}
            if not ud.get("approved"):
                continue
            if ud.get("role") == "admin" or ud.get("book_club_access"):
                roster.append({
                    "uid": u.id,
                    "name": ud.get("displayName") or ud.get("email") or "Member",
                })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({
        "ok": True,
        "members": members,
        "roster": roster,
        "me_uid": g.uid,
        "me_name": _bc_display_name(),
    })


@app.route("/api/book-club/availability", methods=["POST"])
@book_club_required
def api_book_club_availability_save():
    """Upsert the caller's own days-off map (replaces it wholesale). Each
    value is coerced to {allDay:true} — availability is day-level only."""
    body = request.get_json(force=True, silent=True) or {}
    busy = body.get("busy")
    if not isinstance(busy, dict):
        return jsonify({"ok": False, "error": "busy must be an object keyed by date"}), 400
    clean = {}
    for k, v in busy.items():
        if _DATE_RE.match(str(k)) and isinstance(v, dict) and (v.get("allDay") or v.get("ranges")):
            clean[k] = {"allDay": True}
    db = get_db()
    try:
        db.collection(_BOOK_CLUB_AVAIL).document(g.uid).set({
            "name": _bc_display_name(),
            "busy": clean,
            "updated_at": firestore.SERVER_TIMESTAMP,
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True, "saved": len(clean)})


# ---------------------------------------------------------------------------
# Grocery list (family shopping app) — schemaless Firestore, Admin SDK.
# Two collections:
#   grocery_items/{id}   — the CURRENT shared shopping list (transient per run)
#   grocery_staples/{id} — the persistent staple catalog that re-seeds each
#                          new list. Removing a staple-seeded item from the
#                          current list does NOT remove the staple; it returns
#                          on the next "Start new list". Un-star to stop it.
# Walmart import: each item/staple can carry a `walmart_id`; the page builds a
# single add-to-cart deep link from them (one tap loads the whole cart). An
# optional, env-gated Walmart.io API resolver fills IDs from names — see
# _walmart_search / /api/grocery/walmart-resolve.
# ---------------------------------------------------------------------------

_GROCERY_ITEMS = "grocery_items"
_GROCERY_STAPLES = "grocery_staples"
# Persistent name → Walmart item-ID memory (one doc, a {name_lower: id} map).
# So once any item gets a walmart_id, typing that name again auto-fills it —
# no need to star it as a staple. Survives list clears.
_GROCERY_META = "grocery_meta"
_KNOWN_IDS_DOC = "known_ids"
# Aisle/category vocabulary the UI groups by. Free-text is tolerated, but the
# client offers these; "Other" is the catch-all.
_GROCERY_CATEGORIES = [
    "Produce", "Meat & Seafood", "Dairy & Eggs", "Bakery", "Frozen",
    "Pantry", "Snacks", "Beverages", "Breakfast", "Deli", "Canned & Jarred",
    "Baking", "Condiments", "Household", "Personal Care", "Baby", "Pet", "Other",
]


def _grocery_name():
    return g.user_data.get("displayName") or g.user_data.get("email") or "Family"


def _g_iso(ts):
    """Firestore timestamp → ISO string (or None). Mirrors _bc_iso."""
    try:
        return ts.isoformat() if ts is not None and hasattr(ts, "isoformat") else None
    except Exception:
        return None


def _g_int(v, default=1):
    try:
        n = int(float(v))
        return n if n > 0 else default
    except (TypeError, ValueError):
        return default


def _g_cat(v):
    c = (v or "").strip()
    return c if c else "Other"


def _serialize_grocery_item(doc_id, d):
    return {
        "id": doc_id,
        "name": (d.get("name") or "").strip(),
        "qty": _g_int(d.get("qty"), 1),
        "category": _g_cat(d.get("category")),
        "note": (d.get("note") or "").strip(),
        "checked": bool(d.get("checked")),
        "staple": bool(d.get("staple")),
        "walmart_id": (d.get("walmart_id") or "").strip(),
        "added_by": d.get("added_by") or "",
        "added_by_name": d.get("added_by_name") or "",
        "checked_by_name": d.get("checked_by_name") or "",
        "created_at": _g_iso(d.get("created_at")),
        "updated_at": _g_iso(d.get("updated_at")),
    }


def _serialize_grocery_staple(doc_id, d):
    return {
        "id": doc_id,
        "name": (d.get("name") or "").strip(),
        "qty": _g_int(d.get("qty"), 1),
        "category": _g_cat(d.get("category")),
        "walmart_id": (d.get("walmart_id") or "").strip(),
        "added_by_name": d.get("added_by_name") or "",
        "created_at": _g_iso(d.get("created_at")),
    }


def _staple_key(name):
    return (name or "").strip().lower()


def _known_ids(db):
    """The persistent {name_lower: walmart_id} memory map (or {})."""
    try:
        snap = db.collection(_GROCERY_META).document(_KNOWN_IDS_DOC).get()
        return (snap.to_dict() or {}) if snap.exists else {}
    except Exception:
        return {}


def _remember_walmart_id(db, name, wid):
    """Persist a name → Walmart item-ID mapping so the same item auto-fills its
    ID on any future add. No-op when either is blank. Firestore map keys can't
    contain '/' or '.', so sanitize the key (still matched the same way client-
    side via _ki_key)."""
    key = _ki_key(name)
    wid = (wid or "").strip()
    if not key or not wid:
        return
    try:
        db.collection(_GROCERY_META).document(_KNOWN_IDS_DOC).set({key: wid}, merge=True)
    except Exception:
        pass


def _ki_key(name):
    # Firestore map keys: lowercase, collapse whitespace, drop '/' '.' '~' '*' '['
    # ']' which are illegal in field paths. Mirrored by _kiKey() in grocery.html.
    k = (name or "").strip().lower()
    for ch in "/.~*[]":
        k = k.replace(ch, " ")
    return " ".join(k.split())


def _upsert_staple(db, name, qty, category, walmart_id=""):
    """Create-or-update a staple by case-insensitive name. Returns the doc id."""
    key = _staple_key(name)
    if not key:
        return None
    existing = None
    for s in db.collection(_GROCERY_STAPLES).stream():
        sd = s.to_dict() or {}
        if _staple_key(sd.get("name")) == key:
            existing = s
            break
    payload = {
        "name": name.strip(),
        "qty": _g_int(qty, 1),
        "category": _g_cat(category),
    }
    if walmart_id:
        payload["walmart_id"] = walmart_id.strip()
    if existing:
        db.collection(_GROCERY_STAPLES).document(existing.id).update(payload)
        return existing.id
    payload["added_by_name"] = _grocery_name()
    payload["created_at"] = firestore.SERVER_TIMESTAMP
    ref = db.collection(_GROCERY_STAPLES).document()
    ref.set(payload)
    return ref.id


def _remove_staple_by_name(db, name):
    key = _staple_key(name)
    for s in db.collection(_GROCERY_STAPLES).stream():
        sd = s.to_dict() or {}
        if _staple_key(sd.get("name")) == key:
            db.collection(_GROCERY_STAPLES).document(s.id).delete()


@app.route("/api/grocery")
@grocery_required
def api_grocery_list():
    """Current list + staple catalog + caller info, in one payload. The page
    polls this every few seconds so the family sees each other's changes."""
    db = get_db()
    try:
        items = [_serialize_grocery_item(d.id, d.to_dict() or {})
                 for d in db.collection(_GROCERY_ITEMS).stream()]
        staples = [_serialize_grocery_staple(d.id, d.to_dict() or {})
                   for d in db.collection(_GROCERY_STAPLES).stream()]
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    items.sort(key=lambda i: (i["checked"], i["category"].lower(), i["name"].lower()))
    staples.sort(key=lambda s: (s["category"].lower(), s["name"].lower()))
    return jsonify({
        "ok": True,
        "items": items,
        "staples": staples,
        "known_ids": _known_ids(db),
        "categories": _GROCERY_CATEGORIES,
        "walmart_configured": bool(_walmart_configured()),
        "me": {"uid": g.uid, "name": _grocery_name(),
               "is_admin": g.user_data.get("role") == "admin"},
    })


@app.route("/api/grocery/item", methods=["POST"])
@grocery_required
def api_grocery_add():
    body = request.get_json(force=True, silent=True) or {}
    name = (body.get("name") or "").strip()
    if not name:
        return jsonify({"ok": False, "error": "Name is required"}), 400
    qty = _g_int(body.get("qty"), 1)
    category = _g_cat(body.get("category"))
    walmart_id = (body.get("walmart_id") or "").strip()
    is_staple = bool(body.get("staple"))
    doc = {
        "name": name,
        "qty": qty,
        "category": category,
        "note": (body.get("note") or "").strip(),
        "checked": False,
        "staple": is_staple,
        "walmart_id": walmart_id,
        "added_by": g.uid,
        "added_by_name": _grocery_name(),
        "checked_by_name": "",
        "created_at": firestore.SERVER_TIMESTAMP,
        "updated_at": firestore.SERVER_TIMESTAMP,
    }
    db = get_db()
    try:
        ref = db.collection(_GROCERY_ITEMS).document()
        ref.set(doc)
        if is_staple:
            _upsert_staple(db, name, qty, category, walmart_id)
        if walmart_id:
            _remember_walmart_id(db, name, walmart_id)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True, "id": ref.id}), 201


@app.route("/api/grocery/item/<item_id>", methods=["PATCH"])
@grocery_required
def api_grocery_update(item_id):
    body = request.get_json(force=True, silent=True) or {}
    db = get_db()
    ref = db.collection(_GROCERY_ITEMS).document(item_id)
    snap = ref.get()
    if not snap.exists:
        return jsonify({"ok": False, "error": "Item not found"}), 404
    cur = snap.to_dict() or {}
    patch = {"updated_at": firestore.SERVER_TIMESTAMP}
    if "name" in body:
        nm = (body.get("name") or "").strip()
        if not nm:
            return jsonify({"ok": False, "error": "Name cannot be empty"}), 400
        patch["name"] = nm
    if "qty" in body:
        patch["qty"] = _g_int(body.get("qty"), 1)
    if "category" in body:
        patch["category"] = _g_cat(body.get("category"))
    if "note" in body:
        patch["note"] = (body.get("note") or "").strip()
    if "walmart_id" in body:
        patch["walmart_id"] = (body.get("walmart_id") or "").strip()
    if "checked" in body:
        patch["checked"] = bool(body.get("checked"))
        patch["checked_by_name"] = _grocery_name() if body.get("checked") else ""
    if "staple" in body:
        patch["staple"] = bool(body.get("staple"))
    try:
        ref.update(patch)
        # Keep the staple catalog in sync when an item's staple flag / details
        # change, so re-seeding the next list reflects the latest.
        name = patch.get("name", cur.get("name"))
        qty = patch.get("qty", cur.get("qty"))
        category = patch.get("category", cur.get("category"))
        wid = patch.get("walmart_id", cur.get("walmart_id"))
        if "staple" in body:
            if body.get("staple"):
                _upsert_staple(db, name, qty, category, wid or "")
            else:
                _remove_staple_by_name(db, name)
        elif cur.get("staple"):
            # Editing a still-staple item updates its catalog entry too.
            _upsert_staple(db, name, qty, category, wid or "")
        # Remember the Walmart id by name so future adds of this item auto-fill
        # it (independent of staple status).
        if "walmart_id" in body and patch.get("walmart_id"):
            _remember_walmart_id(db, name, patch["walmart_id"])
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True})


@app.route("/api/grocery/item/<item_id>", methods=["DELETE"])
@grocery_required
def api_grocery_delete(item_id):
    """Remove an item from the CURRENT list only. A staple-seeded item removed
    here still returns on the next new list (the staple catalog is untouched)."""
    db = get_db()
    try:
        db.collection(_GROCERY_ITEMS).document(item_id).delete()
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True})


@app.route("/api/grocery/clear-checked", methods=["POST"])
@grocery_required
def api_grocery_clear_checked():
    """Remove all checked-off (in-cart) items from the current list."""
    db = get_db()
    n = 0
    try:
        for d in db.collection(_GROCERY_ITEMS).stream():
            if (d.to_dict() or {}).get("checked"):
                db.collection(_GROCERY_ITEMS).document(d.id).delete()
                n += 1
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True, "removed": n})


@app.route("/api/grocery/new-list", methods=["POST"])
@grocery_required
def api_grocery_new_list():
    """Start a fresh list: delete every current item, then seed one unchecked
    item per staple. The clean slate everyone wants for the next shopping run,
    pre-loaded with the recurring staples."""
    db = get_db()
    try:
        for d in db.collection(_GROCERY_ITEMS).stream():
            db.collection(_GROCERY_ITEMS).document(d.id).delete()
        seeded = 0
        for s in db.collection(_GROCERY_STAPLES).stream():
            sd = s.to_dict() or {}
            name = (sd.get("name") or "").strip()
            if not name:
                continue
            db.collection(_GROCERY_ITEMS).document().set({
                "name": name,
                "qty": _g_int(sd.get("qty"), 1),
                "category": _g_cat(sd.get("category")),
                "note": "",
                "checked": False,
                "staple": True,
                "walmart_id": (sd.get("walmart_id") or "").strip(),
                "added_by": g.uid,
                "added_by_name": "Staple",
                "checked_by_name": "",
                "created_at": firestore.SERVER_TIMESTAMP,
                "updated_at": firestore.SERVER_TIMESTAMP,
            })
            seeded += 1
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True, "seeded": seeded})


@app.route("/api/grocery/staple", methods=["POST"])
@grocery_required
def api_grocery_staple_add():
    body = request.get_json(force=True, silent=True) or {}
    name = (body.get("name") or "").strip()
    if not name:
        return jsonify({"ok": False, "error": "Name is required"}), 400
    db = get_db()
    try:
        sid = _upsert_staple(db, name, body.get("qty"), body.get("category"),
                             (body.get("walmart_id") or "").strip())
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True, "id": sid}), 201


@app.route("/api/grocery/staple/<staple_id>", methods=["PATCH"])
@grocery_required
def api_grocery_staple_update(staple_id):
    body = request.get_json(force=True, silent=True) or {}
    db = get_db()
    ref = db.collection(_GROCERY_STAPLES).document(staple_id)
    if not ref.get().exists:
        return jsonify({"ok": False, "error": "Staple not found"}), 404
    patch = {}
    if "name" in body:
        nm = (body.get("name") or "").strip()
        if not nm:
            return jsonify({"ok": False, "error": "Name cannot be empty"}), 400
        patch["name"] = nm
    if "qty" in body:
        patch["qty"] = _g_int(body.get("qty"), 1)
    if "category" in body:
        patch["category"] = _g_cat(body.get("category"))
    if "walmart_id" in body:
        patch["walmart_id"] = (body.get("walmart_id") or "").strip()
    if not patch:
        return jsonify({"ok": True})
    try:
        ref.update(patch)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True})


@app.route("/api/grocery/staple/<staple_id>", methods=["DELETE"])
@grocery_required
def api_grocery_staple_delete(staple_id):
    """Remove a staple from the catalog (it stops recurring on new lists).
    Any matching item already on the current list is left in place."""
    db = get_db()
    try:
        snap = db.collection(_GROCERY_STAPLES).document(staple_id).get()
        name = (snap.to_dict() or {}).get("name") if snap.exists else None
        db.collection(_GROCERY_STAPLES).document(staple_id).delete()
        # Un-flag any matching current item so its star reflects reality.
        if name:
            key = _staple_key(name)
            for d in db.collection(_GROCERY_ITEMS).stream():
                dd = d.to_dict() or {}
                if dd.get("staple") and _staple_key(dd.get("name")) == key:
                    db.collection(_GROCERY_ITEMS).document(d.id).update({"staple": False})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True})


# --- Walmart import (env-gated "magic mode" name → item-ID resolver) ---------
# The add-to-cart deep link itself needs no auth and is built client-side from
# items that carry a `walmart_id`. This endpoint is the optional upgrade: if a
# Walmart.io developer app is configured (WALMART_CONSUMER_ID + private key),
# it resolves item names to Walmart item IDs server-side and persists them so a
# staple becomes one-click forever. No creds → graceful {configured:false}.

def _walmart_configured():
    return bool(os.getenv("WALMART_CONSUMER_ID") and os.getenv("WALMART_PRIVATE_KEY"))


def _walmart_sign(consumer_id, key_version, private_key_pem):
    """Build Walmart.io auth headers (RSA-SHA256 over id\\ntimestamp\\nkeyVer\\n).
    Lazy-imports cryptography; returns None on any failure so callers no-op."""
    try:
        import base64
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
        ts = str(int(datetime.now(timezone.utc).timestamp() * 1000))
        pem = private_key_pem.strip()
        if "BEGIN" not in pem:
            pem = "-----BEGIN PRIVATE KEY-----\n" + pem + "\n-----END PRIVATE KEY-----"
        key = serialization.load_pem_private_key(pem.encode(), password=None)
        data = f"{consumer_id}\n{ts}\n{key_version}\n".encode()
        sig = key.sign(data, padding.PKCS1v15(), hashes.SHA256())
        return {
            "WM_CONSUMER.ID": consumer_id,
            "WM_CONSUMER.INTIMESTAMP": ts,
            "WM_SEC.KEY_VERSION": key_version,
            "WM_SEC.AUTH_SIGNATURE": base64.b64encode(sig).decode(),
        }
    except Exception:
        return None


def _walmart_search(query):
    """Return the best-match Walmart item ID for a search string, or None.
    Uses the affiliate search API; silent-fails (network/creds/shape)."""
    consumer_id = os.getenv("WALMART_CONSUMER_ID")
    private_key = os.getenv("WALMART_PRIVATE_KEY")
    key_version = os.getenv("WALMART_KEY_VERSION", "1")
    if not (consumer_id and private_key and query):
        return None
    headers = _walmart_sign(consumer_id, key_version, private_key)
    if not headers:
        return None
    headers["Accept"] = "application/json"
    url = "https://developer.api.walmart.com/api-proxy/service/affil/product/v2/search"
    try:
        r = _http.get(url, headers=headers,
                      params={"query": query, "numItems": 1}, timeout=8)
        if r.status_code != 200:
            return None
        items = (r.json() or {}).get("items") or []
        if not items:
            return None
        iid = items[0].get("itemId")
        return str(iid) if iid else None
    except Exception:
        return None


@app.route("/api/grocery/walmart-resolve", methods=["POST"])
@grocery_required
def api_grocery_walmart_resolve():
    """Fill missing walmart_id on current (unchecked) items by name. No-op
    when Walmart.io isn't configured. Persists resolved IDs onto items AND
    their staple so they stick."""
    if not _walmart_configured():
        return jsonify({"ok": True, "configured": False, "resolved": 0})
    db = get_db()
    resolved = 0
    try:
        for d in db.collection(_GROCERY_ITEMS).stream():
            it = d.to_dict() or {}
            if it.get("checked") or (it.get("walmart_id") or "").strip():
                continue
            name = (it.get("name") or "").strip()
            if not name:
                continue
            iid = _walmart_search(name)
            if not iid:
                continue
            db.collection(_GROCERY_ITEMS).document(d.id).update({"walmart_id": iid})
            if it.get("staple"):
                _upsert_staple(db, name, it.get("qty"), it.get("category"), iid)
            _remember_walmart_id(db, name, iid)
            resolved += 1
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True, "configured": True, "resolved": resolved})


# ---------------------------------------------------------------------------
# API routes — Odds
# ---------------------------------------------------------------------------

# ESPN scoreboard config — sport → (group, league) + a small in-memory cache.
# Shared by _fetch_espn_scoreboard (per-sport) and _espn_scoreboard_raw
# (group/league passthrough — World Cup, MMA). NOTE: these constants were
# accidentally swept up when the Odds Board's /api/odds route was removed
# (June 2026); restored here — _build_worldcup / live tracker / pm-snapshot-wc
# all depend on them, so losing them 500'd every ESPN-backed endpoint.
_ESPN_PATH = {
    "mlb":   ("baseball",       "mlb"),
    "nba":   ("basketball",     "nba"),
    "nhl":   ("hockey",         "nhl"),
    "nfl":   ("football",       "nfl"),
    "ncaab": ("basketball",     "mens-college-basketball"),
    "ncaaf": ("football",       "college-football"),
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


def _espn_scoreboard_raw(grp: str, league: str, dates: str | None = None) -> list:
    """Fetch ANY ESPN scoreboard by group/league path (e.g. soccer/fifa.world,
    mma/ufc) — generalizes _fetch_espn_scoreboard beyond the board's
    _ESPN_PATH. `dates` is optional 'YYYYMMDD' or 'YYYYMMDD-YYYYMMDD'. 30s
    cache per (path, dates). [] on error. No API key. This is the shared
    reader for ESPN-as-schedule-spine work (World Cup tab, post-cutover
    markets ingest)."""
    import time
    key = f"{grp}/{league}?{dates or ''}"
    now = time.time()
    cached = _ESPN_CACHE.get(key)
    if cached and (now - cached[0]) < _ESPN_TTL:
        return cached[1]
    url = f"https://site.api.espn.com/apis/site/v2/sports/{grp}/{league}/scoreboard"
    try:
        r = _http.get(url, params=({"dates": dates} if dates else None), timeout=8)
        if r.status_code != 200:
            return []
        events = (r.json() or {}).get("events", []) or []
        _ESPN_CACHE[key] = (now, events)
        return events
    except Exception:
        return []


def _espn_soccer_match(ev: dict) -> dict | None:
    """Parse one ESPN soccer scoreboard event → flat match dict:
    {away, home, date, state, detail, completed, away_score, home_score}.
    None on unexpected shape. (state ∈ pre / in / post.)"""
    try:
        comp = (ev.get("competitions") or [{}])[0]
        home = away = None
        hs = as_ = None
        for c in (comp.get("competitors") or []):
            t = c.get("team") or {}
            name = (t.get("displayName") or t.get("name")
                    or t.get("shortDisplayName") or "")
            sc = c.get("score")
            if c.get("homeAway") == "home":
                home, hs = name, sc
            elif c.get("homeAway") == "away":
                away, as_ = name, sc
        st = ((ev.get("status") or {}).get("type") or {})
        return {
            "away": away, "home": home,
            "date": ev.get("date"),
            "state": st.get("state"),                       # pre / in / post
            "detail": st.get("shortDetail") or st.get("description") or "",
            "completed": bool(st.get("completed")),
            "away_score": as_, "home_score": hs,
        }
    except Exception:
        return None


# ESPN ↔ Polymarket World Cup country-name variants (keys are
# pmm_markets._norm output — lowercase, accent-stripped, space-collapsed).
# Both feeds get canonicalized through this so "South Korea"/"Korea
# Republic", "Turkey"/"Turkiye", "Ivory Coast"/"Cote d'Ivoire" etc. match
# on the same key. Tune from /debug-worldcup's `unmatched_espn` list.
_WC_COUNTRY_ALIASES = {
    "south korea": "korea", "korea republic": "korea", "republic of korea": "korea",
    "turkey": "turkiye",
    "ir iran": "iran",
    "ivory coast": "cote d ivoire",
    "cape verde": "cabo verde",
    "czech republic": "czechia",
    "dr congo": "congo dr", "democratic republic of the congo": "congo dr",
    "congo democratic republic": "congo dr",
    "usa": "united states", "us": "united states",
    "bosnia and herzegovina": "bosnia herzegovina",
}


def _wc_country_key(name: str) -> str:
    """Canonical country key for ESPN↔PMM World Cup matching."""
    import pmm_markets as _pm
    n = _pm._norm(name or "")
    return _WC_COUNTRY_ALIASES.get(n, n)


# ───────────────────────── Kalshi (free, no-auth market data) ─────────────
# Kalshi is a deep prediction-market exchange whose price (in cents = implied
# probability) moves with the sharp number — like Polymarket, a free/keyless
# "birdie" that the line moved. We read it ONLY as a signal source (the user
# doesn't bet Kalshi). Market-data GET endpoints are public — no API key.
#
# This is step 1 of the PMM+Kalshi cross-confirm detector: get a working
# Kalshi reader + verify the exact response shape in production (I can't reach
# Kalshi from the build sandbox). /debug-kalshi dumps the raw first-market
# object so we can lock the field names, then build the matcher + logger.
#
# Base URL has moved historically (trading-api -> elections -> external), so
# we try candidates in order and report which one answered.
_KALSHI_BASES = [
    "https://api.elections.kalshi.com/trade-api/v2",
    "https://external-api.kalshi.com/trade-api/v2",
    "https://trading-api.kalshi.com/trade-api/v2",
]
# sport -> Kalshi series_ticker. MLB confirmed (KXMLBGAME); others TBD once
# we can browse Kalshi live (NBA/NHL/NFL series tickers added later).
# sport (UPPER, our markets.sport code) -> Kalshi per-game series ticker.
# Confirmed live via /debug-kalshi. Add a sport here + a team map in
# _TEAM_TO_KALSHI + to _PM_SPORTS / odds_api _TRIGGER_SPORTS to cover it.
_KALSHI_SERIES = {"MLB": "KXMLBGAME", "NBA": "KXNBAGAME", "NHL": "KXNHLGAME",
                  "WORLDCUP": "KXWCGAME",
                  # Football (added July 2026 ahead of the season). NFL is
                  # believed KXNFLGAME / NCAAF KXNCAAFGAME — UNVERIFIED from
                  # the sandbox (proxy blocks Kalshi); confirm the tickers +
                  # side-suffix codes via /debug-kalshi?sport=nfl once live.
                  # A wrong ticker just returns 0 markets (defensive fetch).
                  "NFL": "KXNFLGAME", "NCAAF": "KXNCAAFGAME",
                  # UFC: PROBE-ONLY guess (July 2026) so /debug-kalshi?sport=ufc
                  # can show whether/how Kalshi lists fights. NOT matched in the
                  # snapshot loop (no _TEAM_TO_KALSHI entry — fighters need a
                  # name-matched index like _kalshi_wc_index, built only after
                  # the probe confirms the series + yes_sub_title shape).
                  "UFC": "KXUFCFIGHT"}
# Sports the cent-logger + cross-confirm trigger cover (cent data flows).
# NCAAF and UFC are PMM-ONLY (no _TEAM_TO_KALSHI map — school codes are
# unmappable blind, fighters aren't teams; the pm-snapshot loop skips the
# Kalshi bulk fetch for map-less sports). Their MLs gate on PMM steam alone
# — see handicapper_web.KALSHI_CONFIRM_SPORTS (the unconfirmed-ML demotion
# only applies where a Kalshi confirm venue actually exists).
_PM_SPORTS = ["MLB", "NBA", "NHL", "NFL", "NCAAF", "UFC"]
# How far out the cross-confirm watcher tracks each sport. MLB is dense
# (daily) so 12h is plenty; NBA/NHL playoff series are 2-3 days apart, so
# we watch farther out to catch the early line movement on the next game
# (few games, so the per-game PMM budget cost is negligible).
# NFL is WEEKLY and its sharp money famously moves early in the week
# (look-ahead lines) — watch the full 7 days so we capture the Sun→Wed
# steam, not just game-day. NCAAF gets 72h (Wed→Sat covers most movement;
# the Saturday slate is 60+ games, so a 7-day window would swamp the
# budgeted PMM rotation — widen only with budget headroom data).
# UFC: cards are weekly Saturdays and fight lines move all week off camp
# news/weigh-ins — 96h catches the Wed→Sat drift. NOTE the UFC block-time
# quirk (gotcha in _ufc_pickable_market_rows) doesn't matter here: the
# watcher only needs the nominal event_start to be in the future window.
_PM_WINDOW_H = {"MLB": 12, "NBA": 96, "NHL": 96, "NFL": 168, "NCAAF": 72,
                "UFC": 96}
_KALSHI_CACHE: dict[str, tuple[float, dict]] = {}
_KALSHI_TTL = 30  # seconds


def _kalshi_cents(v):
    """Kalshi quotes prices as dollar STRINGS ('0.5100') — convert to the
    integer cents (51) the 1c-move trigger works in. None on junk; note
    '0.0000' -> 0 means no bid/ask, not a real 0c price (detector handles)."""
    try:
        return round(float(v) * 100)
    except (TypeError, ValueError):
        return None


def _fetch_kalshi_markets(series_ticker: str, status: str = "open") -> dict:
    """Fetch open markets for a Kalshi series (public, no auth). Defensive —
    never raises. Returns {ok, base_used, http_status, count, markets:[...],
    raw_sample, error}. `raw_sample` is the FULL first market object so we can
    confirm the real field names (yes_bid/yes_ask/last_price/ticker/...) from
    production via /debug-kalshi. Failures are NOT cached (so the next hit
    retries fresh while we iterate)."""
    import time
    key = f"{series_ticker}:{status}"
    now = time.time()
    cached = _KALSHI_CACHE.get(key)
    if cached and (now - cached[0]) < _KALSHI_TTL:
        return cached[1]

    out: dict = {"ok": False, "series_ticker": series_ticker,
                 "base_used": None, "http_status": None,
                 "count": 0, "markets": [], "raw_sample": None, "error": None}
    headers = {"Accept": "application/json", "User-Agent": "kahla-house/1.0"}
    params = {"series_ticker": series_ticker, "status": status, "limit": 200}
    last_err = None
    for base in _KALSHI_BASES:
        try:
            r = _http.get(f"{base}/markets", params=params,
                          headers=headers, timeout=10)
            out["base_used"] = base
            out["http_status"] = r.status_code
            if r.status_code != 200:
                last_err = f"HTTP {r.status_code} @ {base}: {r.text[:160]}"
                continue
            data = r.json()
            markets = data.get("markets") if isinstance(data, dict) else None
            if markets is None:
                last_err = (f"no 'markets' key @ {base}; top keys="
                            + str(list(data.keys()) if isinstance(data, dict) else type(data).__name__))
                continue
            out["raw_sample"] = markets[0] if markets else None
            parsed = []
            for m in markets:
                # Verified shape (prod /debug-kalshi): each EVENT
                # (event_ticker) has two market rows, one per team; `team`
                # (= yes_sub_title) is the side this YES contract pays.
                # Prices are dollar strings -> integer cents.
                parsed.append({
                    "ticker":       m.get("ticker"),
                    "event_ticker": m.get("event_ticker"),
                    "title":        m.get("title"),
                    "team":         m.get("yes_sub_title"),
                    "yes_bid_c":    _kalshi_cents(m.get("yes_bid_dollars")),
                    "yes_ask_c":    _kalshi_cents(m.get("yes_ask_dollars")),
                    "last_c":       _kalshi_cents(m.get("last_price_dollars")),
                    "volume":       m.get("volume_fp") or m.get("volume_24h_fp"),
                    "status":       m.get("status"),
                    "close_time":   m.get("close_time"),
                })
            out["ok"] = True
            out["count"] = len(parsed)
            out["markets"] = parsed
            _KALSHI_CACHE[key] = (now, out)
            return out
        except Exception as e:
            last_err = f"{type(e).__name__} @ {base}: {e}"
            continue
    out["error"] = last_err
    return out


@app.route("/debug-kalshi")
def debug_kalshi():
    """Verify the live Kalshi response shape. Public market data only (no
    secrets), so no auth — hit it in a browser: /debug-kalshi?sport=mlb.
    `raw_sample` shows the true field names so we can lock the parser, then
    build the PMM+Kalshi cent-move detector on top. Temporary verify tool."""
    # ?series= probes a raw Kalshi series ticker directly (to discover the
    # per-game series for new sports, e.g. KXNBAGAME); ?sport= uses the map.
    series = (request.args.get("series") or "").strip().upper()
    if not series:
        sport = (request.args.get("sport") or "mlb").upper()
        series = _KALSHI_SERIES.get(sport)
        if not series:
            return jsonify({"ok": False, "error": f"unknown sport '{sport}'",
                            "known": list(_KALSHI_SERIES)}), 400
    return jsonify(_fetch_kalshi_markets(series))


def _probe_vsin_url(url: str) -> dict:
    """Fetch one VSiN URL server-side (Vercel egress reaches it where the
    build sandbox + a phone can't) and characterize the response so we can
    find where Circa lines + DK splits actually live. No secrets, read-only."""
    import re as _re
    out = {"url": url}
    try:
        r = _http.get(url, headers={
            "User-Agent": _SPLITS_UA,
            "Accept": "text/html,application/json,application/xhtml+xml,*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.vsin.com/",
        }, timeout=14)
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
        return out
    body = r.text or ""
    low = body.lower()
    out["status"] = r.status_code
    out["content_type"] = r.headers.get("content-type")
    out["length"] = len(body)
    out["server"] = r.headers.get("server")
    # Cloudflare / bot-challenge tells
    out["looks_blocked"] = any(s in low for s in (
        "just a moment", "cf-challenge", "cf-browser-verification",
        "attention required", "access denied", "captcha"))
    # Is the data embedded (SSR / JSON blob) or loaded by a separate call?
    out["has_next_data"] = '__next_data__' in low or 'id="__next_data__"' in low
    out["keyword_hits"] = {k: low.count(k) for k in
        ("circa", "draftkings", "handle", "ticket", "percent", "%", "splits", "consensus")
        if k in low}
    # Any internal API / data URLs the page references (the real "endpoint")
    urls = set(_re.findall(r'https?://[^\s"\'<>]+', body))
    apiish = sorted(u for u in urls if any(t in u.lower()
                    for t in ("api", "/json", "data.vsin", "/splits", "graphql", ".json")))[:25]
    out["api_like_urls"] = apiish
    # Pull the splits TABLE structure so we can write the parser: find tables,
    # pick the one that looks like the splits grid (most rows / has % cells),
    # dump its header + first rows. CSS paywall clips visually but the rows are
    # in the DOM, so a server parse sees them.
    try:
        from bs4 import BeautifulSoup as _BS
        soup = _BS(body, "lxml")
        tables = soup.find_all("table")
        out["table_count"] = len(tables)
        best, best_score = None, -1
        for t in tables:
            rows = t.find_all("tr")
            txt = t.get_text(" ", strip=True)
            score = len(rows) + txt.count("%") * 2
            if score > best_score:
                best, best_score = t, score
        if best is not None:
            rows = best.find_all("tr")
            def _cells(tr):
                return [c.get_text(" ", strip=True)[:40]
                        for c in tr.find_all(["th", "td"])]
            out["table"] = {
                "n_rows": len(rows),
                "header": _cells(rows[0]) if rows else [],
                "sample_rows": [_cells(tr) for tr in rows[1:6]],
            }
    except Exception as e:
        out["table_error"] = f"{type(e).__name__}: {e}"
    # If a JSON response, show top keys; if HTML, a small head snippet
    ct = (out["content_type"] or "").lower()
    if "json" in ct:
        try:
            j = r.json()
            out["json_top_keys"] = (sorted(j.keys())[:30] if isinstance(j, dict)
                                    else f"list[{len(j)}]")
        except Exception:
            out["json_parse"] = "failed"
    out["head_snippet"] = body[:600]
    return out


@app.route("/debug-vsin")
def debug_vsin():
    """Probe VSiN to find where Circa lines + DK splits live (HTML blob vs a
    separate data call) and whether the Vercel runtime can even reach it.
    Public, read-only, no secrets. ?url= probes one arbitrary URL; default
    walks a candidate list. Temporary discovery tool."""
    one = (request.args.get("url") or "").strip()
    if one:
        if not one.startswith("http"):
            return jsonify({"ok": False, "error": "url must be absolute http(s)"}), 400
        return jsonify({"ok": True, "results": [_probe_vsin_url(one)]})
    candidates = [
        "https://data.vsin.com/draftkings/betting-splits/?view=mlb",
        "https://data.vsin.com/circa/betting-splits/?view=mlb",
        "https://data.vsin.com/betting-splits/?view=mlb",
    ]
    return jsonify({"ok": True, "results": [_probe_vsin_url(u) for u in candidates]})


def _vsin_pct(s: str):
    """Pull the integer percent out of a VSiN cell like '83% ▲' / '24% ▼'."""
    import re as _re
    m = _re.search(r"(\d+)\s*%", s or "")
    return int(m.group(1)) if m else None


def _vsin_num(s: str):
    """Clean a VSiN line cell ('-1.5', '+104', '8.5') → stripped string."""
    import re as _re
    m = _re.search(r"[+-]?\d+(?:\.\d+)?", (s or "").replace("−", "-"))
    return m.group(0) if m else None


# VSiN sport → ?view= code (extend as we add sports). MLB confirmed live.
_VSIN_VIEW = {"mlb": "mlb", "nba": "nba", "nhl": "nhl",
              "nfl": "nfl", "ncaaf": "cfb", "ncaab": "cbb"}


def _fetch_vsin_splits(sport: str = "mlb", book: str = "draftkings") -> dict:
    """Scrape VSiN betting splits (server-rendered HTML, no auth) for one
    book. Returns handle% (money) AND bets% (tickets) per side per market for
    SPR/TOT/ML. book ∈ {draftkings, circa}. The SSR table carries the full
    slate even behind VSiN's CSS paywall (it only clips visually)."""
    from bs4 import BeautifulSoup as _BS
    import time as _time
    view = _VSIN_VIEW.get(sport.lower(), sport.lower())
    cache_key = f"vsin:{book}:{view}"
    _now = _time.time()
    _c = _cache.get(cache_key)
    if _c and (_now - _c["ts"]) < 900:        # 15-min cache (slow-moving %s)
        return _c["data"]
    url = f"https://data.vsin.com/{book}/betting-splits/?view={view}"
    debug = {"url": url, "ok": False}
    try:
        r = _http.get(url, headers={
            "User-Agent": _SPLITS_UA,
            "Accept": "text/html,*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.vsin.com/",
        }, timeout=14)
        debug["status"] = r.status_code
        if r.status_code != 200:
            debug["error"] = f"HTTP {r.status_code}"
            return {"events": [], "debug": debug}
        soup = _BS(r.text, "lxml")
    except Exception as e:
        debug["error"] = f"{type(e).__name__}: {e}"
        return {"events": [], "debug": debug}

    # The splits grid = the table with the most rows + % cells.
    best, best_score = None, -1
    for t in soup.find_all("table"):
        rows = t.find_all("tr")
        score = len(rows) + t.get_text(" ", strip=True).count("%") * 2
        if score > best_score:
            best, best_score = t, score
    if best is None:
        debug["error"] = "no table"
        return {"events": [], "debug": debug}

    # Collect valid team rows: [team, spr, spr_h, spr_b, tot, tot_h, tot_b, ml, ml_h, ml_b]
    team_rows = []
    for tr in best.find_all("tr"):
        c = [td.get_text(" ", strip=True) for td in tr.find_all(["td", "th"])]
        if len(c) < 11:
            continue
        team = c[1].strip()
        # skip header / non-team rows (need a team name + a ML handle %)
        if not team or _vsin_pct(c[9]) is None or "handle" in team.lower():
            continue
        team_rows.append({
            "team": team,
            "spr_line": _vsin_num(c[2]), "spr_h": _vsin_pct(c[3]), "spr_b": _vsin_pct(c[4]),
            "tot_line": _vsin_num(c[5]), "tot_h": _vsin_pct(c[6]), "tot_b": _vsin_pct(c[7]),
            "ml_line": _vsin_num(c[8]),  "ml_h": _vsin_pct(c[9]),  "ml_b": _vsin_pct(c[10]),
        })

    # Pair consecutive rows into games (away on top, home below).
    events = []
    for i in range(0, len(team_rows) - 1, 2):
        a, h = team_rows[i], team_rows[i + 1]
        events.append({
            "book": book,
            "away_team": a["team"], "home_team": h["team"],
            "ml": {"away_line": a["ml_line"], "home_line": h["ml_line"],
                   "away_handle": a["ml_h"], "away_bets": a["ml_b"],
                   "home_handle": h["ml_h"], "home_bets": h["ml_b"]},
            "spread": {"away_line": a["spr_line"], "home_line": h["spr_line"],
                       "away_handle": a["spr_h"], "away_bets": a["spr_b"],
                       "home_handle": h["spr_h"], "home_bets": h["spr_b"]},
            "total": {"line": a["tot_line"],
                      "over_handle": a["tot_h"], "over_bets": a["tot_b"],
                      "under_handle": h["tot_h"], "under_bets": h["tot_b"]},
        })
    debug["ok"] = True
    debug["n_events"] = len(events)
    result = {"events": events, "debug": debug}
    if events:                                 # cache successes only (like splits)
        _cache[cache_key] = {"data": result, "ts": _now}
    return result


@app.route("/debug-vsin-parsed")
def debug_vsin_parsed():
    """View the PARSED VSiN splits (clean games) for a sport+book so we can
    verify the parser on a phone. Public, read-only. ?sport=mlb&book=circa."""
    sport = (request.args.get("sport") or "mlb").lower()
    book = (request.args.get("book") or "draftkings").lower()
    return jsonify(_fetch_vsin_splits(sport, book))


@app.route("/debug-pmm")
def debug_pmm():
    """Diagnose the Polymarket SDK path — does the authed client construct, and
    does a minimal MARKET-DATA read (public-ish) work? Returns ONLY ok/timing/
    exception text (no secrets). Public, read-only. This isolates whether the
    outage is auth/client-construction vs the read endpoint vs a timeout."""
    import time as _t
    out = {"keys_present": bool(POLYMARKET_KEY_ID and POLYMARKET_SECRET_KEY)}
    # 1) Client construction (this is where account-auth would fail).
    t0 = _t.time()
    try:
        client = get_client()
        out["get_client"] = {"ok": True, "ms": round((_t.time() - t0) * 1000)}
    except Exception as e:
        out["get_client"] = {"ok": False, "ms": round((_t.time() - t0) * 1000),
                             "error": f"{type(e).__name__}: {str(e)[:300]}"}
        return jsonify(out)
    # 2) Discovery: test the ACTUAL slugs (tagSlug='mlb' etc.) with the REAL
    # query shape the lookup uses (tag + time window + relatedTags), plus a
    # no-tag sample so we can see what events/tags Polymarket actually exposes
    # right now (did the slug/taxonomy change?).
    now = datetime.now(timezone.utc)
    tmin = (now - timedelta(hours=12)).isoformat()
    tmax = (now + timedelta(hours=24)).isoformat()

    def _count(params):
        t1 = _t.time()
        try:
            r = client.events.list(params)
            evs = (r if isinstance(r, list)
                   else (r or {}).get("events") or (r or {}).get("results") or [])
            return {"count": len(evs), "ms": round((_t.time() - t1) * 1000)}, evs
        except Exception as e:
            return {"error": f"{type(e).__name__}: {str(e)[:180]}",
                    "ms": round((_t.time() - t1) * 1000)}, []

    # Real shape (what lookup actually sends), a few sports to see if it's
    # MLB-only or all sports.
    out["real_shape"] = {}
    for tg in ("mlb", "nba", "nfl", "soccer"):
        res, _ = _count({"tagSlug": tg, "closed": False, "relatedTags": True,
                         "startTimeMin": tmin, "startTimeMax": tmax, "limit": 50})
        out["real_shape"][tg] = res
    # Bare tag (no time window) — candidate MLB slugs.
    out["bare_tag"] = {}
    for tg in ("mlb", "baseball-mlb", "baseball", "sports"):
        res, _ = _count({"tagSlug": tg, "closed": False, "limit": 10})
        out["bare_tag"][tg] = res
    # No-tag sample — what's actually live + how it's tagged.
    res, evs = _count({"closed": False, "limit": 25})
    sample = []
    for e in evs[:10]:
        if isinstance(e, dict):
            sample.append({"title": e.get("title") or e.get("question") or e.get("slug"),
                           "tags": e.get("tags") or e.get("tagSlugs") or e.get("tag")})
    out["no_tag"] = {"result": res, "sample": sample}

    # 3) FULL lookup — exactly what pm-snapshot calls (import pmm_markets +
    # lookup + team-match + markets.list) on a real upcoming MLB game. This is
    # the path that produces the PMM snapshot rows; if it fails/empties here,
    # that's the silent gap. Import is inside the try so an import break shows.
    try:
        sb = get_supabase()
        grow = (sb.table("markets").select("event_name,event_start")
                .eq("sport", "MLB").eq("status", "active")
                .gte("event_start", now.isoformat())
                .order("event_start").limit(1).execute().data or [None])[0]
        if not grow:
            out["lookup"] = {"error": "no upcoming MLB game in markets"}
        else:
            away, home = [s.strip() for s in grow["event_name"].split(" @ ", 1)]
            import pmm_markets as _pm
            diag = {}
            t2 = _t.time()
            res2 = _pm.lookup(client, "MLB", away, home, grow["event_start"], diag=diag)
            out["lookup"] = {
                "game": grow["event_name"], "ms": round((_t.time() - t2) * 1000),
                "res_keys": sorted((res2 or {}).keys()),
                "has_ml": bool((res2 or {}).get("ml")),
                "has_spread": bool((res2 or {}).get("spread")),
                "has_total": bool((res2 or {}).get("total")),
                "diag": diag,
            }
    except Exception as e:
        import traceback
        out["lookup"] = {"error": f"{type(e).__name__}: {str(e)[:300]}",
                         "tb": traceback.format_exc()[-600:]}
    return jsonify(out)


def _fetch_kalshi_orderbook(ticker: str) -> dict:
    """Full Kalshi order book (all bid levels, both sides) for one market.
    Public, no auth. `GET /markets/{ticker}/orderbook`."""
    headers = {"Accept": "application/json", "User-Agent": "kahla-house/1.0"}
    last = None
    for base in _KALSHI_BASES:
        try:
            r = _http.get(f"{base}/markets/{ticker}/orderbook",
                          headers=headers, timeout=10)
            if r.status_code != 200:
                last = f"HTTP {r.status_code} @ {base}: {r.text[:140]}"
                continue
            return {"base_used": base, "data": r.json()}
        except Exception as e:
            last = f"{type(e).__name__} @ {base}: {e}"
    return {"error": last}


# ───────────── Order-book depth readers + make/take signal ─────────────
# Both venues expose a full ladder. Normalize each to a common per-side shape
# {best_bid, best_ask, bids:[(cent,size)], asks:[(cent,size)]} for the side
# you'd BUY, so the make/take math is venue-agnostic. The decision reads the
# TOP ROW ONLY (best bid size vs best ask size) — a 2-level/depth sum gets
# fooled by a deep wall one tick off the touch (it took the Cardinals when
# the touch was 4x ask-heavy). These books are massive (RULE 0.001), so the
# touch ratio is the whole signal — no depth window, no size floor.

# THE TAKE RULE (research-calibrated June 2026): TAKE when the TOP ROW (the
# touch) is ≥ 1.5× more contracts BID than OFFERED — more buyers than
# sellers at the price = pressure UP = a resting maker joins the back of a
# queue sellers aren't coming to clear (queue-reactive fill intensities +
# back-of-queue adverse selection — Huang/Lehalle/Rosenbaum 2015, Moallemi
# & Yuan 2016, Lehalle & Mounjid 2017). The practitioner cluster is 1.5-2.0×.
# Read off the TOP ROW only — a 2-level/depth sum gets fooled by a wall one
# tick off the touch (it took the Cardinals when the touch was 4× ask-heavy).
# These books are massive (RULE 0.001) — no size floor, no spoof gate; the
# touch ratio is the whole signal.
_TAKE_IMB = 1.5          # top-row bid size ≥ this × top-row ask size ⇒ TAKE


def _pmm_book(client, slug: str) -> dict | None:
    """Polymarket markets.book(slug) -> normalized side book in cents.
    bids = buyers of this side, asks (offers) = sellers of this side."""
    try:
        md = (client.markets.book(slug) or {}).get("marketData") or {}
    except Exception:
        return None

    def lv(rows):
        out = []
        for r in (rows or []):
            try:
                # Polymarket now quotes HALF-CENT ticks — round to the nearest
                # 0.5c (not 1c) so a 47.5c bid/ask is preserved, not collapsed
                # onto 47/48. The half-cent is load-bearing for the maker
                # queue-jump (MAKE+) in _cross_book_signal.
                c = round(float((r.get("px") or {}).get("value")) * 200) / 2.0
                q = float(r.get("qty"))
                if 0 < c < 100 and q > 0:
                    out.append((c, q))
            except Exception:
                pass
        return out
    bids = sorted(lv(md.get("bids")),   key=lambda x: -x[0])
    asks = sorted(lv(md.get("offers")), key=lambda x:  x[0])
    return {"bids": bids, "asks": asks,
            "best_bid": bids[0][0] if bids else None,
            "best_ask": asks[0][0] if asks else None}


def _kalshi_book(ticker: str) -> dict | None:
    """Kalshi orderbook for one side -> normalized book in cents. yes_dollars
    = bids for this side; no_dollars = the other side's bids, which become
    THIS side's asks at (100 - no_price)."""
    ob = _fetch_kalshi_orderbook(ticker)
    data = (ob.get("data") or {}).get("orderbook_fp") or {}
    if not data:
        return None

    def cents(rows):
        out = []
        for pair in (rows or []):
            try:
                c = round(float(pair[0]) * 100)
                q = float(pair[1])
                if 0 < c < 100 and q > 0:
                    out.append((c, q))
            except Exception:
                pass
        return out
    bids = sorted(cents(data.get("yes_dollars")), key=lambda x: -x[0])
    asks = sorted([(100 - c, q) for c, q in cents(data.get("no_dollars"))],
                  key=lambda x: x[0])
    return {"bids": bids, "asks": asks,
            "best_bid": bids[0][0] if bids else None,
            "best_ask": asks[0][0] if asks else None}


def _invert_book(book: dict | None) -> dict | None:
    """NO-side view of a binary YES book. A NO buyer is a YES seller, so the
    NO bids are the YES asks at (100−c) and vice versa. Needed because PMM
    has ONE market per line (the YES side); picks on the synthesized inverse
    side (pmm `synthetic: true`) share the YES market's slug, and reading the
    YES book unflipped computes make/take + imbalance for the WRONG side."""
    if not book:
        return None
    bids = sorted([(100 - c, q) for c, q in (book.get("asks") or [])],
                  key=lambda x: -x[0])
    asks = sorted([(100 - c, q) for c, q in (book.get("bids") or [])],
                  key=lambda x: x[0])
    return {"bids": bids, "asks": asks,
            "best_bid": bids[0][0] if bids else None,
            "best_ask": asks[0][0] if asks else None}


def _pmm_taker_fee_cents(price_cents) -> float:
    """Polymarket US TAKER fee per share, in cents: 5c · p·(1-p) (p = price
    in dollars) — peaks at 1.25c/share at 50c, tapers to the wings. Confirmed
    on live tickets (54c -> $1.24/100sh, 47c -> $1.25/100sh) AND against the
    published schedule (0.05·C·p·(1-p), effective Apr 2026). The fee PEAKS at
    coin-flips, exactly where sharp action lives, so taking a 50/50 is the
    most expensive case."""
    if price_cents is None:
        return 0.0
    p = max(0.0, min(1.0, price_cents / 100.0))
    return round(5.0 * p * (1.0 - p), 2)


def _pmm_maker_rebate_cents(price_cents) -> float:
    """Polymarket US MAKER rebate per share, in cents: +1.25c · p·(1-p),
    credited at fill (max +0.31c at 50c). Makers don't just pay zero — they
    get PAID (verified June 2026 vs docs.polymarket.us/fees: 0.0125·C·p·(1-p)).
    Counted as a forgone-rebate term in the make-vs-take cost gap."""
    if price_cents is None:
        return 0.0
    p = max(0.0, min(1.0, price_cents / 100.0))
    return round(1.25 * p * (1.0 - p), 2)


def _book_signal(book: dict | None, edge_units: float = 1,
                 starts_in_min: float | None = None) -> dict | None:
    """Make-vs-take for BUYING this side, FEE- AND TIME-aware. A maker fills
    only if SELLERS hit your bid (zero fee) and it needs TIME to happen; a
    taker crosses to the ask + pays the fee (costs `spread + fee`, ~2c+ near
    50/50) but is instant. So the call is "will my maker fill before I need
    the bet?" — driven by the book (will sellers hit it?) AND the clock (is
    there time?). DEFAULT MAKE; TAKE when the maker will likely MISS — the
    TOP ROW is ≥ _TAKE_IMB bid-heavy (the research threshold) OR the clock is
    running out. Reported take price is FEE-INCLUSIVE, not the ask."""
    if not book or book.get("best_bid") is None or book.get("best_ask") is None:
        return None
    bb, ba = book["best_bid"], book["best_ask"]
    spread = ba - bb
    fee = _pmm_taker_fee_cents(ba)
    rebate = _pmm_maker_rebate_cents(bb)   # what a filled maker would EARN
    take_eff = round(ba + fee, 2)          # TRUE cost of taking (ask + fee)
    # take vs make all-in gap: spread + taker fee + the maker rebate you
    # give up by not resting (~2.6c at 50/50 on a 1c book).
    take_cost = round(spread + fee + rebate, 2)
    # THE TOP ROW is the truth — best bid size vs best ask size. NOT a
    # 2-level sum: a deep bid wall ONE tick below the touch fooled the old
    # 2-level read into TAKING the Cardinals when the touch was 4x ASK-heavy
    # (= MAKE). On Polymarket's deep sports books there are no thin markets
    # to model with depth windows; the touch is all that matters.
    l1_bid = round(book["bids"][0][1]) if book["bids"] else 0
    l1_ask = round(book["asks"][0][1]) if book["asks"] else 0
    touch_imb = round(l1_bid / l1_ask, 2) if l1_ask else None
    sim = starts_in_min

    take, why = False, []
    # THE RULE (research-calibrated): TAKE when the TOP ROW is ≥ _TAKE_IMB
    # (1.5x) more contracts BID than OFFERED — buyers stacked over sellers at
    # the price → pressing up, a resting maker won't fill. Otherwise MAKE
    # (rest at the bid; an ask-heavy or balanced touch means sellers come to
    # you). No size floor — these books are massive (RULE 0.001).
    if (touch_imb is not None and touch_imb >= _TAKE_IMB):
        take = True
        why.append(f"top row {bb}c: {l1_bid:,} bid vs {l1_ask:,} offered ({touch_imb}x — {int(round((touch_imb - 1) * 100))}% more buyers than sellers) — a maker won't fill; take it")
    # Clock fallback — even on a balanced book, a maker needs TIME to fill;
    # inside 30m a resting limit may not fill before the game (unless the
    # touch is ask-heavy, where sellers hit your bid fast anyway).
    elif sim is not None and sim <= 30:
        if touch_imb is not None and touch_imb <= round(1.0 / _TAKE_IMB, 2):
            why.append(f"{round(sim)}m to tip but ask-heavy at the touch ({touch_imb}x) — sellers fill your maker fast, still make")
        else:
            take = True
            why.append(f"{round(sim)}m to tip — a maker won't fill in time, take it in")
    else:
        why.append(f"rest the maker — taking costs {take_cost}c ({spread}c spread + {fee}c fee + {rebate}c forgone maker rebate)")
    return {"rec": "TAKE" if take else "MAKE",
            "make_price": bb, "take_price": ba, "take_fee": fee,
            "make_rebate": rebate,
            "take_eff": take_eff, "take_cost": take_cost,
            "target": take_eff if take else bb,    # fee-inclusive take / rest at bid
            "best_bid": bb, "best_ask": ba, "spread": spread,
            "imbalance": touch_imb,                # top-row ratio (the decision)
            "top_bid_size": l1_bid, "top_ask_size": l1_ask,
            "touch_imbalance": touch_imb, "queue_ahead": l1_bid, "ask_touch": l1_ask,
            "why": why}


# ── Kalshi US fee schedule (per contract, in cents) ──────────────────
# Verified June 2026 against live order tickets: taker peaks 1.75c at 50/50
# (0.07·p·(1−p)); maker is 1/4 of that (empirically 0.0176·p·(1−p), ~0.44c
# at 50/50). Kalshi round-up-to-cent applies to the order TOTAL; for the
# per-contract make-vs-take comparison the rate is what matters. Unlike
# Polymarket (which PAYS makers a rebate), Kalshi CHARGES makers.
def _kalshi_taker_fee_cents(price_cents) -> float:
    if price_cents is None:
        return 0.0
    p = max(0.0, min(1.0, price_cents / 100.0))
    return round(7.0 * p * (1.0 - p), 3)


def _kalshi_maker_fee_cents(price_cents) -> float:
    if price_cents is None:
        return 0.0
    p = max(0.0, min(1.0, price_cents / 100.0))
    return round(1.76 * p * (1.0 - p), 3)


def _kalshi_side_book(sport: str, away: str, home: str, side: str) -> dict | None:
    """Live Kalshi top-of-book for the picked side of one game, or None.
    ML only (the Kalshi reader is ML-only). Same {bids,asks,best_bid,
    best_ask} shape as _pmm_book — bids = buyers of THIS side. Matches the
    game by team codes in the event ticker + the side by the ticker suffix."""
    series = _KALSHI_SERIES.get(sport)
    if not series:
        return None
    picked = home if side == "home" else away
    pc = _our_team_to_kalshi_code(sport, picked)
    ac = _our_team_to_kalshi_code(sport, away)
    hc = _our_team_to_kalshi_code(sport, home)
    if not (pc and ac and hc):
        return None
    data = _fetch_kalshi_markets(series)
    for m in (data.get("markets") or []):
        tk = m.get("ticker") or ""
        et = m.get("event_ticker") or ""
        if "-" not in tk or tk.rsplit("-", 1)[1] != pc:
            continue
        if ac not in et or hc not in et:
            continue
        return _kalshi_book(tk)
    return None


# MLB run-total + run-line series — confirmed live via /debug-kalshi-discover
# (June 2026). TOTAL (KXMLBTOTAL): YES = "Over (N-0.5) runs", ticker suffix
# N = line + 0.5 (8.5 → 9); UNDER is the inverse side. SPREAD (KXMLBSPREAD)
# is an alt-ladder: YES = "{team} wins by over (N-0.5) runs", suffix
# {teamcode}{N} with N = margin + 0.5 — so the standard −1.5 run-line =
# "wins by over 1.5" = suffix {fav}2; the +1.5 dog is the inverse of the
# favorite's market. Same {YY}{MON}{DD}{HHMM}{AWAY}{HOME} event encoding +
# team codes as KXMLBGAME.
_KALSHI_LINE_SERIES = {"MLB": {"total": "KXMLBTOTAL", "spread": "KXMLBSPREAD"}}


def _kalshi_line_book(sport: str, away: str, home: str, market_type: str,
                      side: str, line) -> dict | None:
    """Kalshi top-of-book for a TOTAL or SPREAD pick, or None. Same shape as
    _pmm_book (bids = buyers of THIS side). Maps our (market_type, side, line)
    onto Kalshi's YES contract (Over for totals; '{fav} wins by over X.5' for
    spreads) and INVERTS the book for the synthesized side (under / run-line
    dog). Currently MLB-only — the line series are MLB; add others to
    _KALSHI_LINE_SERIES + confirm their suffix encoding before enabling."""
    mt = ("total" if market_type in ("total", "tot")
          else "spread" if market_type in ("spread", "spr") else None)
    if mt is None or line is None:
        return None
    series = (_KALSHI_LINE_SERIES.get(sport) or {}).get(mt)
    if not series:
        return None
    try:
        L = float(line)
    except (TypeError, ValueError):
        return None
    ac = _our_team_to_kalshi_code(sport, away)
    hc = _our_team_to_kalshi_code(sport, home)
    if not (ac and hc):
        return None
    mkts = (_fetch_kalshi_markets(series).get("markets") or [])

    if mt == "total":
        want = str(int(round(abs(L) + 0.5)))          # 8.5 → "9"
        for m in mkts:
            tk = m.get("ticker") or ""
            et = m.get("event_ticker") or ""
            if "-" not in tk or tk.rsplit("-", 1)[1] != want:
                continue
            if ac not in et or hc not in et:
                continue
            book = _kalshi_book(tk)
            return book if side == "over" else _invert_book(book)   # YES = OVER
        return None

    # spread: the YES market is always the FAVORITE "wins by over (mag-0.5)".
    n = int(round(abs(L) + 0.5))                       # 1.5 → 2
    fav_is_picked = (L < 0)                            # negative line = laying runs
    if fav_is_picked:
        fav_code = _our_team_to_kalshi_code(sport, home if side == "home" else away)
    else:
        fav_code = ac if side == "home" else hc        # the OTHER team is the favorite
    if not fav_code:
        return None
    want = f"{fav_code}{n}"
    for m in mkts:
        tk = m.get("ticker") or ""
        et = m.get("event_ticker") or ""
        if "-" not in tk or tk.rsplit("-", 1)[1] != want:
            continue
        if ac not in et or hc not in et:
            continue
        book = _kalshi_book(tk)
        return book if fav_is_picked else _invert_book(book)
    return None


def _cross_book_signal(pmm_book: dict | None, kalshi_book: dict | None,
                       units: float = 1, starts_in_min: float | None = None) -> dict | None:
    """Best execution across {MAKE,TAKE} × {Polymarket, Kalshi} for BUYING
    one side. Both venues' contracts pay $1 on the SAME outcome, so all-in
    CENTS are directly comparable — pick the cheapest fillable option. MAKE
    rests at the venue's bid (Polymarket EARNS a rebate, Kalshi pays a maker
    fee); it's only fillable when the venue's TOP ROW isn't bid-heavy (else a
    resting maker won't fill — sellers aren't coming) and the clock allows.
    TAKE crosses at the ask + taker fee; always fills. Returns the chosen
    option + all four for transparency, or None if neither book is usable."""
    opts: list[dict] = []
    inv_take = round(1.0 / _TAKE_IMB, 2)

    def add(venue: str, book: dict | None, taker_fee, maker_adj,
            half_cent: bool = False):
        if not book or book.get("best_bid") is None or book.get("best_ask") is None:
            return
        bb, ba = book["best_bid"], book["best_ask"]
        l1b = round(book["bids"][0][1]) if book.get("bids") else 0
        l1a = round(book["asks"][0][1]) if book.get("asks") else 0
        timb = round(l1b / l1a, 2) if l1a else None
        bid_heavy = (timb is not None and timb >= _TAKE_IMB)
        ask_heavy = (timb is not None and timb <= inv_take)
        clock_ok = (starts_in_min is None or starts_in_min > 30)
        base = {"bid": bb, "ask": ba, "touch_imb": timb,
                "queue_ahead": l1b, "ask_touch": l1a}
        # PLAIN MAKE — rest at the bid. A CONFIDENT fill only when there are
        # more sellers than buyers at the touch (ask-heavy): sellers come to
        # your bid. On a balanced/bid-heavy book a resting bid sits behind the
        # queue and may never fill — that's what MAKE+ is for. Cost basis =
        # bid ± maker adj (PMM EARNS a rebate, Kalshi PAYS a maker fee).
        opts.append({**base, "venue": venue, "rec": "MAKE",
                     "all_in": round(bb + maker_adj(bb), 2),
                     "fillable": ask_heavy, "post_price": bb})
        # MAKE+ (Polymarket half-cent queue jump). Rest at bid+0.5c → step in
        # FRONT of the whole bid queue for half a cent → fill on the next
        # sell, still as a maker (earns the rebate). Fillable whenever the
        # touch isn't a blowout (bid_heavy → price gapping up past your
        # half-cent, must take) and the clock allows; needs ≥1c spread room so
        # bid+0.5 stays below the ask (else it's a cross = take).
        if half_cent and (not bid_heavy) and clock_ok and (bb + 0.5) < ba:
            hp = bb + 0.5
            opts.append({**base, "venue": venue, "rec": "MAKE+",
                         "all_in": round(hp + maker_adj(hp), 2),
                         "fillable": True, "post_price": hp})
        # TAKE — cross at ask + taker fee. Always fills.
        opts.append({**base, "venue": venue, "rec": "TAKE",
                     "all_in": round(ba + taker_fee(ba), 2),
                     "fillable": True})

    add("POLYMARKET", pmm_book, _pmm_taker_fee_cents,
        lambda c: -_pmm_maker_rebate_cents(c), half_cent=True)
    add("KALSHI", kalshi_book, _kalshi_taker_fee_cents,
        lambda c: _kalshi_maker_fee_cents(c))
    if not opts:
        return None
    pool = [o for o in opts if o["fillable"]] or opts
    best = min(pool, key=lambda o: o["all_in"])
    vlabel = {"POLYMARKET": "Polymarket", "KALSHI": "Kalshi"}
    runner = min((o for o in pool if o is not best), key=lambda o: o["all_in"], default=None)
    why = (f"{best['rec'].lower()} {vlabel[best['venue']]} — {best['all_in']}c all-in"
           + (f" vs {runner['all_in']}c next-best ({runner['rec'].lower()} "
              f"{vlabel[runner['venue']]})" if runner else "")
           + (f"; {vlabel[best['venue']]} touch {best['touch_imb']}x" if best['touch_imb'] is not None else ""))
    # `price` is the ACTIONABLE directive shown on the chip: where to rest a
    # maker (bid / bid+0.5) or the fee-inclusive cross for a take. `entry_cents`
    # is the EFFECTIVE cost (all-in, fee/rebate baked in) the pick logs so CLV
    # and to-WIN reflect the real edge — the two differ for a maker by the
    # rebate.
    directive = best.get("post_price")
    if directive is None:
        directive = best["all_in"]
    return {
        "rec": best["rec"], "venue": best["venue"], "venue_label": vlabel[best["venue"]],
        "price": directive, "entry_cents": best["all_in"],
        "best_bid": best["bid"], "best_ask": best["ask"],
        "touch_imbalance": best["touch_imb"],
        "queue_ahead": best["queue_ahead"], "ask_touch": best["ask_touch"],
        "options": opts, "why": [why],
    }


# Best-execution routing to sportsbooks was REMOVED June 2026 — the user is
# Polymarket-exclusive and doesn't bet the US books, so TAKE always means
# "cross on Polymarket" (the fee-inclusive ask). MAKE = rest at the bid. No
# venue routing. (Full _best_book_for / _ROUTE_BOOKS routing is in git history
# if it's ever wanted back.)


@app.route("/debug-orderbook")
def debug_orderbook():
    """Probe the FULL depth ladder on both venues (read-only, public market
    data). Polymarket: introspects the live SDK (`dir`) to reveal whether a
    depth method exists beyond top-of-book `bbo`, and tries candidate names.
    Kalshi: fetches the orderbook directly. Pass ?slug=<pmm-market-slug> and
    ?ticker=<kalshi-market-ticker> to probe specific markets; Kalshi auto-
    picks a live MLB ticker if none given. Temporary discovery tool."""
    out: dict = {"polymarket": {}, "kalshi": {}}

    # ---- Polymarket: introspect the SDK + try to pull depth ----
    try:
        client = get_client()
        out["polymarket"]["markets_methods"] = sorted(
            m for m in dir(client.markets) if not m.startswith("_"))
        out["polymarket"]["client_resources"] = sorted(
            m for m in dir(client) if not m.startswith("_"))
        slug = (request.args.get("slug") or "").strip()
        if not slug:
            # Auto-find a live PMM market slug from the soonest MLB game so the
            # book-method call below actually runs without needing a ?slug=.
            try:
                import pmm_markets as _pm
                _sb = get_supabase()
                g = ((_sb.table("markets").select("event_name,event_start")
                      .eq("sport", "MLB").eq("status", "active")
                      .gte("event_start", datetime.now(timezone.utc).isoformat())
                      .order("event_start").limit(1).execute().data) or []) if _sb else []
                if g and " @ " in (g[0].get("event_name") or ""):
                    aw, hm = [s.strip() for s in g[0]["event_name"].split(" @ ", 1)]
                    data = _pm.lookup(client, "MLB", aw, hm, g[0]["event_start"])
                    ml = (data or {}).get("ml") or []
                    slug = (ml[0].get("slug") if ml else "") or ""
                    out["polymarket"]["auto_slug"] = slug
            except Exception as e:
                out["polymarket"]["auto_slug_error"] = str(e)[:200]
        if slug:
            try:
                out["polymarket"]["bbo"] = client.markets.bbo(slug)
            except Exception as e:
                out["polymarket"]["bbo_error"] = str(e)[:200]
            # Dump the raw market so we can spot token-id fields (clobTokenIds /
            # marketSides) for a direct CLOB /book fallback if the SDK has no
            # depth method.
            try:
                m = client.markets.retrieve_by_slug(slug)
                out["polymarket"]["market_keys"] = sorted(
                    (m.keys() if isinstance(m, dict) else
                     [a for a in dir(m) if not a.startswith("_")]))
            except Exception as e:
                out["polymarket"]["market_keys_error"] = str(e)[:200]
            # Normalized book + make/take signal (the real output).
            pb = _pmm_book(client, slug)
            out["polymarket"]["book_top5"] = None if not pb else {
                "best_bid": pb["best_bid"], "best_ask": pb["best_ask"],
                "bids": pb["bids"][:5], "asks": pb["asks"][:5]}
            out["polymarket"]["make_take"] = _book_signal(pb)
    except Exception as e:
        out["polymarket"]["error"] = f"{type(e).__name__}: {e}"[:200]

    # ---- Kalshi: full orderbook (auto-pick a live MLB ticker if none) ----
    ticker = (request.args.get("ticker") or "").strip()
    if not ticker:
        kal = _fetch_kalshi_markets("KXMLBGAME")
        mk = kal.get("markets") or []
        ticker = mk[0]["ticker"] if mk else None
    out["kalshi"]["ticker"] = ticker
    if ticker:
        kb = _kalshi_book(ticker)
        out["kalshi"]["book_top5"] = None if not kb else {
            "best_bid": kb["best_bid"], "best_ask": kb["best_ask"],
            "bids": kb["bids"][:5], "asks": kb["asks"][:5]}
        out["kalshi"]["make_take"] = _book_signal(kb)
    return jsonify(out)


@app.route("/api/handicapper/make-take")
@bot_required
def api_make_take():
    """Live best execution for one picked side across BOTH US venues:
    {MAKE,TAKE} × {Polymarket, Kalshi}. The card polls this so the chip
    updates as the books move. Params: slug (PMM market), units,
    starts_in_min, inverse (synthetic NO side), and the game context
    (sport, away, home, side, market_type) used to find the Kalshi market.
    Kalshi is consulted for moneyline only (the reader is ML-only); for
    spread/total or an unmatched game it's Polymarket-only. Returns the
    cheapest fillable option (venue + MAKE/TAKE + all-in cents)."""
    slug = (request.args.get("slug") or "").strip()
    if not slug:
        return jsonify({"ok": False, "error": "missing slug"}), 400
    try:
        units = float(request.args.get("units") or 1)
    except ValueError:
        units = 1
    try:
        sim = float(request.args.get("starts_in_min"))
    except (TypeError, ValueError):
        sim = None
    inverse = (request.args.get("inverse") or "") in ("1", "true", "yes")
    sport = (request.args.get("sport") or "").strip().upper()
    away = (request.args.get("away") or "").strip()
    home = (request.args.get("home") or "").strip()
    side = (request.args.get("side") or "").strip()
    market_type = (request.args.get("market_type") or "").strip()
    try:
        line = float(request.args.get("line"))
    except (TypeError, ValueError):
        line = None
    try:
        book = _pmm_book(get_client(), slug)
    except Exception as e:
        return jsonify({"ok": True, "available": False,
                        "error": f"{type(e).__name__}: {e}"[:160]})
    if inverse:
        book = _invert_book(book)
    # Kalshi cross-shopped on ALL main markets now (ML via the game series,
    # TOTAL/SPREAD via the run-total/run-line series) on covered sports.
    kbook = None
    if sport and away and home:
        try:
            if market_type in ("moneyline", "ml") and side in ("home", "away"):
                kbook = _kalshi_side_book(sport, away, home, side)
            elif market_type in ("total", "tot", "spread", "spr") and line is not None:
                kbook = _kalshi_line_book(sport, away, home, market_type, side, line)
        except Exception:
            kbook = None
    sig = _cross_book_signal(book, kbook, units=units, starts_in_min=sim)
    if not sig:
        return jsonify({"ok": True, "available": False})
    return jsonify({"ok": True, "available": True,
                    "kalshi": bool(kbook), **sig})


@app.route("/debug-crossbook")
def debug_crossbook():
    """PUBLIC verify of the cross-venue make/take (Polymarket + Kalshi) for
    the soonest upcoming game of a sport. Public market data only (no secrets).
    ?sport=mlb&side=home&market_type=total — shows both side books + the cross
    verdict so the cross-venue logic can be checked from Vercel (sandbox can't
    reach Kalshi/PMM). market_type ml|total|spread (default ml); for total/
    spread it uses the soonest game's PMM line for that side."""
    sport = (request.args.get("sport") or "MLB").upper()
    side = (request.args.get("side") or "home").strip()
    mt_raw = (request.args.get("market_type") or "ml").strip().lower()
    mt = ("total" if mt_raw in ("total", "tot")
          else "spread" if mt_raw in ("spread", "spr") else "ml")
    out: dict = {"sport": sport, "side": side, "market_type": mt}
    try:
        client = get_client()
        sb = get_supabase()
        g = ((sb.table("markets").select("event_name,event_start")
              .eq("sport", sport).eq("status", "active")
              .gte("event_start", datetime.now(timezone.utc).isoformat())
              .order("event_start").limit(1).execute().data) or []) if sb else []
        if not g or " @ " not in (g[0].get("event_name") or ""):
            return jsonify({**out, "error": "no upcoming game"})
        aw, hm = [s.strip() for s in g[0]["event_name"].split(" @ ", 1)]
        out["game"] = {"away": aw, "home": hm, "start": g[0]["event_start"]}
        import pmm_markets as _pm
        data = _pm.lookup(client, sport, aw, hm, g[0]["event_start"])
        pmm_key = {"ml": "ml", "total": "total", "spread": "spread"}[mt]
        rows = (data or {}).get(pmm_key) or []
        entry = next((e for e in rows if e.get("side") == side), None)
        out["pmm_line"] = entry.get("line") if entry else None
        pbook = _pmm_book(client, entry["slug"]) if entry and entry.get("slug") else None
        if entry and entry.get("synthetic") and pbook:
            pbook = _invert_book(pbook)
        if mt == "ml":
            kbook = _kalshi_side_book(sport, aw, hm, side)
        else:
            kbook = _kalshi_line_book(sport, aw, hm, mt, side,
                                      entry.get("line") if entry else None)
        out["pmm_book"] = None if not pbook else {
            "best_bid": pbook["best_bid"], "best_ask": pbook["best_ask"],
            "bids": pbook["bids"][:3], "asks": pbook["asks"][:3]}
        out["kalshi_book"] = None if not kbook else {
            "best_bid": kbook["best_bid"], "best_ask": kbook["best_ask"],
            "bids": kbook["bids"][:3], "asks": kbook["asks"][:3]}
        out["cross"] = _cross_book_signal(pbook, kbook)
    except Exception as e:
        import traceback
        out["error"] = str(e)[:200]
        out["trace"] = traceback.format_exc()[:1500]
    return jsonify(out)


# ───────────── PMM + Kalshi cent-logger (the cross-confirm memory) ─────────
# Step 2 of the detector: every ~1 min, record the free Polymarket + Kalshi
# cent prices per side for our upcoming games into pm_snapshots (deduped on
# change). This is both the detector's MEMORY (compare to last to catch a
# >=1c move) and the VALIDATION dataset (prove the 1c-AND fires + measure how
# many minutes these free feeds lead our paid PINN capture in book_snapshots).
# Zero PINN cost. Read-only signal — we never trade Kalshi.

# Our team full name -> Kalshi ticker code (the suffix after the last '-',
# e.g. KXMLBGAME-...BOSNYY-BOS). Keyed by mascot (unique) so it works on any
# city/name variant The Odds API sends.
# Per-sport mascot -> Kalshi ticker code (the suffix after the last '-').
# Keyed by mascot (unique within a sport) so it works on any city/name
# variant The Odds API sends. Confirmed via /debug-kalshi: MLB (all), NBA
# (SAS/NYK), NHL (VGK/CAR); the rest are the standard sports tricodes —
# a wrong code only fails THAT game's match (no trigger), which is safe.
_TEAM_TO_KALSHI = {
    "MLB": {
        "diamondbacks": "AZ", "braves": "ATL", "orioles": "BAL", "red sox": "BOS",
        "white sox": "CWS", "cubs": "CHC", "reds": "CIN", "guardians": "CLE",
        "rockies": "COL", "tigers": "DET", "astros": "HOU", "royals": "KC",
        "angels": "LAA", "dodgers": "LAD", "marlins": "MIA", "brewers": "MIL",
        "twins": "MIN", "mets": "NYM", "yankees": "NYY", "athletics": "ATH",
        "phillies": "PHI", "pirates": "PIT", "padres": "SD", "giants": "SF",
        "mariners": "SEA", "cardinals": "STL", "rays": "TB", "rangers": "TEX",
        "blue jays": "TOR", "nationals": "WSH",
    },
    "NBA": {
        "hawks": "ATL", "celtics": "BOS", "nets": "BKN", "hornets": "CHA",
        "bulls": "CHI", "cavaliers": "CLE", "mavericks": "DAL", "nuggets": "DEN",
        "pistons": "DET", "warriors": "GSW", "rockets": "HOU", "pacers": "IND",
        "clippers": "LAC", "lakers": "LAL", "grizzlies": "MEM", "heat": "MIA",
        "bucks": "MIL", "timberwolves": "MIN", "pelicans": "NOP", "knicks": "NYK",
        "thunder": "OKC", "magic": "ORL", "76ers": "PHI", "suns": "PHX",
        "trail blazers": "POR", "blazers": "POR", "kings": "SAC", "spurs": "SAS",
        "raptors": "TOR", "jazz": "UTA", "wizards": "WAS",
    },
    "NHL": {
        "ducks": "ANA", "bruins": "BOS", "sabres": "BUF", "flames": "CGY",
        "hurricanes": "CAR", "blackhawks": "CHI", "avalanche": "COL",
        "blue jackets": "CBJ", "stars": "DAL", "red wings": "DET", "oilers": "EDM",
        "panthers": "FLA", "kings": "LAK", "wild": "MIN", "canadiens": "MTL",
        "predators": "NSH", "devils": "NJD", "islanders": "NYI", "rangers": "NYR",
        "senators": "OTT", "flyers": "PHI", "penguins": "PIT", "sharks": "SJS",
        "kraken": "SEA", "blues": "STL", "lightning": "TBL", "maple leafs": "TOR",
        "canucks": "VAN", "golden knights": "VGK", "capitals": "WSH", "jets": "WPG",
    },
    # NFL — standard tricodes, best-effort until verified against live
    # KXNFLGAME tickers via /debug-kalshi?sport=nfl (a wrong code just
    # fails that game's match, per the NBA/NHL precedent). Most likely to
    # differ: WAS (vs WSH), JAX (vs JAC), LV (vs LVR).
    "NFL": {
        "cardinals": "ARI", "falcons": "ATL", "ravens": "BAL", "bills": "BUF",
        "panthers": "CAR", "bears": "CHI", "bengals": "CIN", "browns": "CLE",
        "cowboys": "DAL", "broncos": "DEN", "lions": "DET", "packers": "GB",
        "texans": "HOU", "colts": "IND", "jaguars": "JAX", "chiefs": "KC",
        "chargers": "LAC", "rams": "LAR", "raiders": "LV", "dolphins": "MIA",
        "vikings": "MIN", "patriots": "NE", "saints": "NO", "giants": "NYG",
        "jets": "NYJ", "eagles": "PHI", "steelers": "PIT", "seahawks": "SEA",
        "49ers": "SF", "buccaneers": "TB", "titans": "TEN", "commanders": "WAS",
    },
    # NCAAF intentionally absent — see _PM_SPORTS note (PMM-only sport).
}
_KALSHI_MONTHS = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
                  "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}


def _our_team_to_kalshi_code(sport: str, name: str):
    n = (name or "").lower()
    for mascot, code in _TEAM_TO_KALSHI.get(sport, {}).items():
        if mascot in n:
            return code
    return None


def _mid_cents(bid, ask, last):
    """Mid of bid/ask in int cents; one side if only one quoted; else last."""
    vals = [v for v in (bid, ask) if v is not None]
    if len(vals) == 2:
        return round((vals[0] + vals[1]) / 2)
    if len(vals) == 1:
        return vals[0]
    return last


def _kalshi_ml_index(markets: list) -> list:
    """Group Kalshi market rows by event_ticker:
    [{'date':'YYYY-MM-DD','codes':{CODE: cents|None}}]. CODE = ticker side
    suffix; date parsed from the event ticker (the ET game date)."""
    import re
    events: dict = {}
    for m in markets:
        tk = m.get("ticker") or ""
        et = m.get("event_ticker") or ""
        if "-" not in tk or not et:
            continue
        code = tk.rsplit("-", 1)[1]
        mo = re.match(r"KX[A-Z]+GAME-(\d{2})([A-Z]{3})(\d{2})", et)
        if not mo:
            continue
        yy, mon, dd = mo.groups()
        mm = _KALSHI_MONTHS.get(mon)
        if not mm:
            continue
        date = f"20{yy}-{mm:02d}-{int(dd):02d}"
        cents = _mid_cents(m.get("yes_bid_c"), m.get("yes_ask_c"), m.get("last_c"))
        e = events.setdefault(et, {"date": date, "codes": {}})
        if code not in e["codes"] or (e["codes"][code] is None and cents is not None):
            e["codes"][code] = cents
    return list(events.values())


def _match_kalshi(events: list, away_code: str, home_code: str, our_date):
    """Kalshi cents for our game: nearest-date event containing both codes.
    Returns {'away': cents|None, 'home': cents|None} or {}."""
    from datetime import date as _date
    want = {away_code, home_code}
    best, best_diff = None, 99
    for e in events:
        if not want.issubset(e["codes"].keys()):
            continue
        try:
            diff = abs((_date.fromisoformat(e["date"]) - our_date).days)
        except Exception:
            continue
        if diff <= 1 and diff < best_diff:
            best, best_diff = e, diff
    if not best:
        return {}
    return {"away": best["codes"].get(away_code),
            "home": best["codes"].get(home_code)}


def _kalshi_wc_index(markets: list) -> list:
    """Group Kalshi World Cup (KXWCGAME) markets by event_ticker into
    [{'date','outcomes':{country_key|'draw': cents}}]. It's a 3-way market:
    each event has THREE binary YES contracts — one per country + a tie.
    Draw is the ticker '-TIE' suffix; team outcomes are keyed by canonical
    country key (from yes_sub_title via _wc_country_key) so they match our
    ESPN/PMM fixtures regardless of the name variant Kalshi prints."""
    import re
    events: dict = {}
    for m in markets:
        tk = (m.get("ticker") or "").upper()
        et = m.get("event_ticker") or ""
        if not et:
            continue
        date = None
        mo = re.match(r"KXWCGAME-(\d{2})([A-Z]{3})(\d{2})", et)
        if mo:
            yy, mon, dd = mo.groups()
            mm = _KALSHI_MONTHS.get(mon)
            if mm:
                date = f"20{yy}-{mm:02d}-{int(dd):02d}"
        cents = _mid_cents(m.get("yes_bid_c"), m.get("yes_ask_c"), m.get("last_c"))
        if tk.endswith("-TIE"):
            outcome = "draw"
        else:
            team = m.get("team") or ""
            if not team:
                continue
            outcome = _wc_country_key(team)
        e = events.setdefault(et, {"date": date, "outcomes": {}})
        if outcome not in e["outcomes"] or (
                e["outcomes"][outcome] is None and cents is not None):
            e["outcomes"][outcome] = cents
    return list(events.values())


def _match_kalshi_wc(events: list, away_key: str, home_key: str) -> dict:
    """Kalshi cents for our WC fixture: the event whose two TEAM outcomes
    match away/home country keys. Returns {'home','draw','away'} cents
    (any may be None) or {} if no event carries both countries."""
    want = {away_key, home_key}
    for e in events:
        teams = {k for k in e["outcomes"] if k != "draw"}
        if want.issubset(teams):
            return {"home": e["outcomes"].get(home_key),
                    "draw": e["outcomes"].get("draw"),
                    "away": e["outcomes"].get(away_key)}
    return {}


def _pmm_game_quotes(pm, client, away, home, event_start, sport="MLB") -> dict:
    """Polymarket quotes for one game, all main markets, mids in int cents.
    Returns {"ml": {side: cents}, "rows": [(market_type, side, line, cents)]}
    or {} on any miss. ONE lookup() call — it already fetches + classifies
    every market, so spread/total ride along at zero extra network cost.
    The `ml` dict feeds the cross-confirm trigger (unchanged); `rows` is
    the full per-line feed for pm_snapshots (the Odds-API-retirement
    history: sharp score / board movement / CLV all read this)."""
    if not pm or not client:
        return {}
    try:
        data = pm.lookup(client, sport, away, home, event_start)
    except Exception:
        return {}
    if not data:
        return {}
    ml, rows = {}, []
    for mt in ("ml", "spread", "total"):
        for row in (data.get(mt) or []):
            side = row.get("side")
            mid = (row.get("quote") or {}).get("mid")
            if not side or mid is None:
                continue
            try:
                cents = round(float(mid) * 100)
            except (TypeError, ValueError):
                continue
            if cents <= 0 or cents >= 100:      # no quote / degenerate
                continue
            line = None
            if mt != "ml" and row.get("line") is not None:
                try:
                    line = float(row["line"])
                except (TypeError, ValueError):
                    continue                     # spr/tot without a line is junk
            rows.append((mt, side, line, cents))
            if mt == "ml" and side in ("home", "away"):
                ml[side] = cents
    return {"ml": ml, "rows": rows}


def _pm_insert_changed(sb, rows, now) -> int:
    """Insert (market_id, source, market_type, side, line, cents) rows,
    deduped: only when the cent differs from the latest stored value for
    that key (book_snapshots pattern). One row per key per tick. `line` is
    part of the key — PMM offers several spread/total lines per game and
    each is its own price series."""
    if not rows:
        return 0

    def _lkey(v):
        try:
            return None if v is None else round(float(v), 2)
        except (TypeError, ValueError):
            return None

    mids = list({r[0] for r in rows})
    last: dict = {}
    try:
        recent = (sb.table("pm_snapshots")
                  .select("market_id,source,market_type,side,line,cents,captured_at")
                  .in_("market_id", mids)
                  .gte("captured_at", (now - timedelta(hours=24)).isoformat())
                  .order("captured_at", desc=True).limit(5000).execute().data) or []
        for r in recent:
            k = (r["market_id"], r["source"], r.get("market_type") or "ml",
                 r["side"], _lkey(r.get("line")))
            if k not in last:           # first seen = latest (desc order)
                last[k] = r["cents"]
    except Exception:
        pass
    ins, seen = [], set()
    for (mid, source, mt, side, line, cents) in rows:
        k = (mid, source, mt, side, _lkey(line))
        if k in seen:
            continue
        seen.add(k)
        if last.get(k) == cents:        # unchanged → skip
            continue
        ins.append({"market_id": mid, "source": source, "market_type": mt,
                    "side": side, "line": line, "cents": cents,
                    "captured_at": now.isoformat()})
    if ins:
        try:
            sb.table("pm_snapshots").insert(ins).execute()
        except Exception:
            return 0
    return len(ins)


# ───────────── Cross-confirm trigger (PMM+Kalshi → on-demand PINN pull) ─────
_XCONFIRM_MOVE = 1   # cents — both feeds must move >= this, same direction


def _in_blackout_mt(now) -> bool:
    """11pm–7am America/Phoenix = the odds-ingest blackout (user asleep)."""
    h = now.astimezone(ZoneInfo("America/Phoenix")).hour
    return h >= 23 or h < 7


def _xconfirm_detect(sb, cur: dict, now) -> dict:
    """`cur`: {market_id: {sport, pmm_home, kalshi_home, starts_in_min}}.
    For each game, compare current HOME cents to the stored baseline; when
    BOTH PMM and Kalshi have drifted >= _XCONFIRM_MOVE the SAME direction
    (and the game is >60min out and we're not in blackout), write a per-sport
    `odds_pull_requests` row + reset that game's baseline. Kalshi orientation
    is trusted — a same-city PMM side-flip just makes the signs disagree, so
    the AND doesn't fire (safe, no false trigger). The per-sport 5-min cap +
    blackout are enforced cron-side when the request is consumed."""
    out = {"triggered": [], "baselined": 0}
    if not cur or _in_blackout_mt(now):
        return out
    mids = list(cur.keys())
    base = {}
    try:
        rows = (sb.table("xconfirm_state").select("market_id,pmm_base,kalshi_base")
                .in_("market_id", mids).execute().data) or []
        for r in rows:
            base[r["market_id"]] = (r.get("pmm_base"), r.get("kalshi_base"))
    except Exception:
        pass
    upserts, trig = [], set()
    for mid, c in cur.items():
        ph, kh = c["pmm_home"], c["kalshi_home"]
        b = base.get(mid)
        if not b or b[0] is None or b[1] is None:           # first sight → baseline
            upserts.append({"market_id": mid, "sport": c["sport"],
                            "pmm_base": ph, "kalshi_base": kh,
                            "baseline_at": now.isoformat(), "last_trigger_at": None})
            continue
        if (c.get("starts_in_min") or 0) <= 60:             # >60min rule
            continue
        pd, kd = ph - b[0], kh - b[1]
        if abs(pd) >= _XCONFIRM_MOVE and abs(kd) >= _XCONFIRM_MOVE and (pd > 0) == (kd > 0):
            trig.add(c["sport"])
            upserts.append({"market_id": mid, "sport": c["sport"],
                            "pmm_base": ph, "kalshi_base": kh,
                            "baseline_at": now.isoformat(),
                            "last_trigger_at": now.isoformat()})
    out["baselined"] = len(upserts)
    if upserts:
        try:
            sb.table("xconfirm_state").upsert(upserts).execute()
        except Exception:
            pass
    for sp in trig:
        try:
            sb.table("odds_pull_requests").upsert({
                "sport": sp, "requested_at": now.isoformat(),
                "consumed_at": None, "reason": "xconfirm"}).execute()
        except Exception:
            pass
    out["triggered"] = sorted(trig)
    return out


@app.route("/api/pm-snapshot")
def api_pm_snapshot():
    """Cron-pinged ~1/min. Logs free PMM + Kalshi cents per side for upcoming
    games in every _PM_SPORTS sport into pm_snapshots (deduped). Auth: ?key=
    matched to PM_SNAPSHOT_SECRET or (fallback) FILLS_CRON_SECRET."""
    import time as _time
    expected = (os.environ.get("PM_SNAPSHOT_SECRET")
                or os.environ.get("FILLS_CRON_SECRET") or "").strip()
    provided = (request.args.get("key") or "").strip()
    if not expected or not secrets.compare_digest(provided, expected):
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    sb = get_supabase()
    if sb is None:
        return jsonify({"ok": False, "error": "supabase unavailable"}), 503

    now = datetime.now(timezone.utc)

    # Gather upcoming games across every cent-logged sport, tagged with sport.
    # Per-sport watch window: MLB 12h, NBA/NHL out to 96h (catch playoff-gap
    # early movement on the next Finals/Cup game).
    all_games = []
    for sp in _PM_SPORTS:
        hi = (now + timedelta(hours=_PM_WINDOW_H.get(sp, 12))).isoformat()
        try:
            rows_sp = (sb.table("markets").select("id,event_name,event_start")
                       .eq("sport", sp).eq("status", "active")
                       .gte("event_start", now.isoformat()).lte("event_start", hi)
                       .order("event_start").execute().data) or []
        except Exception:
            rows_sp = []
        seen = set()
        for g in rows_sp:
            n = g.get("event_name") or ""
            if " @ " in n and n not in seen:        # dedup dup market rows
                seen.add(n)
                g["_sport"] = sp
                all_games.append(g)
    if not all_games:
        return jsonify({"ok": True, "games": 0,
                        "reason": "no upcoming games in any watch window"})

    # Stale-first ordering across all sports — rotate the budgeted PMM
    # coverage so far-out (trigger-relevant) games aren't starved.
    try:
        bl_rows = (sb.table("xconfirm_state").select("market_id,baseline_at")
                   .in_("market_id", [g["id"] for g in all_games]).execute().data) or []
        bl = {r["market_id"]: r.get("baseline_at") for r in bl_rows}
        all_games.sort(key=lambda g: bl.get(g["id"]) or "")
    except Exception:
        pass
    # Near-game priority tier (July 2026, football prep): games starting
    # within 6h jump the rotation queue (stable sort keeps stale-first
    # order inside each tier). The football watch windows put a big far-out
    # slate in view (16 NFL games all week, a 60-game NCAAF Saturday from
    # Wednesday) — pure stale-first would let those rotate in-prime MLB/NFL
    # games out to a many-minute refresh, and the prime-window engine
    # (sharp score recency weights, one-tap chips) depends on fresh cents
    # near tip. Far-out games only need enough cadence to catch slow steam.
    near_cut = now + timedelta(hours=6)

    def _near_tier(g):
        try:
            dt = datetime.fromisoformat(str(g["event_start"]).replace("Z", "+00:00"))
            return 0 if dt <= near_cut else 1
        except Exception:
            return 0
    all_games.sort(key=_near_tier)

    # Kalshi: one bulk call per sport (cheap), indexed up front. Only for
    # sports that actually have a game in the watch window — fetching the
    # full KXNBAGAME/KXNHLGAME books on an empty slate is wasted CPU.
    # Sports with no _TEAM_TO_KALSHI map (NCAAF) skip the fetch entirely —
    # no codes to match the tickers with, so the index would be dead weight.
    active_sports = {g["_sport"] for g in all_games}
    kal_idx, kal_meta = {}, {}
    for sp in active_sports:
        if not _TEAM_TO_KALSHI.get(sp):
            kal_meta[sp] = {"ok": None, "count": 0, "skipped": "no team map"}
            kal_idx[sp] = []
            continue
        kal = _fetch_kalshi_markets(_KALSHI_SERIES[sp])
        kal_meta[sp] = {"ok": kal.get("ok"), "count": kal.get("count")}
        kal_idx[sp] = _kalshi_ml_index(kal.get("markets") or []) if kal.get("ok") else []

    try:
        import pmm_markets as _pm
        _pmm_client = get_client()
    except Exception:
        _pm, _pmm_client = None, None
    pmm_deadline = _time.time() + 7.5

    rows = []
    st = {"pmm_games": 0, "kalshi_games": 0, "pmm_skipped": 0}
    cur = {}   # market_id -> current home cents (both feeds) for the trigger
    for g in all_games:
        sp = g["_sport"]
        away, home = [s.strip() for s in g["event_name"].split(" @ ", 1)]
        mid = g["id"]

        # Kalshi (from the per-sport bulk fetch — no per-game network)
        ac, hc = _our_team_to_kalshi_code(sp, away), _our_team_to_kalshi_code(sp, home)
        kc = {}
        if ac and hc:
            try:
                od = (datetime.fromisoformat(str(g["event_start"]).replace("Z", "+00:00"))
                      .astimezone(ZoneInfo("America/New_York")).date())
                kc = _match_kalshi(kal_idx.get(sp, []), ac, hc, od)
            except Exception:
                kc = {}
        if kc.get("home") or kc.get("away"):
            st["kalshi_games"] += 1

        # PMM (budgeted) — one lookup returns ML + spread + total quotes;
        # all of them land in pm_snapshots (the post-Odds-API history).
        pmm, pmm_rows = {}, []
        if _time.time() < pmm_deadline:
            pq = _pmm_game_quotes(_pm, _pmm_client, away, home, g["event_start"], sport=sp)
            if pq:
                st["pmm_games"] += 1
                pmm = pq.get("ml") or {}
                pmm_rows = pq.get("rows") or []
        else:
            st["pmm_skipped"] += 1

        for (mt, p_side, p_line, c) in pmm_rows:
            rows.append((mid, "pmm", mt, p_side, p_line, c))
        for side in ("home", "away"):
            c = kc.get(side)
            if c is None or c <= 0 or c >= 100:       # no quote / degenerate
                continue
            rows.append((mid, "kalshi", "ml", side, None, int(c)))

        # Capture both feeds' HOME cents for the cross-confirm trigger.
        if (pmm.get("home") not in (None, 0)) and (kc.get("home") not in (None, 0)):
            try:
                sim = round((datetime.fromisoformat(str(g["event_start"]).replace("Z", "+00:00"))
                             - now).total_seconds() / 60)
            except Exception:
                sim = None
            cur[mid] = {"sport": sp, "pmm_home": int(pmm["home"]),
                        "kalshi_home": int(kc["home"]), "starts_in_min": sim}

    inserted = _pm_insert_changed(sb, rows, now)
    xc = _xconfirm_detect(sb, cur, now)
    return jsonify({"ok": True, "games": len(all_games), "inserted": inserted,
                    "kalshi": kal_meta, "xconfirm_triggered": xc["triggered"], **st})


# ───────────── VSiN splits snapshot (Circa + DK handle/bets time series) ─────
# Sports VSiN carries a betting-splits view for. CBB/CFB and NCAAB/NCAAF both
# resolve (see _VSIN_VIEW + the lowercase default), so list both spellings.
_VSIN_SNAP_SPORTS = {"MLB", "NBA", "NHL", "NFL", "NCAAF", "NCAAB", "CBB", "CFB"}
# Watch window per sport — daily-grind sports need ~a day; weekly football
# accrues splits over the week, so watch further out.
_VSIN_SNAP_WINDOW_H = {"NFL": 80, "NCAAF": 80, "CFB": 80}


def _vsin_rows_for_game(vsin: dict | None, mid: str) -> list[tuple]:
    """Flatten a matched _vsin_for_game result into snapshot tuples
    (market_id, book, market_type, side, line, handle_pct, bets_pct) for both
    books × ML/SPR/TOT × both sides. Drops sides VSiN reported nothing for."""
    out = []
    books = (vsin or {}).get("books") or {}
    for book in ("circa", "draftkings"):
        ev = books.get(book)
        if not ev:
            continue
        ml = ev.get("ml") or {}
        for side in ("away", "home"):
            out.append((mid, book, "ml", side, None,
                        ml.get(f"{side}_handle"), ml.get(f"{side}_bets")))
        sp = ev.get("spread") or {}
        for side in ("away", "home"):
            out.append((mid, book, "spread", side, sp.get(f"{side}_line"),
                        sp.get(f"{side}_handle"), sp.get(f"{side}_bets")))
        tt = ev.get("total") or {}
        for side in ("over", "under"):
            out.append((mid, book, "total", side, tt.get("line"),
                        tt.get(f"{side}_handle"), tt.get(f"{side}_bets")))
    return [r for r in out if not (r[5] is None and r[6] is None)]


def _vsin_insert_changed(sb, rows, now) -> int:
    """Insert (mid, book, market_type, side, line, handle, bets) rows, deduped:
    only when (handle, bets) differs from the latest stored value for that key
    (the pm_snapshots dedup-on-change pattern). One row per key per tick."""
    if not rows:
        return 0

    def _lkey(v):
        try:
            return None if v is None else round(float(v), 2)
        except (TypeError, ValueError):
            return None

    mids = list({r[0] for r in rows})
    last: dict = {}
    try:
        recent = (sb.table("vsin_snapshots")
                  .select("market_id,book,market_type,side,line,handle_pct,bets_pct,captured_at")
                  .in_("market_id", mids)
                  .gte("captured_at", (now - timedelta(hours=36)).isoformat())
                  .order("captured_at", desc=True).limit(8000).execute().data) or []
        for r in recent:
            k = (r["market_id"], r["book"], r["market_type"], r["side"], _lkey(r.get("line")))
            if k not in last:                 # first seen = latest (desc order)
                last[k] = (r.get("handle_pct"), r.get("bets_pct"))
    except Exception:
        pass
    ins, seen = [], set()
    for (mid, book, mt, side, line, handle, bets) in rows:
        k = (mid, book, mt, side, _lkey(line))
        if k in seen:
            continue
        seen.add(k)
        if last.get(k) == (handle, bets):     # unchanged → skip
            continue
        ins.append({"market_id": mid, "book": book, "market_type": mt,
                    "side": side, "line": line, "handle_pct": handle,
                    "bets_pct": bets, "captured_at": now.isoformat()})
    if ins:
        try:
            sb.table("vsin_snapshots").insert(ins).execute()
        except Exception:
            return 0
    return len(ins)


@app.route("/api/vsin-snapshot")
def api_vsin_snapshot():
    """Cron-pinged ~every 15 min. Logs Circa + DraftKings handle%/bets% per
    side per market (ML/SPR/TOT) for upcoming games into vsin_snapshots
    (deduped on change). The sharp-money movement curve — feeds model tuning
    (when does sharp money hit Circa?) + the resolver's per-pick closing stamp.
    Auth: ?key= matched to VSIN_SNAPSHOT_SECRET or (fallback) FILLS_CRON_SECRET
    — no new secret needed."""
    expected = (os.environ.get("VSIN_SNAPSHOT_SECRET")
                or os.environ.get("FILLS_CRON_SECRET") or "").strip()
    provided = (request.args.get("key") or "").strip()
    if not expected or not secrets.compare_digest(provided, expected):
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    sb = get_supabase()
    if sb is None:
        return jsonify({"ok": False, "error": "supabase unavailable"}), 503

    import handicapper_web
    now = datetime.now(timezone.utc)
    games = []
    for sp in _VSIN_SNAP_SPORTS:
        hi = (now + timedelta(hours=_VSIN_SNAP_WINDOW_H.get(sp, 24))).isoformat()
        try:
            rows_sp = (sb.table("markets").select("id,event_name,event_start,sport")
                       .eq("sport", sp).eq("status", "active")
                       .gte("event_start", now.isoformat()).lte("event_start", hi)
                       .order("event_start").execute().data) or []
        except Exception:
            rows_sp = []
        seen = set()
        for g in rows_sp:
            n = g.get("event_name") or ""
            if " @ " in n and n not in seen:        # dedup dup market rows
                seen.add(n)
                games.append(g)
    if not games:
        return jsonify({"ok": True, "games": 0, "matched": 0, "inserted": 0})

    rows, matched = [], 0
    for g in games:
        try:
            away, home = [s.strip() for s in g["event_name"].split(" @ ", 1)]
        except ValueError:
            continue
        try:
            vsin = handicapper_web._vsin_for_game(g.get("sport") or "", away, home)
        except Exception:
            vsin = None
        if vsin and vsin.get("matched"):
            matched += 1
            rows.extend(_vsin_rows_for_game(vsin, g["id"]))
    inserted = _vsin_insert_changed(sb, rows, now)
    return jsonify({"ok": True, "games": len(games), "matched": matched,
                    "inserted": inserted})


# Sports the paperlog auto-logs suggestions for. Mirrors _PM_SPORTS (the
# cent-logged sports — a sport with no pm_snapshots history has no exchange
# sharp score / fair anchor, so its dossier can't produce gated suggestions
# worth logging). Off-season sports are free: no games in the 5h window,
# the query just returns nothing. UFC caveat: the window keys off the
# NOMINAL event_start (block-times), so a late-card fight stops logging at
# its nominal time even while still 'pre' — acceptable, forward data accrues.
_PAPERLOG_SPORTS = ["MLB", "NBA", "NHL", "NFL", "NCAAF", "UFC"]


@app.route("/api/handicapper/paperlog")
def api_handicapper_paperlog():
    """Cron-pinged ~1/min. Auto-logs every GATE-CLEARED Pick Bot suggestion
    for upcoming games (every _PAPERLOG_SPORTS sport — was MLB-only until
    the July 2026 football prep) across the 5h->1min pre-game window into
    pickbot_paperlog — one row per (game, market) each time the suggested
    (side, units) CHANGES (a flip or a size up/down). The complete bot-
    suggestion dataset for the 2-week review, separate from the user's real
    bot_picks. No forced leans (gates_cleared only). Budgeted (~8s) so we
    never exceed Vercel's 10s; stale-game-first ordering cycles coverage
    across ticks. Auth: ?key= matched to PAPERLOG_SECRET or FILLS_CRON_SECRET."""
    import time as _time
    import handicapper_web
    expected = (os.environ.get("PAPERLOG_SECRET")
                or os.environ.get("FILLS_CRON_SECRET") or "").strip()
    provided = (request.args.get("key") or "").strip()
    if not expected or not secrets.compare_digest(provided, expected):
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    sb = get_supabase()
    if sb is None:
        return jsonify({"ok": False, "error": "supabase unavailable"}), 503

    now = datetime.now(timezone.utc)
    lo = (now + timedelta(minutes=1)).isoformat()    # stop 1 min before tip
    hi = (now + timedelta(hours=5)).isoformat()      # start 5h out
    try:
        raw = (sb.table("markets").select("id,event_name,event_start,sport")
               .in_("sport", _PAPERLOG_SPORTS).eq("status", "active")
               .gte("event_start", lo).lte("event_start", hi)
               .order("event_start").execute().data) or []
    except Exception as e:
        return jsonify({"ok": False, "error": f"markets: {e}"}), 500
    games, seen = [], set()           # dedup duplicate market rows (gotcha #30)
    for g in raw:
        n = g.get("event_name") or ""
        if n and n not in seen:
            seen.add(n)
            games.append(g)
    if not games:
        return jsonify({"ok": True, "games": 0, "reason": "no upcoming games in 5h"})

    # Recent paperlog → (a) last logged_at/game for stale-first ordering,
    # (b) last (side,units) per (game,market) for the bet-change dedup.
    mids = [g["id"] for g in games]
    last_logged, last_bet = {}, {}
    # Dedup key carries a VARIANT discriminator: '' (the real suggestion),
    # 'model' (the shadow spread model), 'veto' (a VSiN-vetoed would-be pick).
    # Without it, two writers sharing (market_id,'spread') — the steam spread
    # on one side, the shadow model on the other — alternate the stored
    # (side,units) so EVERY tick reads as a bet change and logs both rows
    # forever (the June 28 flood: 239 rows/day, one game logged 15+ times,
    # which inflated the shadow spread record to a fake 131-13).
    def _variant(blob) -> str:
        b = blob or {}
        if b.get("spread_model") in (True, "true"):
            return "model"
        if b.get("vsin_vetoed_pick") in (True, "true"):
            return "veto"
        return ""
    try:
        recent = (sb.table("pickbot_paperlog")
                  .select("market_id,market_type,side,units,logged_at,signal_blob")
                  .in_("market_id", mids)
                  .gte("logged_at", (now - timedelta(hours=6)).isoformat())
                  .order("logged_at", desc=True).limit(5000).execute().data) or []
        for r in recent:
            last_logged.setdefault(r["market_id"], r["logged_at"])
            last_bet.setdefault(
                (r["market_id"], r["market_type"], _variant(r.get("signal_blob"))),
                (r.get("side"), r.get("units")))
    except Exception:
        pass
    # (dedup keys, stale-first ordering, and the dossier build below are all
    # sport-agnostic — nothing else in this loop assumes MLB.)
    games.sort(key=lambda g: last_logged.get(g["id"]) or "")   # never-logged first

    deadline = _time.time() + 8.0
    rows, processed, with_pick = [], 0, 0
    for g in games:
        if _time.time() >= deadline:
            break
        try:
            d = handicapper_web.build_dossier(sb, None, None, market_id=g["id"])
        except Exception:
            continue
        processed += 1
        en, es, sp = d.get("event_name"), d.get("event_start_utc"), d.get("sport")
        sim = d.get("starts_in_min")
        tw = handicapper_web._timing_window(sim)
        pc = handicapper_web._is_prime_core(sim)

        bets = []
        for s in (d.get("suggestions") or []):
            # Gate-cleared picks AND VSiN-vetoed would-be picks (shadow rows,
            # gates_cleared=false + signal_blob.vsin_vetoed_pick) — the latter
            # give the Circa veto a measurable counterfactual. Plain forced
            # leans still never log.
            if not (s.get("market_type") and s.get("side")):
                continue
            if not (s.get("gates_cleared") or s.get("vsin_vetoed_pick")):
                continue
            entry = s.get("pmm_bid_american")
            if entry is None:
                entry = s.get("fair_american")
            bets.append({
                "market_type": s["market_type"], "side": s["side"],
                "units": s.get("units"), "confidence": s.get("confidence"),
                "line": (s.get("pmm_line") if s.get("uses_pmm_projection") else s.get("pin_line")),
                "entry_price": entry, "fair_american": s.get("fair_american"),
                "sharp_score": s.get("sharp_score"), "edge_pp": s.get("edge_pp"),
                "gates_cleared": bool(s.get("gates_cleared")),
                "signal_blob": {"combined_score": s.get("combined_score"),
                                "model_edge_pp": s.get("model_edge_pp"),
                                "splits_pp": s.get("splits_pp"),
                                "sticky": bool(s.get("sticky")),
                                "x_score": s.get("x_score"),
                                "x_side": s.get("x_side"),
                                "x_agree": s.get("x_agree"),
                                "vsin": s.get("vsin"),
                                "vsin_veto": bool(s.get("vsin_veto")),
                                "vsin_vetoed_pick": bool(s.get("vsin_vetoed_pick")),
                                # Circa handle trajectory on this side at log
                                # time (full-window Δpp from vsin_snapshots) —
                                # the movement-curve signal under review.
                                "circa_move_pp": s.get("circa_move_pp"),
                                # TEST O/U tier — model-driven totals at 0.25u.
                                # Tagged so the 2-week review can isolate them.
                                "test_only": bool(s.get("test_only")),
                                "model_total_diff": s.get("model_total_diff"),
                                "uses_pmm_projection": s.get("uses_pmm_projection")},
            })
        nrfi = d.get("nrfi") or {}
        if nrfi.get("gates_cleared") and nrfi.get("bet_side"):
            bs = nrfi["bet_side"]
            bets.append({
                # 1u — matches the LIVE NRFI sizing (promoted from 0.5u June
                # 2026); the writer was stale at 0.5 so paperlog NRFI pnl
                # understated the real bet by half.
                "market_type": "nrfi", "side": bs, "units": 1, "confidence": "low",
                "line": None, "entry_price": nrfi.get("entry_price"),
                "fair_american": (nrfi.get("nrfi_fair_american") if bs == "no"
                                  else nrfi.get("yrfi_fair_american")),
                "sharp_score": None, "edge_pp": nrfi.get("bet_edge_pp"),
                "signal_blob": {"p_nrfi": nrfi.get("p_nrfi")},
            })
        # SHADOW market-anchored spread (direction from exchange ML + run model).
        # Logged at a flat 1u, flagged signal_blob.spread_model so the 2-week
        # review can isolate it (filter signal_blob->>'spread_model'='true').
        spm = d.get("spread_model") or {}
        if spm.get("bet_side"):
            bs = spm["bet_side"]
            bets.append({
                "market_type": "spread", "side": bs, "units": 1, "confidence": "low",
                "line": spm.get("line"), "entry_price": spm.get("entry_price"),
                "fair_american": spm.get("fair_american"),
                "sharp_score": None, "edge_pp": spm.get("bet_edge_pp"),
                "signal_blob": {"spread_model": True, "p_home_win": spm.get("p_home_win"),
                                "proj_total": spm.get("proj_total"),
                                "home_cover": spm.get("home_cover"),
                                "away_cover": spm.get("away_cover")},
            })
        if bets:
            with_pick += 1
        for b in bets:
            k = (g["id"], b["market_type"], _variant(b.get("signal_blob")))
            if last_bet.get(k) == (b["side"], b["units"]):   # unchanged bet → skip
                continue
            last_bet[k] = (b["side"], b["units"])            # no dup within this tick
            rows.append({
                "market_id": g["id"], "event_name": en, "event_start": es, "sport": sp,
                "market_type": b["market_type"], "side": b["side"], "line": b["line"],
                "entry_price": b["entry_price"], "units": b["units"],
                "confidence": b["confidence"], "timing_window": tw, "prime_core": pc,
                "starts_in_min": (round(sim) if sim is not None else None),
                "sharp_score": b["sharp_score"], "edge_pp": b["edge_pp"],
                "fair_american": b["fair_american"],
                "gates_cleared": b.get("gates_cleared", True),
                "signal_blob": b["signal_blob"], "logged_at": now.isoformat(),
            })
    new_rows = 0
    if rows:
        try:
            sb.table("pickbot_paperlog").insert(rows).execute()
            new_rows = len(rows)
        except Exception as e:
            return jsonify({"ok": False, "error": f"insert: {e}",
                            "processed": processed}), 500
    return jsonify({"ok": True, "games": len(games), "processed": processed,
                    "with_pick": with_pick, "new_rows": new_rows})


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
# Polymarket fill alerts — Telegram notification when an order on
# Polymarket fills. The US Polymarket app has no native fill
# notifications (only the international web app does), so we poll the
# SDK every minute via cron-job.org pinging /api/polymarket/check-fills
# and diff cum_quantity against polymarket_fill_state in Supabase.
#
# Auth: shared-secret ?key= matched to FILLS_CRON_SECRET env var. Cron
# services can't carry Firebase tokens; the secret is the gate.
#
# Milestones tracked per order (25 / 50 / 75 / 100). Only the highest
# milestone newly crossed in any single tick fires a Telegram message —
# a market order that fills 0 → 100% in one tick sends one alert, not
# four. The alerts_sent jsonb array on each row prevents re-firing.
#
# First-sight safety:
#   • Fresh open order   → snapshot only, no alert (placement isn't a fill)
#   • Fresh terminal old → snapshot terminal=true, no alert (historical)
#   • Fresh terminal new → fire "100" alert (instant fill between ticks).
#     "New" = createTime within FILL_FRESH_TERMINAL_SECONDS.
# ---------------------------------------------------------------------------

# SDK order states that mean "this order will never fill more". Once we
# see a row in one of these, mark terminal so the next cron tick can
# fast-skip it.
_TERMINAL_ORDER_STATES = {
    "ORDER_STATE_FILLED",
    "ORDER_STATE_CANCELED",
    "ORDER_STATE_EXPIRED",
    "ORDER_STATE_REJECTED",
}

# Fill-progress milestones. (key, threshold_pct). "100" has an extra
# gate (requires FILLED state OR pct >= 100) handled in
# _crossed_fill_milestones — partials can hover at 99.7% for a while
# without us calling them "filled".
_FILL_MILESTONES = (("25", 25.0), ("50", 50.0), ("75", 75.0), ("100", 100.0))

# An order we've never seen before that's ALREADY in a terminal state
# is either (a) historical, or (b) a brand-new market order that filled
# instantly between cron ticks. We use createTime to tell them apart:
# fresh terminal = real instant fill (fire 100 alert); old terminal =
# historical (snapshot, skip). 10 min covers a typical 1-min cron with
# generous tolerance for cron-job.org occasional misses.
_FILL_FRESH_TERMINAL_SECONDS = 600


def _fmt_pmm_price(p):
    """Polymarket prices are 0-1 probabilities. Render as $0.42."""
    if p is None:
        return "?"
    try:
        return f"${float(p):.2f}"
    except (TypeError, ValueError):
        return "?"


def _send_fill_telegram(text):
    """POST to Telegram sendMessage via the "Filled Bot" — a dedicated
    Telegram bot for Polymarket fill notifications. No-op (False) when
    FILLED_BOT_TOKEN / FILLED_BOT_CHAT_ID aren't set in Vercel env.
    Stdlib urllib so we don't add a new dep just for this.

    Distinct from the retired "sharp alerts" Telegram bot — the user
    explicitly asked for a separate bot so fill messages are clearly
    labeled in their Telegram client."""
    import urllib.request
    import urllib.error
    token = (os.environ.get("FILLED_BOT_TOKEN") or "").strip()
    chat_id = (os.environ.get("FILLED_BOT_CHAT_ID") or "").strip()
    if not token or not chat_id:
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }).encode()
    req = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status == 200
    except (urllib.error.HTTPError, urllib.error.URLError):
        return False


def _crossed_fill_milestones(prev_pct, curr_pct, curr_state, already_sent):
    """Return list of milestone keys newly crossed this tick (in
    ascending threshold order). A milestone fires once per (order,
    milestone) — already_sent is the persisted jsonb list."""
    out = []
    for key, threshold in _FILL_MILESTONES:
        if key in already_sent:
            continue
        if key == "100":
            crossed = (curr_state == "ORDER_STATE_FILLED") or curr_pct >= 100
        else:
            crossed = curr_pct >= threshold
        if crossed:
            out.append(key)
    return out


def _is_fresh_terminal(order_created_at):
    """True iff the order's createTime is within the last
    _FILL_FRESH_TERMINAL_SECONDS. Used to decide whether a first-sight
    already-terminal order is a real instant fill (fire alert) or just
    history (snapshot, skip). Returns False on parse failure — safer to
    skip than spam."""
    if not order_created_at:
        return False
    try:
        # SDK returns ISO 8601; tolerate both "Z" and "+00:00".
        s = order_created_at.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds() <= _FILL_FRESH_TERMINAL_SECONDS
    except (ValueError, AttributeError):
        return False


def _format_fill_alert(row, milestone, fill_pct):
    """Build the Telegram message for a fill milestone.

    Visually distinguishes buys vs sells via emoji + verb in the header:
      buys  → ✅ FILLED  /  📈 25% FILLED
      sells → 💰 SOLD    /  📤 25% SOLD
    so the user can tell at a glance whether a notification is about
    entering or exiting a position without having to read the side label."""
    pick = row.get("pick") or row.get("market_name") or "(unknown)"
    market = row.get("market_name") or ""
    price = _fmt_pmm_price(row.get("price"))
    side = row.get("side_label") or ""
    qty = row.get("quantity") or 0
    cum = row.get("last_cum_quantity") or 0

    is_sell = (row.get("intent") or "").startswith("ORDER_INTENT_SELL_")
    verb = "SOLD" if is_sell else "FILLED"
    full_emoji = "💰" if is_sell else "✅"
    partial_emoji = "📤" if is_sell else "📈"

    if milestone == "100":
        header = f"{full_emoji} *{verb}*"
        progress = f"{int(cum)}/{int(qty)} shares"
    else:
        header = f"{partial_emoji} *{milestone}% {verb}*"
        progress = f"{int(cum)}/{int(qty)} shares ({fill_pct:.0f}%)"

    lines = [header]
    if market and market != pick:
        lines.append(f"_{market}_")
    lines.append(f"*{pick}* · {side} @ {price}")
    lines.append(progress)
    return "\n".join(lines)


# ──────────────── Pick Bot prime-window alerts ────────────────
# Telegram heads-up (via the Filled Bot) when a cluster of games crosses
# into the PRIME betting window — 90-120 min before first pitch, the
# 1.5-2h band where the bot's live picks demonstrably print (68.4%,
# +27u over 38 picks in the 118-pick review). One BATCHED message per
# sport ("5 MLB games entering the prime betting window"), NOT one per
# game. Dedup via the `prime_alerts` table so each game pings once.
#
# Pinged every ~1 min by the scanner cron (a curl step alongside the
# fill-alert ping — no new cron-job.org job). Auth: shared-secret ?key=
# matched to PRIME_CRON_SECRET, falling back to FILLS_CRON_SECRET so no
# new secret is required. Telegram routes through _send_fill_telegram
# (the Filled Bot creds already in Vercel env).
# Fire the heads-up as games cross the TOP of the prime window (now 3h,
# extended from 2h June 2026) — you want the full prime window to act.
_PRIME_LO_MIN       = 150   # consider games 150+ min out for the entering-cluster query
_PRIME_HI_MIN       = 180   # fire when the earliest fresh game is <= 180 min out (just entered prime / 3h)
_PRIME_BATCH_HI_MIN = 210   # look 30 min past prime to pull a whole cluster into one alert
_PRIME_QUIET_BEFORE = 7     # don't alert before 7am AZ (no overnight/early-morning pings)


def _fmt_az_time(dt: datetime) -> str:
    """UTC datetime → 'h:MM AM' in Arizona time (no leading zero)."""
    local = dt.astimezone(ZoneInfo("America/Phoenix"))
    return local.strftime("%I:%M %p").lstrip("0")


@app.route("/api/handicapper/prime-alert")
def api_handicapper_prime_alert():
    """Send a batched Telegram alert when games enter the prime betting
    window (90-120 min pre-tip). One message per sport for the whole
    cluster; deduped via `prime_alerts`. Returns a small JSON payload so
    the cron-job history shows what fired."""
    expected = (os.environ.get("PRIME_CRON_SECRET")
                or os.environ.get("FILLS_CRON_SECRET") or "").strip()
    provided = (request.args.get("key") or "").strip()
    if not expected or not secrets.compare_digest(provided, expected):
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    sb = get_supabase()
    if sb is None:
        return jsonify({"ok": False, "error": "supabase unavailable"}), 503

    now = datetime.now(timezone.utc)
    # Quiet hours — no pings before 7am Arizona time (a game would have to
    # start ~9am for its prime window to land that early; none do).
    if now.astimezone(ZoneInfo("America/Phoenix")).hour < _PRIME_QUIET_BEFORE:
        return jsonify({"ok": True, "alerted": 0, "reason": "quiet hours"})

    # Fire as games cross the OUTER edge of the (data-driven, multi-zone)
    # prime window — the same zones the website uses, so the alert tracks
    # the tuner instead of a hardcoded 3h. env_hi = the far edge of prime;
    # alerting there gives the user the whole window to act. Falls back to
    # the constants if the tuner row is unreadable.
    try:
        _zones = handicapper_web._load_prime_zones(sb) or []
        env_hi = max((hi for _lo, hi in _zones), default=_PRIME_HI_MIN)
    except Exception:
        env_hi = _PRIME_HI_MIN
    fire_min = int(env_hi)                 # fire when the earliest fresh game ≤ this
    lo_min   = max(0, fire_min - 30)       # 60-min cluster straddling the edge
    batch_hi = fire_min + 30
    lo = (now + timedelta(minutes=lo_min)).isoformat()
    hi = (now + timedelta(minutes=batch_hi)).isoformat()
    prime_hi = now + timedelta(minutes=fire_min)

    try:
        rows = (sb.table("markets")
                .select("id,sport,event_name,event_start")
                .eq("status", "active")
                .gte("event_start", lo)
                .lte("event_start", hi)
                .order("event_start")
                .execute().data) or []
    except Exception as e:
        return jsonify({"ok": False, "error": f"markets query: {e}"}), 500
    if not rows:
        return jsonify({"ok": True, "alerted": 0, "reason": "no games in window"})

    # Drop games we've already pinged (per-game dedup).
    ids = [r["id"] for r in rows]
    try:
        ex = (sb.table("prime_alerts").select("market_id")
              .in_("market_id", ids).execute().data) or []
        already = {e["market_id"] for e in ex}
    except Exception as e:
        # Table missing/unreadable — bail rather than risk spamming.
        return jsonify({"ok": False, "error": f"prime_alerts read: {e}"}), 500
    fresh = [r for r in rows if r["id"] not in already]
    if not fresh:
        return jsonify({"ok": True, "alerted": 0, "reason": "all already alerted"})

    # Group by sport; only fire a sport whose EARLIEST fresh game has
    # actually entered prime (≤120 min out) — so we don't pre-fire a
    # cluster that's still 2h+ away. Once fired, the whole batch (out to
    # +150 min) is marked so the trailing games don't re-ping.
    by_sport: dict[str, list] = {}
    for r in fresh:
        by_sport.setdefault(r["sport"], []).append(r)

    sports_fired = 0
    marked: list[dict] = []
    for sport, games in by_sport.items():
        games.sort(key=lambda g: g["event_start"])
        try:
            earliest = datetime.fromisoformat(
                str(games[0]["event_start"]).replace("Z", "+00:00"))
        except Exception:
            continue
        if earliest > prime_hi:
            continue  # leading edge not in prime yet — wait for a later tick

        if _send_fill_telegram(_build_prime_alert_msg(sport, games)):
            sports_fired += 1
            for g in games:
                marked.append({"market_id": g["id"], "sport": sport,
                               "event_start": g["event_start"],
                               "alerted_at": now.isoformat()})

    if marked:
        try:
            sb.table("prime_alerts").upsert(marked).execute()
        except Exception as e:
            # Send happened but the mark failed — next tick may re-ping.
            # Rare; surfaced in the response for visibility.
            return jsonify({"ok": False, "error": f"mark failed: {e}",
                            "sports_fired": sports_fired}), 500

    # Opportunistic cleanup — drop rows older than 2 days (markets long over).
    try:
        cutoff = (now - timedelta(days=2)).isoformat()
        sb.table("prime_alerts").delete().lt("alerted_at", cutoff).execute()
    except Exception:
        pass

    return jsonify({"ok": True, "sports_fired": sports_fired,
                    "games_marked": len(marked)})


def _build_prime_alert_msg(sport: str, games: list) -> str:
    """Batched Telegram message body for one sport's prime-window cluster.
    Markdown — matches the Filled Bot's other messages."""
    n = len(games)
    starts = []
    for g in games:
        try:
            starts.append(datetime.fromisoformat(
                str(g["event_start"]).replace("Z", "+00:00")))
        except Exception:
            pass
    when = ""
    if starts:
        # Sport-neutral — these clusters can be MLB/soccer/UFC, so "Starts",
        # not "First pitch". Lead time is computed (was a stale hardcoded
        # "~3h" that drifted when the prime window changed).
        lo_t, hi_t = _fmt_az_time(min(starts)), _fmt_az_time(max(starts))
        when = (f"🕐 Starts {lo_t} AZ" if lo_t == hi_t
                else f"🕐 Starts {lo_t}–{hi_t} AZ")
        try:
            mins = int((min(starts) - datetime.now(timezone.utc)).total_seconds() // 60)
            if mins > 0:
                h, mm = divmod(mins, 60)
                lead = (f"{h}h {mm}m" if h else f"{mm}m")
                when += f"  (~{lead} out)"
        except Exception:
            pass
    plural = "game" if n == 1 else "games"
    lines = [f"🎯 *{n} {sport} {plural}* entering the prime betting window"]
    if when:
        lines.append(when)
    for g in games[:8]:
        lines.append(f"• {g.get('event_name', '?')}")
    if n > 8:
        lines.append(f"…and {n - 8} more")
    lines.append("Make your picks → thekahlahouse.com/handicapper")
    return "\n".join(lines)


@app.route("/api/polymarket/check-fills")
def api_check_fills():
    """Polymarket order fill detector. Pulled every ~1 min by
    cron-job.org. Diffs current SDK orders against
    polymarket_fill_state, sends Telegram per milestone crossed,
    persists new state. Returns a small JSON payload that shows up in
    cron-job.org's response history for live debugging."""
    expected = (os.environ.get("FILLS_CRON_SECRET") or "").strip()
    provided = (request.args.get("key") or "").strip()
    if not expected or not secrets.compare_digest(provided, expected):
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    sb = get_supabase()
    if sb is None:
        return jsonify({"ok": False, "error": "supabase unavailable"}), 503

    # Pull orders from Polymarket. Failure = bail with no state mutation;
    # the next tick will retry cleanly. Don't half-update state.
    try:
        client = get_client()
        resp = client.orders.list()
        raw = resp.get("orders") if isinstance(resp, dict) else getattr(resp, "orders", []) or []
    except Exception as e:
        return jsonify({"ok": False, "error": f"sdk: {e}"}), 502

    order_ids = []
    for o in raw:
        oid = (o.get("id") if isinstance(o, dict) else getattr(o, "id", None))
        if oid:
            order_ids.append(oid)
    seen_order_ids = set(order_ids)

    state_map = {}
    if order_ids:
        try:
            rows = (sb.table("polymarket_fill_state")
                      .select("*").in_("order_id", order_ids).execute().data) or []
            for r in rows:
                state_map[r["order_id"]] = r
        except Exception as e:
            return jsonify({"ok": False, "error": f"db read: {e}"}), 500

    processed = 0
    alerts_fired = 0
    skipped_historical = 0
    upserts = []
    now_iso = datetime.now(timezone.utc).isoformat()

    for o in raw:
        def _g(key, default=None, _o=o):
            if isinstance(_o, dict): return _o.get(key, default)
            return getattr(_o, key, default)

        oid = _g("id") or ""
        if not oid:
            continue

        state = _g("state") or ""
        prev = state_map.get(oid)
        # Fast-skip orders we already marked terminal — saves rebuilding
        # rows for every long-dead order the SDK still returns.
        if prev and prev.get("terminal"):
            continue

        qty = _safe_float(_g("quantity")) or 0
        cum_qty = _safe_float(_g("cumQuantity")) or 0
        raw_price = _safe_float(_g("price"))
        intent = _g("intent") or ""
        # Same NO-side price flip as /api/my-orders — display what the
        # user actually paid for their picked outcome, not the
        # YES-canonical complement.
        needs_flip = intent.endswith("_SHORT")
        if raw_price is not None and 0 <= raw_price <= 1 and needs_flip:
            price = 1 - raw_price
        else:
            price = raw_price

        md = _g("marketMetadata") or {}
        if not isinstance(md, dict):
            md = {k: getattr(md, k, None) for k in
                  ("slug", "title", "outcome", "eventSlug", "team")}
        slug = md.get("slug") or ""
        title = md.get("title") or ""
        outcome = md.get("outcome") or ""
        team = md.get("team") or {}
        team_name = team.get("name", "") if isinstance(team, dict) else ""

        pick = outcome
        if team_name and outcome and re.search(r"[0-9]", outcome):
            pick = f"{team_name} {outcome}"
        elif team_name:
            pick = team_name

        # Side word: mirror the Polymarket app + the dashboard betslip —
        # show the AFFIRMATIVE of the outcome you actually picked
        # ("Switzerland -2.5 · Yes"), NOT the canonical BUY YES/NO from
        # the order intent. On spread / "wins by N goals" markets the
        # picked side is Polymarket's canonical NO, so the intent-based
        # label (_INTENT_LABEL → "BUY NO") reads as the opposite of what
        # you hold — the synthetic-side inversion. `outcome` already
        # encodes the side you selected, and the _SHORT price flip above
        # already gives THAT side's price, so a fill on it = you're on
        # `pick` → "Yes". Only honor a literal yes/no when the market
        # gives nothing else to name the side by.
        _ro = outcome.strip().lower()
        side_label = _ro.title() if _ro in ("yes", "no") else "Yes"
        order_created_at = _g("createTime") or _g("insertTime") or ""

        fill_pct = (cum_qty / qty * 100) if qty else 0
        already_sent = (prev or {}).get("alerts_sent") or []
        is_terminal_now = state in _TERMINAL_ORDER_STATES

        new_milestones = []
        if not prev:
            # First-sight order. Three sub-cases:
            #   1) Open + un-filled         → snapshot, no alert
            #   2) Terminal + recent create → fired between ticks, ALERT
            #   3) Terminal + old create    → historical, snapshot only
            if is_terminal_now and state == "ORDER_STATE_FILLED" \
                    and _is_fresh_terminal(order_created_at):
                new_milestones = ["100"]
            elif is_terminal_now:
                skipped_historical += 1
        else:
            prev_pct = ((prev.get("last_cum_quantity") or 0) / qty * 100) if qty else 0
            new_milestones = _crossed_fill_milestones(
                prev_pct=prev_pct, curr_pct=fill_pct,
                curr_state=state, already_sent=already_sent)

        new_alerts = list(already_sent) + [m for m in new_milestones if m not in already_sent]

        row_snapshot = {
            "order_id": oid,
            "market_name": title,
            "pick": pick,
            "slug": slug,
            "intent": intent,
            "side_label": side_label,
            "quantity": qty,
            "price": price,
            "last_cum_quantity": cum_qty,
            "last_state": state,
            "alerts_sent": new_alerts,
            "order_created_at": order_created_at,
            "last_seen_at": now_iso,
            "terminal": is_terminal_now,
        }
        # Deliberately omit first_seen_at — the DB default `now()`
        # handles initial INSERT, and omitting it from UPDATE preserves
        # the original value across upserts.

        if new_milestones:
            top = new_milestones[-1]  # highest threshold crossed this tick
            msg = _format_fill_alert(row_snapshot, top, fill_pct)
            if _send_fill_telegram(msg):
                alerts_fired += 1
            # Even if Telegram send returned False, mark all crossed
            # milestones as sent. A Telegram outage shouldn't queue up
            # alerts that flood when the bot recovers — sharp_alerts.py
            # treats Telegram as best-effort for the same reason.

        upserts.append(row_snapshot)
        processed += 1

    # ── Disappeared-order detector ────────────────────────────────
    # Polymarket SDK's orders.list() returns ONLY currently-open orders;
    # once an order fills (or is canceled / expired) it vanishes from
    # the list entirely. So my milestone-diff loop above can never fire
    # a 100% alert for a fully-filled order — by the time it's filled,
    # we don't see it anymore. To detect full fills we:
    #
    #   1. Query state table for rows we know are still non-terminal.
    #   2. Any non-terminal row whose order_id isn't in this tick's SDK
    #      response is "disappeared" → it either filled or was canceled.
    #   3. Cross-reference recent ACTIVITY_TYPE_TRADE entries against
    #      each disappeared order's marketSlug. A matching trade in the
    #      last ~30 min implies the order filled. No match implies the
    #      user canceled it (no alert — user knows they canceled).
    #
    # First-tick-with-no-prior-state still snapshots open orders; the
    # disappearance path only fires once we have history to compare
    # against (i.e. starting on the SECOND cron tick after a deploy).
    disappeared_filled = 0
    disappeared_canceled = 0
    try:
        known_active = (sb.table("polymarket_fill_state")
                          .select("*").eq("terminal", False).execute().data) or []
    except Exception:
        known_active = []

    disappeared = [r for r in known_active if r["order_id"] not in seen_order_ids]

    if disappeared:
        # One activities page (most-recent first) is more than enough for
        # any realistic per-user trade volume between two cron ticks.
        # Additionally, only accept trades that happened in the last 3
        # minutes — otherwise old historical trades on the same slug
        # (e.g. a buy earlier today) can be falsely matched to a brand-
        # new sell order that just disappeared, firing a spurious SOLD
        # alert. 3 min absorbs cron jitter while keeping the window
        # tight enough that only the actual triggering trade matches.
        recent_trades = []
        trade_cutoff = datetime.now(timezone.utc) - timedelta(minutes=3)
        try:
            act_resp = client.portfolio.activities(params={"limit": 100})
            for act in act_resp.get("activities", []):
                if act.get("type") != "ACTIVITY_TYPE_TRADE":
                    continue
                # SDK returns trade detail nested under "trade" key —
                # NOT under "ACTIVITY_TYPE_TRADE" as the activity type
                # string would suggest. Verified empirically.
                detail = act.get("trade")
                if not isinstance(detail, dict):
                    # Defensive fallback for any future shape drift.
                    for k, v in act.items():
                        if k != "type" and isinstance(v, dict):
                            detail = v
                            break
                if not isinstance(detail, dict) or not detail.get("marketSlug"):
                    continue
                t_str = detail.get("updateTime") or detail.get("timestamp") or ""
                if not t_str:
                    continue
                try:
                    t_dt = datetime.fromisoformat(t_str.replace("Z", "+00:00"))
                    if t_dt.tzinfo is None:
                        t_dt = t_dt.replace(tzinfo=timezone.utc)
                except (ValueError, AttributeError):
                    continue
                if t_dt < trade_cutoff:
                    # Activities are most-recent-first — once we see
                    # one older than the cutoff, the rest are too.
                    break
                recent_trades.append(detail)
        except Exception:
            recent_trades = []

        for row in disappeared:
            slug = row.get("slug") or ""
            qty = _safe_float(row.get("quantity")) or 0

            # Consume the first matching trade so two simultaneous orders
            # on the same market don't both match the same trade.
            match_idx = None
            for i, t in enumerate(recent_trades):
                if t.get("marketSlug") == slug:
                    match_idx = i
                    break

            sent = list(row.get("alerts_sent") or [])
            row_snapshot = {
                "order_id":      row["order_id"],
                "market_name":   row.get("market_name"),
                "pick":          row.get("pick"),
                "slug":          row.get("slug"),
                "intent":        row.get("intent"),
                "side_label":    row.get("side_label"),
                "quantity":      qty,
                "price":         row.get("price"),
                "last_cum_quantity": row.get("last_cum_quantity") or 0,
                "last_state":    row.get("last_state"),
                "alerts_sent":   sent,
                "order_created_at": row.get("order_created_at"),
                "last_seen_at":  now_iso,
                "terminal":      True,
            }

            if match_idx is not None:
                recent_trades.pop(match_idx)
                disappeared_filled += 1
                # Fire 100% alert if we haven't already. Update the
                # snapshot to reflect the full fill in the message body.
                if "100" not in sent:
                    row_snapshot["last_cum_quantity"] = qty
                    row_snapshot["last_state"] = "ORDER_STATE_FILLED"
                    row_snapshot["alerts_sent"] = sent + ["100"]
                    msg = _format_fill_alert(row_snapshot, "100", 100.0)
                    if _send_fill_telegram(msg):
                        alerts_fired += 1
            else:
                disappeared_canceled += 1
            # Either way, mark terminal so we stop scanning this row.

            upserts.append(row_snapshot)

    if upserts:
        try:
            (sb.table("polymarket_fill_state")
               .upsert(upserts, on_conflict="order_id").execute())
        except Exception as e:
            return jsonify({"ok": False, "error": f"db write: {e}",
                            "processed": processed, "alerts": alerts_fired}), 500

    return jsonify({
        "ok": True,
        "processed": processed,
        "alerts_fired": alerts_fired,
        "skipped_historical": skipped_historical,
        "disappeared_filled": disappeared_filled,
        "disappeared_canceled": disappeared_canceled,
    })


# ---------------------------------------------------------------------------
# CLV (Closing Line Value) — how each Polymarket bet compares to PIN's

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
@odds_required
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
                        or (a["type"] in _REWARD_TYPE_LABELS and a.get("pnl") is not None)]

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
# Handicapper Bot — picks tracker
# ---------------------------------------------------------------------------

@app.route("/api/handicapper")
@bot_required   # PER-USER: every bot_access user logs + tracks their OWN book. Scoped to asked_by==g.uid below — each user sees only their own pending/settled/stats/CLV. The aggregate of ALL users' picks (the model-tuning signal) lives behind the admin-only /api/handicapper/analytics surface.
def api_handicapper():
    """JSON for /handicapper. Returns the CALLER'S OWN pending picks (no age
    cap), their settled picks from the last 30d, and their rollup stats from
    bot_picks. Every row is filtered to asked_by==g.uid so each user's book +
    ROI is private to them.

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
    yesterday_start_iso = ((today_start_local - timedelta(days=1))
                           .astimezone(timezone.utc).isoformat())
    cutoff_7d = (now - timedelta(days=7)).isoformat()

    cols = ("id,market_id,picked_at,asked_by,query_text,sport,event_name,event_start,"
            "market_type,side,entry_book,entry_price,entry_line,"
            "units,confidence,fair_prob,edge_pp,sharp_score,clv_pp,"
            "analysis_md,reasons,"
            "status,pnl_units,result_score,settled_at,"
            "actual_fill_price,actual_fill_qty,actual_fill_line,actual_fill_pnl,"
            "polymarket_outcome,pmm_side,auto_linked")
    try:
        pending = (sb.table("bot_picks").select(cols)
                   .eq("status", "pending")
                   .eq("asked_by", g.uid)
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
                       .eq("asked_by", g.uid)
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
                "pnl": 0.0, "hit_rate": None, "roi": None,
                # CLV (closing line value) rollup. clv_sum / clv_n track
                # the running average across picks that HAVE a CLV value
                # (PIN closing pair existed); avg_clv_pp is finalized
                # below. CLV is the edge-proof metric — positive average
                # = the bot is consistently beating the close, which is
                # +EV regardless of short-run W/L variance.
                "clv_sum": 0.0, "clv_n": 0, "avg_clv_pp": None}

    overall_today  = _new_bucket()
    overall_yesterday = _new_bucket()
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
        try:
            clv = r.get("clv_pp")
            clv = float(clv) if clv is not None else None
        except (TypeError, ValueError):
            clv = None

        def _add(bucket):
            bucket["graded"] += 1
            bucket[st] += 1
            bucket["pnl"] += pnl
            if clv is not None:
                bucket["clv_sum"] += clv
                bucket["clv_n"] += 1

        for bucket in (overall_30d, by_conf.get(r.get("confidence"))):
            if bucket is None:
                continue
            _add(bucket)
        if event_start >= cutoff_7d:
            _add(overall_week)
        if event_start >= today_start_iso:
            _add(overall_today)
        elif event_start >= yesterday_start_iso:
            _add(overall_yesterday)

    def _finalize(s: dict) -> None:
        decided = s["won"] + s["lost"]
        if decided > 0:
            s["hit_rate"] = round(s["won"] / decided, 4)
        if s["graded"] > 0:
            s["roi"] = round(s["pnl"] / s["graded"], 4)
        if s["clv_n"] > 0:
            s["avg_clv_pp"] = round(s["clv_sum"] / s["clv_n"], 2)
        s["pnl"] = round(s["pnl"], 3)

    for s in (overall_today, overall_yesterday, overall_week, overall_30d,
              *by_conf.values()):
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
        "stats_yesterday": overall_yesterday,
        "stats_week":   overall_week,
        "stats_30d":    overall_30d,
        "stats":        overall_30d,        # back-compat alias = 30d
        "stats_by_confidence": by_conf,
        "resolver":     resolver,
    })


def _live_split_event(name: str):
    if name and " @ " in name:
        a, h = name.split(" @ ", 1)
        return a.strip(), h.strip()
    return None, None


def _live_match_espn(events: list, away: str, home: str, event_start_iso: str):
    """Match a bot_pick's game to an ESPN scoreboard entry. Returns a score
    dict {state, away_score, home_score, period, clock, display_status,
    inn1_away, inn1_home} or None. Team substring + ±90 min, same pattern
    as the resolver / _merge_espn_scores."""
    if not (away and home):
        return None
    an, hn = away.lower(), home.lower()
    bet_dt = _parse_iso(event_start_iso) if event_start_iso else None
    for g in events:
        comp = (g.get("competitions") or [{}])[0]
        cs = comp.get("competitors") or []
        if len(cs) != 2:
            continue
        h = next((c for c in cs if c.get("homeAway") == "home"), cs[0])
        a = next((c for c in cs if c.get("homeAway") == "away"), cs[1])
        h_name = ((h.get("team") or {}).get("displayName") or "").lower()
        a_name = ((a.get("team") or {}).get("displayName") or "").lower()
        if not h_name or not a_name:
            continue
        if not ((hn in h_name or h_name in hn) and (an in a_name or a_name in an)):
            continue
        comp_dt = _parse_iso(comp.get("date") or g.get("date") or "")
        if bet_dt and comp_dt and abs((bet_dt - comp_dt).total_seconds()) > 90 * 60:
            continue
        status = comp.get("status") or {}
        type_ = status.get("type") or {}

        def _int(v):
            try:
                return int(v)
            except (TypeError, ValueError):
                return None

        def _inn1(c):
            ls = c.get("linescores") or []
            if not ls:
                return None
            return _int((ls[0] or {}).get("value"))

        return {
            "state":          type_.get("state", ""),
            "display_status": type_.get("shortDetail") or type_.get("description") or "",
            "period":         status.get("period"),
            "clock":          status.get("displayClock") or "",
            "away_score":     _int(a.get("score")),
            "home_score":     _int(h.get("score")),
            "inn1_away":      _inn1(a),
            "inn1_home":      _inn1(h),
        }
    return None


def _live_decided_prob(bet: dict, m: dict):
    """Win prob (1.0/0.0/0.5) when the bet is already decided by the current
    game state — final score, or NRFI/YRFI once the 1st inning is complete.
    Returns None when it's not yet decided (caller falls back to the live
    market price)."""
    mt, side = bet.get("market_type"), bet.get("side")
    a, h = m.get("away_score"), m.get("home_score")
    line = bet.get("entry_line")
    final = m.get("state") == "post"

    if mt == "nrfi":
        a1, h1 = m.get("inn1_away"), m.get("inn1_home")
        if a1 is None and h1 is None:
            return None
        ran = (a1 or 0) > 0 or (h1 or 0) > 0
        if not ran and not (final or (m.get("period") or 0) >= 2):
            return None    # 1st inning not complete yet
        yrfi = ran
        won = (side == "yes" and yrfi) or (side == "no" and not yrfi)
        return 1.0 if won else 0.0

    if not final or a is None or h is None:
        return None
    if mt == "moneyline":
        if a == h:
            return 0.5
        winner = "home" if h > a else "away"
        return 1.0 if side == winner else 0.0
    if mt == "total" and line is not None:
        tot = a + h
        if tot == float(line):
            return 0.5
        over = tot > float(line)
        return 1.0 if (side == "over") == over else 0.0
    if mt == "spread" and line is not None:
        margin = (h - a) + float(line) if side == "home" else (a - h) + float(line)
        if margin == 0:
            return 0.5
        return 1.0 if margin > 0 else 0.0
    return None


_PMM_LIVE_KEY = {"moneyline": "ml", "spread": "spread", "total": "total", "nrfi": "nrfi"}


_LIVE_BOOK_TTL = 20  # seconds — fresh enough for a live ring; bounds book reads
_LIVE_BOOK_CACHE: dict[tuple, tuple[float, float]] = {}   # (slug,inv) -> (ts, mid)


def _live_book_mid(client, slug: str, synthetic: bool):
    """Fresh live implied-prob (mid) for ONE PMM market via a single
    lightweight order-book read (_pmm_book) — NOT the heavy event lookup.
    This is the trick that lets the live ring refresh every 30s without the
    cost of re-running event discovery: the slug comes from the 5-min-cached
    lookup, but the QUOTE is read live here. Cached ~20s per (slug, side) so a
    30s poll re-reads fresh while a faster poll can't run away. 0<mid<1 or None."""
    if not slug or client is None:
        return None
    import time
    key = (slug, bool(synthetic))
    now = time.time()
    hit = _LIVE_BOOK_CACHE.get(key)
    if hit and (now - hit[0]) < _LIVE_BOOK_TTL:
        return hit[1]
    try:
        book = _pmm_book(client, slug)
        if synthetic:
            book = _invert_book(book)
    except Exception:
        book = None
    if not book or book.get("best_bid") is None or book.get("best_ask") is None:
        return None
    mid = (book["best_bid"] + book["best_ask"]) / 200.0      # cents → prob, mid
    if not (0.0 < mid < 1.0):
        return None
    mid = round(mid, 4)
    _LIVE_BOOK_CACHE[key] = (now, mid)
    return mid


def _live_market_prob(bet: dict, away: str, home: str, client, pmm_cache: dict):
    """Current Polymarket implied probability for the bet's side (the live
    'odds of winning'), or None when PMM has no live market for it. The heavy
    event lookup (which market is this?) is 5-min cached; the live QUOTE is a
    fresh ~30s order-book read via _live_book_mid, so the ring tracks the live
    price without re-running discovery every tick."""
    mid = bet.get("market_id")
    if mid not in pmm_cache:
        try:
            import pmm_markets
            pmm_cache[mid] = pmm_markets.lookup(
                client, bet.get("sport") or "", away, home,
                bet.get("event_start") or "")
        except Exception:
            pmm_cache[mid] = None
    pmm = pmm_cache.get(mid)
    if not pmm:
        return None
    key = _PMM_LIVE_KEY.get(bet.get("market_type"))
    if not key:
        return None
    try:
        import pmm_markets
        entry = pmm_markets.best_line_for(pmm, key, bet.get("side"), bet.get("entry_line"))
    except Exception:
        entry = None
    # Fresh live mid from a single lightweight book read on the discovered slug.
    if entry and entry.get("slug"):
        fresh = _live_book_mid(client, entry["slug"], bool(entry.get("synthetic")))
        if fresh is not None:
            return fresh
    # Fallback: the (up to 5-min) cached quote mid from the lookup payload.
    q = (entry or {}).get("quote") or {}
    mid_prob = q.get("mid")
    return round(mid_prob, 4) if isinstance(mid_prob, (int, float)) else None


def _live_wc_match(wc_matches, bet_away, bet_home):
    """Find a World Cup fixture in _build_worldcup's output for the bet's
    canonical (away @ home) pair. Returns the live ESPN score mapped to the
    BET's orientation + the live PMM 1-X-2 `results`, or None. (The standard
    ESPN/_ESPN_PATH live path has no soccer; this is the soccer equivalent.)"""
    import pmm_markets as _pm
    ak, hk = _wc_country_key(bet_away), _wc_country_key(bet_home)
    for m in (wc_matches or []):
        t1, t2 = _pm._wc_teams_from_title(m.get("title") or "")
        if not (t1 and t2):
            continue
        k1, k2 = _wc_country_key(t1), _wc_country_key(t2)
        if {k1, k2} != {ak, hk}:
            continue
        s1, s2 = m.get("away_score"), m.get("home_score")   # t1→s1, t2→s2
        return {
            "state":          m.get("state") or "",
            "display_status": m.get("detail") or "",
            "away_score":     s1 if k1 == ak else s2,
            "home_score":     s1 if k1 == hk else s2,
            "results":        m.get("results") or [],
        }
    return None


def _live_wc_prob(wcm, bet_away, bet_home, side):
    """Live win prob for a World Cup bet. Decided from the score once final
    (3-way: 'draw' wins on a level score; home/away ML LOSES on a draw); else
    the live PMM 1-X-2 implied prob for the side."""
    a, h = wcm.get("away_score"), wcm.get("home_score")
    if wcm.get("state") == "post" and a is not None and h is not None:
        if side == "draw":
            return 1.0 if a == h else 0.0
        if side == "home":
            return 1.0 if h > a else 0.0
        if side == "away":
            return 1.0 if a > h else 0.0
    ak, hk = _wc_country_key(bet_away), _wc_country_key(bet_home)
    for r in (wcm.get("results") or []):
        prob = r.get("prob")
        if prob is None:
            continue
        lbl = (r.get("label") or "")
        lk = _wc_country_key(lbl)
        if (side == "draw" and "draw" in lbl.lower()) \
           or (side == "home" and lk == hk) \
           or (side == "away" and lk == ak):
            return round(min(max(float(prob), 0.0), 1.0), 4)
    return None


@app.route("/api/handicapper/live")
@bot_required   # PER-USER: the CALLER'S OWN in-progress bets (asked_by==g.uid)
def api_handicapper_live():
    """Live tracker: the caller's OWN PENDING bets whose game is in progress,
    enriched with the ESPN live score + a current win probability (live
    Polymarket price, or a deterministic 1/0 once the bet is decided). Powers
    the small 'Live' scoreboard section on /handicapper. Polled ~30s.
    Scoped to asked_by==g.uid so each user only sees their own live book."""
    sb = get_supabase()
    if sb is None:
        return jsonify({"ok": False, "error": "Supabase not configured"}), 503
    try:
        pending = (sb.table("bot_picks")
                   .select("id,market_id,sport,event_name,event_start,"
                           "market_type,side,units,entry_price,entry_line")
                   .eq("status", "pending")
                   .eq("asked_by", g.uid)
                   .order("event_start", desc=False)
                   .limit(200).execute().data) or []
    except Exception as e:
        return jsonify({"ok": False, "error": f"pending fetch: {e}"}), 500

    now = datetime.now(timezone.utc)
    espn_cache: dict = {}
    pmm_cache: dict = {}
    wc_matches = None          # lazy — only built if a WORLDCUP bet exists
    client = None
    out = []
    for bet in pending:
        away, home = _live_split_event(bet.get("event_name") or "")
        sp = (bet.get("sport") or "").lower()
        # Resilient inclusion. ESPN lags flipping a game to "in" (or its first
        # pitch differs from our stored start by >90min, so the match misses) —
        # a game the user KNOWS is live would then vanish. So also include any
        # bet whose event_start has simply PASSED (0–5h ago). 5h cap keeps
        # long-finished (ungraded) bets out.
        bdt = _parse_iso(bet.get("event_start") or "")
        started = bool(bdt) and timedelta(0) <= (now - bdt) <= timedelta(hours=5)

        # World Cup / soccer: the standard pmm_markets.lookup has NO WC tag and
        # NO 'draw' side, so it can't price soccer (the grey-ring bug). Use the
        # World Cup reader (_build_worldcup) for BOTH the live ESPN score AND
        # the live PMM 1-X-2 odds. Polymarket has live soccer markets.
        if sp == "worldcup":
            if wc_matches is None:
                try:
                    wc_matches, _ = _build_worldcup(now)
                except Exception:
                    wc_matches = []
            m = _live_wc_match(wc_matches, away, home)
            matched_live = bool(m) and m.get("state") in ("in", "live", "post")
            if not (matched_live or started):
                continue
            win_prob = _live_wc_prob(m, away, home, bet.get("side")) if m else None
            prob_src = ("decided" if (m and m.get("state") == "post"
                                      and win_prob is not None)
                        else ("market" if win_prob is not None else None))
            out.append({
                "id": bet["id"], "market_id": bet["market_id"],
                "event_name": bet["event_name"], "away": away, "home": home,
                "market_type": bet["market_type"], "side": bet["side"],
                "units": bet["units"], "entry_price": bet["entry_price"],
                "entry_line": bet.get("entry_line"),
                "score": m or {}, "win_prob": win_prob, "prob_src": prob_src,
            })
            continue

        # ESPN score — fetch the game's SPECIFIC DATE (ET), like the resolver.
        # ESPN's no-date scoreboard default is flaky (it drops games as the day
        # rolls / once they finish), which blanked the live score while the PMM
        # ring still rendered. Date-keyed fetch is reliable.
        pair = _ESPN_PATH.get(sp)
        events = []
        if pair:
            grp, league = pair
            dk = None
            if bdt:
                try:
                    dk = bdt.astimezone(ZoneInfo("America/New_York")).strftime("%Y%m%d")
                except Exception:
                    dk = None
            ckey = (sp, dk)
            if ckey not in espn_cache:
                espn_cache[ckey] = (_espn_scoreboard_raw(grp, league, dates=dk)
                                    if dk else _fetch_espn_scoreboard(sp))
            events = espn_cache[ckey]
        m = _live_match_espn(events, away, home, bet.get("event_start"))
        matched_live = bool(m) and m.get("state") in ("in", "live", "post")
        if not (matched_live or started):
            continue

        # Win prob: decided (needs a real ESPN match) → live Polymarket price
        # → grey. Odds-only (works for every sport, no per-sport model).
        win_prob, prob_src = None, None
        if m:
            win_prob = _live_decided_prob(bet, m)
            if win_prob is not None:
                prob_src = "decided"
        if win_prob is None:
            if client is None:
                try:
                    client = get_client()
                except Exception:
                    client = None
            if client is not None:
                win_prob = _live_market_prob(bet, away, home, client, pmm_cache)
                if win_prob is not None:
                    prob_src = "market"

        out.append({
            "id":            bet["id"],
            "market_id":     bet["market_id"],
            "event_name":    bet["event_name"],
            "away":          away,
            "home":          home,
            "market_type":   bet["market_type"],
            "side":          bet["side"],
            "units":         bet["units"],
            "entry_price":   bet["entry_price"],
            "entry_line":    bet.get("entry_line"),
            "score":         m or {},
            "win_prob":      win_prob,
            "prob_src":      prob_src,   # decided | market | None(grey)
        })
    return jsonify({"ok": True, "live": out})


@app.route("/api/handicapper/dossier")
@bot_required
def api_handicapper_dossier():
    """Build the live pre-game dossier for one game.

    Auth: any approved user (viewers included). The dossier shows the
    bot's read on a game — picks, fair lines, splits, injuries. It
    contains NO logged-pick data (no pending/settled rows leak through
    here), so viewers seeing this can't see what's actually been
    logged or by whom. Only `/api/handicapper` and the pick-mutation
    endpoints stay `@bot_required` for that reason.

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
    # Pick Bot doesn't auto-log dossier views anymore. The 'recommended'
    # row creation existed solely to give PMM sync a hook to link real
    # bets to bot suggestions — that whole integration is gone, and
    # leaving the auto-log here actively breaks the real Log Pick flow:
    # opening the dossier wrote a hidden recommended row, then explicit
    # Log Pick clicks dedup'd against it and silently skipped. Picks
    # exist if and only if the user clicks Log on the website.
    code = 200 if dossier.get("ok") else 404
    return jsonify(dossier), code


# Per-sport DISPLAY window for the Pick Bot games list (calendar days,
# anchored to America/Phoenix). User decision (June 2026): show a tight
# "today and tomorrow" (2 days) for the daily/event sports and the full
# week for football (its schedule is weekly, so a 2-day window would show
# almost nothing). NOTE: this is DISPLAY only — we still INGEST games into
# `markets`/`pm_snapshots` as soon as they appear on Polymarket/ESPN so we
# capture opening odds for the Pinnacle/Odds-API cutover; they just don't
# render on the list until they enter this window.
# UFC is CARD-based, not daily — one Saturday card, sometimes with a skipped
# week between cards, so a 2-day window left the tab empty for most of every
# week (the July 2026 "nothing coming up for UFC" report: card 8 days out,
# tab blank). 9 days = the next card is visible the morning after the last
# one ends, even across a skipped week + late-night AZ main-event times.
_GAMES_DISPLAY_DAYS = {"NFL": 7, "NCAAF": 7, "UFC": 9}
_GAMES_DISPLAY_DAYS_DEFAULT = 2  # MLB, NBA, NCAAB, NHL, WORLDCUP/soccer


def _display_window_end(sport: str, now: datetime) -> datetime:
    """UTC cutoff for the games-list display: the END of the last included
    Arizona calendar day. N = the sport's display-day count, so N=2 means
    'today and tomorrow' (through end of tomorrow AZ), N=7 means the next
    seven calendar days. Anchored to America/Phoenix (no DST) like the rest
    of Pick Bot's day math."""
    days = _GAMES_DISPLAY_DAYS.get((sport or "").upper(), _GAMES_DISPLAY_DAYS_DEFAULT)
    az = ZoneInfo("America/Phoenix")
    end_local = (now.astimezone(az) + timedelta(days=days)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    return end_local.astimezone(timezone.utc)


# UFC fights run on imprecise block-times and a card spans hours, so a fight
# ESPN still flags `pre` routinely has a nominal event_start already in the
# PAST — the plain `event_start > now` filter used for every other sport then
# wrongly hides it, emptying the UFC tab mid-card (the "what did you do to UFC"
# regression). For UFC we widen the lower bound and let ESPN's live per-fight
# state decide what's still pickable. Lookback = a full card's run.
_UFC_LIVE_LOOKBACK_H = 8


def _ufc_norm(s: str) -> str:
    """Fighter-name normalization — mirrors bot_picks_resolver._norm."""
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def _ufc_espn_fight_states(now: datetime) -> list[dict]:
    """Every UFC bout ESPN currently knows, as {n1, n2, state}. Iterates
    events × competitions so it handles both ESPN MMA shapes (flat
    one-event-per-fight AND card-with-many-competitions). state ∈ pre/in/post.
    30s-cached via _espn_scoreboard_raw; [] on fetch error."""
    dates = f"{now:%Y%m%d}-{(now + timedelta(days=8)):%Y%m%d}"
    fights: list[dict] = []
    for ev in _espn_scoreboard_raw("mma", "ufc", dates=dates):
        for comp in (ev.get("competitions") or []):
            cs = comp.get("competitors") or []
            if len(cs) != 2:
                continue
            def _nm(c):
                return _ufc_norm((c.get("athlete") or {}).get("displayName")
                                 or (c.get("team") or {}).get("displayName") or "")
            n1, n2 = _nm(cs[0]), _nm(cs[1])
            if not (n1 and n2):
                continue
            state = ((comp.get("status") or {}).get("type") or {}).get("state") or ""
            fights.append({"n1": n1, "n2": n2, "state": state})
    return fights


def _ufc_row_state(away: str, home: str, fights: list[dict]) -> str | None:
    """ESPN live state for one market row's fight, matched by fighter name in
    either orientation (UFC home/away is arbitrary) with a last-name-token
    fallback. Mirrors bot_picks_resolver._ufc_match_espn's matching. None when
    no ESPN bout matches (e.g. ESPN unreachable)."""
    a, h = _ufc_norm(away), _ufc_norm(home)
    if not (a and h):
        return None
    a_tok = [t for t in a.split() if len(t) >= 3]
    h_tok = [t for t in h.split() if len(t) >= 3]

    def hit(x: str, n: str, tok: list[str]) -> bool:
        return (x in n or n in x) or any(t in n for t in tok)

    for f in fights:
        n1, n2 = f["n1"], f["n2"]
        if (hit(a, n1, a_tok) and hit(h, n2, h_tok)) or \
           (hit(a, n2, a_tok) and hit(h, n1, h_tok)):
            return f["state"]
    return None


def _ufc_pickable_market_rows(sb, now: datetime) -> list[dict]:
    """Active UFC `markets` rows still PICKABLE, by ESPN live state. A row is
    kept when its nominal start is still in the future, OR when ESPN still
    flags that fight `pre`. Fail-safe: if ESPN is unreachable (no fight
    states), past-nominal rows are dropped — degrades to the old future-only
    behavior rather than ever showing finished fights. Shared by the games
    list + sport-counts so the badge matches the rendered list."""
    before = _display_window_end("UFC", now).isoformat()
    after = (now - timedelta(hours=_UFC_LIVE_LOOKBACK_H)).isoformat()
    try:
        rows = (sb.table("markets")
                .select("id,sport,event_name,event_start,status")
                .eq("status", "active").eq("sport", "UFC")
                .gte("event_start", after).lte("event_start", before)
                .order("event_start").limit(200).execute().data) or []
    except Exception:
        return []
    fights = _ufc_espn_fight_states(now)
    out: list[dict] = []
    for m in rows:
        sdt = _parse_iso(m.get("event_start") or "")
        if sdt and sdt > now:
            out.append(m)
            continue
        en = m.get("event_name") or ""
        away, home = ("", "")
        if " @ " in en:
            away, home = [p.strip() for p in en.split(" @ ", 1)]
        if _ufc_row_state(away, home, fights) == "pre":
            out.append(m)
    return out


def _wc_in_display_window(matches: list[dict], now: datetime) -> list[dict]:
    """Trim World Cup matches to the same 2-day display window the other
    sports use. Keeps anything that has already kicked off (live/today) and
    drops far-future fixtures; matches without a parseable start_time are
    kept (rare, never silently dropped)."""
    end = _display_window_end("WORLDCUP", now)
    out = []
    for m in matches:
        st = m.get("start_time")
        try:
            sdt = datetime.fromisoformat((st or "").replace("Z", "+00:00")) if st else None
        except Exception:
            sdt = None
        if sdt is None or sdt <= end:
            out.append(m)
    return out


@app.route("/api/handicapper/games")
@bot_required
def api_handicapper_games():
    """List active games for a sport — pre-game window only. Powers the
    click-to-pick UI on /handicapper.

    Auth: any approved user (viewers included). The list is just
    upcoming game metadata; no logged-pick data is exposed.

    Window: per-sport display window (see _display_window_end) — 2 calendar
    days ("today and tomorrow") for MLB/NBA/NCAAB/NHL/UFC/soccer, 7 days for
    football. Live/done games (event_start already passed) are excluded —
    you can't make a pre-game pick on a game that's underway. Sorted by
    event_start.

    Lightweight: only event_name + start time + ids. The dossier (with
    odds, splits, injuries) is fetched on click via /api/handicapper/dossier."""
    sb = get_supabase()
    if sb is None:
        return jsonify({"ok": False, "error": "Supabase not configured"}), 503
    sport = (request.args.get("sport") or "").upper().strip()
    if not sport:
        return jsonify({"ok": False, "error": "missing sport param"}), 400

    now = datetime.now(timezone.utc)
    after  = now.isoformat()
    # Per-sport display window (see _display_window_end): 2 calendar days
    # ("today and tomorrow") for MLB/NBA/NCAAB/NHL/UFC/soccer, 7 days for
    # football. Display is decoupled from the pick/trigger windows AND from
    # ingest — we still capture opening odds for games further out, they
    # just don't render until they enter this window.
    before = _display_window_end(sport, now).isoformat()
    if sport == "UFC":
        # UFC nominal starts are block-times (a fight ESPN still calls `pre`
        # often has a past nominal start); use ESPN live-state to decide what's
        # still pickable instead of the strict future filter (see helper).
        rows = _ufc_pickable_market_rows(sb, now)
    else:
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

    # Phantom-market filter removed. It was joining book_snapshots
    # rows for last 24h and dropping markets with no snapshot row in
    # that set — but the in_(market_ids) query had a hard 5000-row
    # cap, which truncated on busy sports and silently disappeared
    # real games from the list. The event_start window already
    # excludes anything that's already started, so phantoms (markets
    # nobody ever quoted) are rare. Clicking one just yields a
    # "no data" dossier, not a crash.

    games = []
    for m in rows:
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

    # Dedup duplicate market rows for the same game. Cause: The Odds
    # API sometimes reports a game's commence_time with several hours
    # of drift between calls (initial placeholder time vs corrected
    # tip-off). The ingest matcher's ±30 min window misses that drift
    # and creates a NEW markets row, so the same Cavaliers @ Pistons
    # game shows up twice in the list.
    #
    # Strategy: group by event_name. Within a group, the row with the
    # LATER event_start is more likely the corrected one (the Odds API
    # usually pushes a placeholder time forward to the real tip, not
    # the other way around). For MLB, keep both when they're >1h apart
    # — that's the doubleheader case (real, same teams same day).
    games = _dedup_games(games, sport)
    # Prime ZONES (minute-bands the bot sizes up + glows green). Multi-zone
    # since June 2026 (the edge is bimodal) AND per-bet-type — the row glow
    # is per-GAME (one kickoff time), so it uses the UNION of every market's
    # zones (lights when the game is prime for ANY bet type). The precise
    # per-bet-type sizing/badge is resolved server-side in _suggest_picks.
    try:
        import handicapper_web
        prime_zones = [list(z) for z in handicapper_web._prime_zones_union(sb)]
        # Per-side-market zones (ML/SPR/TOT) so the row badge can NAME which
        # markets are prime once they specialize; while pooled they're equal
        # and the frontend renders a bare "PRIME".
        prime_zones_by_market = handicapper_web._prime_zones_by_market_resolved(sb)
    except Exception:
        prime_zones = [[60, 180]]
        prime_zones_by_market = {}
    return jsonify({
        "ok":          True,
        "sport":       sport,
        "now_iso":     now.isoformat(),
        "count":       len(games),
        "games":       games,
        "prime_zones": prime_zones,
        "prime_zones_by_market": prime_zones_by_market,
    })


def _dedup_games(games: list[dict], sport: str) -> list[dict]:
    """Collapse duplicate market rows for the same event_name. Returns
    a new list sorted by event_start asc.

    MLB doubleheaders are real (same teams same day, scheduled hours
    apart). Anything else with the same event_name within a 6h window
    is treated as a duplicate from Odds-API commence_time drift.
    """
    if not games:
        return games
    # Group by event_name (which already encodes away @ home).
    grouped: dict[str, list[dict]] = {}
    for g in games:
        grouped.setdefault(g["event_name"], []).append(g)

    out: list[dict] = []
    is_mlb = sport.upper() == "MLB"
    DUPE_WINDOW = timedelta(hours=1 if is_mlb else 6)
    for name, gs in grouped.items():
        if len(gs) == 1:
            out.append(gs[0])
            continue
        # Sort by event_start asc, then walk and cluster anything within
        # the dupe window. Each cluster collapses to its latest entry.
        gs_sorted = sorted(
            gs, key=lambda g: g["event_start"] or ""
        )
        clusters: list[list[dict]] = []
        for g in gs_sorted:
            try:
                t = datetime.fromisoformat(
                    (g["event_start"] or "").replace("Z", "+00:00")
                )
            except Exception:
                # Unparseable timestamp — treat as its own cluster so
                # we never silently drop it.
                clusters.append([g])
                continue
            if clusters:
                last_g = clusters[-1][-1]
                try:
                    last_t = datetime.fromisoformat(
                        (last_g["event_start"] or "").replace("Z", "+00:00")
                    )
                except Exception:
                    clusters.append([g])
                    continue
                if abs(t - last_t) <= DUPE_WINDOW:
                    clusters[-1].append(g)
                    continue
            clusters.append([g])
        # Pick latest event_start within each cluster.
        for c in clusters:
            winner = max(c, key=lambda g: g["event_start"] or "")
            out.append(winner)

    out.sort(key=lambda g: g["event_start"] or "")
    return out


@app.route("/api/handicapper/sport-counts")
@bot_required
def api_handicapper_sport_counts():
    """One-shot count of upcoming games per sport. Powers the
    /handicapper page's dynamic sport-tab ordering — sports with the
    most games go left, off-season sports drop to the right. Same
    per-sport display window as /api/handicapper/games (2 days for most
    sports, 7 for football; live/done games excluded).

    Returns: {ok: True, counts: {MLB: 15, NBA: 0, ...}}
    """
    sb = get_supabase()
    if sb is None:
        return jsonify({"ok": False, "error": "Supabase not configured"}), 503
    now = datetime.now(timezone.utc)
    after  = now.isoformat()
    # Fetch the widest window any sport uses (UFC = 9 days), then trim
    # each sport to its own display window below so the badge count matches
    # what /api/handicapper/games actually renders.
    _widest_days = max([_GAMES_DISPLAY_DAYS_DEFAULT, *_GAMES_DISPLAY_DAYS.values()])
    before = (now + timedelta(days=_widest_days + 1)).isoformat()
    try:
        rows = (sb.table("markets")
                .select("id,sport,event_name,event_start")
                .eq("status", "active")
                .gte("event_start", after)
                .lte("event_start", before)
                .limit(4000).execute().data) or []
    except Exception as e:
        return jsonify({"ok": False, "error": f"Supabase: {e}"}), 500

    # Group rows by sport, dropping anything past that sport's display
    # window, then run the same dedup as /api/handicapper/games so the count
    # matches what the user actually sees on the page.
    _win_end: dict[str, str] = {}
    by_sport: dict[str, list[dict]] = {}
    for r in rows:
        s = (r.get("sport") or "").upper()
        if not s:
            continue
        end_iso = _win_end.get(s)
        if end_iso is None:
            end_iso = _win_end[s] = _display_window_end(s, now).isoformat()
        if (r.get("event_start") or "") > end_iso:
            continue
        by_sport.setdefault(s, []).append({
            "market_id":   r["id"],
            "event_name":  r.get("event_name") or "",
            "event_start": r.get("event_start"),
            "sport":       s,
        })
    # UFC: the all-sports query above filters `event_start > now`, which hides
    # block-time fights ESPN still flags `pre` (see _ufc_pickable_market_rows).
    # Rebuild the UFC bucket from ESPN live-state so the tab badge matches the
    # games list exactly.
    try:
        ufc_rows = _ufc_pickable_market_rows(sb, now)
        by_sport["UFC"] = [{
            "market_id":   m["id"],
            "event_name":  m.get("event_name") or "",
            "event_start": m.get("event_start"),
            "sport":       "UFC",
        } for m in ufc_rows]
        if not by_sport["UFC"]:
            by_sport.pop("UFC", None)
    except Exception:
        pass
    counts: dict[str, int] = {
        s: len(_dedup_games(gs, s)) for s, gs in by_sport.items()
    }
    # World Cup gets a count badge + count-based ordering like every other
    # sport (it's not in the markets table — sourced from _build_worldcup =
    # ESPN fifa.world + PMM, so this equals what /api/handicapper/worldcup
    # shows). Cache-backed; silent-skip on failure.
    try:
        wc_matches, _wc_meta = _build_worldcup(now)
        wc_matches = _wc_in_display_window(wc_matches, now)
        if wc_matches:
            counts["WORLDCUP"] = len(wc_matches)
    except Exception:
        pass
    return jsonify({"ok": True, "counts": counts, "now_iso": now.isoformat()})


def _build_worldcup(now=None):
    """Shared World Cup builder. ESPN (soccer/fifa.world) is the SCHEDULE +
    LIVE-SCORE spine; Polymarket supplies the 1-X-2 match-result odds,
    matched onto ESPN events by canonical country name. Upcoming matches
    PMM lists but ESPN's window doesn't yet cover are appended (odds only,
    no score) so coverage never regresses below the PMM-only version.
    Returns (matches, meta) — meta carries counts for diagnostics."""
    now = now or datetime.now(timezone.utc)
    # ESPN spine — a date range so we get the upcoming slate, not just today.
    dates = f"{now:%Y%m%d}-{(now + timedelta(days=8)):%Y%m%d}"
    espn_events = _espn_scoreboard_raw("soccer", "fifa.world", dates=dates)
    espn_matches = [m for m in (_espn_soccer_match(e) for e in espn_events)
                    if m and m.get("away") and m.get("home")]

    # Polymarket odds (1-X-2), keyed by canonical country-name pair.
    import pmm_markets as _pm
    try:
        pmm = _pm.list_world_cup(get_client()) or {"matches": []}
    except Exception:
        pmm = {"matches": []}
    pmm_idx = {}
    for pm in pmm.get("matches", []):
        t1, t2 = _pm._wc_teams_from_title(pm.get("title") or "")
        if t1 and t2:
            pmm_idx[frozenset({_wc_country_key(t1), _wc_country_key(t2)})] = pm

    out, used = [], set()
    for m in espn_matches:
        k = frozenset({_wc_country_key(m["away"]), _wc_country_key(m["home"])})
        pm = pmm_idx.get(k)
        if pm:
            used.add(k)
        out.append({
            "title":      f"{m['away']} vs. {m['home']}",
            "start_time": m["date"],
            "state":      m["state"], "detail": m["detail"],
            "away_score": m["away_score"], "home_score": m["home_score"],
            "results":    (pm or {}).get("results", []),
            "src":        "espn",
        })
    # Append PMM-listed upcoming matches ESPN's window didn't include
    # (odds only). Drop ones that already kicked off >4h ago.
    #
    # ROSTER GATE (July 2026): the PMM World Cup tag also returns CLUB
    # soccer events (UEFA qualifiers — "Sabah FK vs The New Saints" etc.).
    # While the odds parse was dead (gotcha #39's WC casualty) they were
    # invisible (skipped as no_odds); the moment odds parsed again they
    # leaked bogus WORLDCUP markets. ESPN's fifa.world scoreboard is the
    # tournament roster of record: only append a PMM-only fixture when
    # BOTH its country keys appear somewhere in ESPN's current window
    # (teams still alive in the tournament are always in it — they have
    # a scheduled/live match; club teams never are).
    roster = set()
    for m in espn_matches:
        roster.add(_wc_country_key(m["away"]))
        roster.add(_wc_country_key(m["home"]))
    drop_before = now - timedelta(hours=4)
    for k, pm in pmm_idx.items():
        if k in used:
            continue
        if not roster or not k.issubset(roster):
            # No roster (ESPN outage) → no appends at all: bogus markets
            # are worse than a briefly thinner list.
            continue
        st = pm.get("start_time")
        try:
            sdt = datetime.fromisoformat((st or "").replace("Z", "+00:00")) if st else None
        except Exception:
            sdt = None
        if sdt is not None and sdt < drop_before:
            continue
        out.append({
            "title":      pm.get("title"),
            "start_time": st,
            "state":      None, "detail": "",
            "away_score": None, "home_score": None,
            "results":    pm.get("results", []),
            "src":        "pmm",
        })
    out.sort(key=lambda x: x.get("start_time") or "")
    meta = {"espn_count": len(espn_matches),
            "pmm_count": len(pmm.get("matches", [])),
            "matched": len(used)}
    return out, meta


@app.route("/api/handicapper/worldcup")
@bot_required
def api_handicapper_worldcup():
    """Read-only World Cup list for the Pick Bot page. ESPN is the
    schedule + live-score spine (soccer/fifa.world); Polymarket supplies
    the 1-X-2 odds matched on. Nothing logged or graded. View-only."""
    now = datetime.now(timezone.utc)
    matches, meta = _build_worldcup(now)
    matches = _wc_in_display_window(matches, now)
    _wc_attach_market_ids(matches)        # so the page can open the dossier + log
    return jsonify({"ok": True, "fetched_iso": now.isoformat(),
                    "matches": matches, **meta})


def _wc_attach_market_ids(matches):
    """Attach `market_id` (the WORLDCUP markets row created by the cent
    logger) to each match by canonical country-pair key, so the page can open
    the 3-way dossier + log picks. Silent-fail (market_id stays absent)."""
    try:
        import pmm_markets as _pm
        sb = get_supabase()
        rows = (sb.table("markets").select("id,event_name")
                .eq("sport", "WORLDCUP").eq("status", "active")
                .execute().data) or []
        idx = {}
        for r in rows:
            en = r.get("event_name") or ""
            if " @ " in en:
                a, h = en.split(" @ ", 1)
                idx[frozenset({_wc_country_key(a), _wc_country_key(h)})] = r["id"]
        for mt in matches:
            t1, t2 = _pm._wc_teams_from_title(mt.get("title") or "")
            if t1 and t2:
                mt["market_id"] = idx.get(
                    frozenset({_wc_country_key(t1), _wc_country_key(t2)}))
    except Exception:
        pass


@app.route("/api/handicapper/worldcup-dossier")
@bot_required
def api_handicapper_worldcup_dossier():
    """3-way (1-X-2) dossier for one World Cup fixture — exchange devig fair +
    cent-movement sharp read + ESPN form/record research + a suggestion + PMM
    maker entry per side. @bot_required (the bot's READ, like /dossier).
    market_id comes from /api/handicapper/worldcup."""
    import handicapper_web
    sb = get_supabase()
    if sb is None:
        return jsonify({"ok": False, "error": "supabase unavailable"}), 503
    mid = (request.args.get("market_id") or "").strip()
    if not mid:
        return jsonify({"ok": False, "error": "market_id required"}), 400
    return jsonify(handicapper_web.build_worldcup_dossier(sb, mid))


def _wc_ensure_market(sb, away, home, start_iso):
    """Select-or-insert a WORLDCUP markets row for one fixture. event_name is
    canonical ('{away} @ {home}', away/home decided by sorted country key) so
    the same match maps to ONE stable market_id no matter which feed (ESPN vs
    PMM) named it first — no duplicate market rows across ticks. WORLDCUP is
    excluded from the ESPN-markets ingest, so this is the sole creator of WC
    market rows. Returns market_id or None (silent-fail). markets.id is
    gen_random_uuid() so we don't pass one."""
    ename = f"{away} @ {home}"
    try:
        ex = (sb.table("markets").select("id")
              .eq("sport", "WORLDCUP").eq("event_name", ename)
              .limit(1).execute().data) or []
        if ex:
            return ex[0]["id"]
        ins = (sb.table("markets").insert(
            {"sport": "WORLDCUP", "event_name": ename,
             "event_start": start_iso, "status": "active"}).execute().data) or []
        return ins[0]["id"] if ins else None
    except Exception:
        return None


@app.route("/api/pm-snapshot-wc")
def api_pm_snapshot_wc():
    """Cron-pinged ~2/min. Logs Polymarket 1-X-2 (home/draw/away) cents for
    UPCOMING World Cup matches into pm_snapshots — the 3-way exchange-history
    clock the soccer sharp score will read (same dataset role pm-snapshot
    plays for MLB/NBA/NHL). Built on _build_worldcup (the proven ESPN+PMM
    reader) + _pm_insert_changed (dedup-on-cent-change). market_type='ml'
    with three sides; line=NULL. Kalshi DOES carry the World Cup
    (series KXWCGAME, 3-way: country/country/-TIE) — so it logs as a second
    cross-confirm venue alongside PMM (source='kalshi'), same role it plays
    for MLB/NBA/NHL. Markets spine: each fixture gets a stable
    WORLDCUP markets row via _wc_ensure_market. Live/finished matches are
    skipped (pre-game history only). Auth: ?key= matched to PM_SNAPSHOT_SECRET
    or FILLS_CRON_SECRET (NOT Firebase)."""
    import pmm_markets as _pm
    expected = (os.environ.get("PM_SNAPSHOT_SECRET")
                or os.environ.get("FILLS_CRON_SECRET") or "").strip()
    provided = (request.args.get("key") or "").strip()
    if not expected or not secrets.compare_digest(provided, expected):
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    sb = get_supabase()
    if sb is None:
        return jsonify({"ok": False, "error": "supabase unavailable"}), 503

    now = datetime.now(timezone.utc)
    matches, meta = _build_worldcup(now)

    # Kalshi 1-X-2 confirm feed (KXWCGAME) — one bulk call, indexed by
    # country pair. Soccer IS a cross-confirm sport now: Kalshi carries the
    # World Cup, so it logs alongside PMM (same role it plays for MLB/NBA/NHL).
    kalshi_wc = []
    try:
        kres = _fetch_kalshi_markets("KXWCGAME")
        kalshi_wc = _kalshi_wc_index(kres.get("markets") or [])
    except Exception:
        kalshi_wc = []

    rows = []
    st = {"matches": 0, "no_odds": 0, "no_start": 0,
          "live_or_done": 0, "unmapped_sides": 0, "kalshi_matched": 0}
    for m in matches:
        results = m.get("results") or []
        if not results:                          # ESPN game with no PMM odds yet
            st["no_odds"] += 1
            continue
        si = m.get("start_time")
        try:
            sdt = datetime.fromisoformat((si or "").replace("Z", "+00:00")) if si else None
        except Exception:
            sdt = None
        if sdt is None:
            st["no_start"] += 1
            continue
        if sdt <= now:                           # pre-game only — no live/finished
            st["live_or_done"] += 1
            continue
        t1, t2 = _pm._wc_teams_from_title(m.get("title") or "")
        if not (t1 and t2):
            continue
        # Canonical orientation: away/home by sorted country key → one fixture
        # is always the same market_id + the same side labels across ticks
        # (WC venues are neutral, so home/away is just a stable label).
        away, home = sorted([t1, t2], key=_wc_country_key)
        ak, hk = _wc_country_key(away), _wc_country_key(home)
        mid = _wc_ensure_market(sb, away, home, sdt.isoformat())
        if not mid:
            continue
        st["matches"] += 1
        for r in results:
            label = (r.get("label") or "")
            prob = r.get("prob")
            try:
                cents = int(round(float(prob) * 100)) if prob is not None else None
            except (TypeError, ValueError):
                cents = None
            if cents is None or cents <= 0 or cents >= 100:   # no/degenerate quote
                continue
            ll = label.lower()
            lk = _wc_country_key(label)
            if "draw" in ll:
                side = "draw"
            elif lk == hk or _pm._name_match(label, home):
                side = "home"
            elif lk == ak or _pm._name_match(label, away):
                side = "away"
            else:
                st["unmapped_sides"] += 1
                continue
            rows.append((mid, "pmm", "ml", side, None, cents))

        # Kalshi confirm row(s) for this fixture (matched by country pair).
        kc = _match_kalshi_wc(kalshi_wc, ak, hk)
        if kc:
            st["kalshi_matched"] += 1
            for side in ("home", "draw", "away"):
                kcents = kc.get(side)
                if kcents is None or kcents <= 0 or kcents >= 100:
                    continue
                rows.append((mid, "kalshi", "ml", side, None, kcents))

    inserted = _pm_insert_changed(sb, rows, now)
    return jsonify({"ok": True, "inserted": inserted, "wc_meta": meta, **st})


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


# Bot market_type → VSiN market_type (NRFI has no VSiN splits). Mirrors
# bot_picks_resolver._VSIN_MT_MAP — keep in sync.
_BETTIME_VSIN_MT = {"moneyline": "ml", "spread": "spread", "total": "total"}


def _bettime_vsin(sb, market_id, market_type):
    """The CURRENT VSiN read (Circa + DraftKings handle%/bets%, both sides)
    for this market at the moment a pick is logged, from vsin_snapshots.
    Same shape as the resolver's closing_vsin ({book: {side: {handle, bets,
    line}}, captured_at}); paired with that closing read it shows whether
    sharp money hit Circa late on the pick's side. Returns None for NRFI /
    sports VSiN doesn't carry / no snapshot. Best-effort — never raises."""
    vmt = _BETTIME_VSIN_MT.get(market_type or "")
    if not (market_id and vmt):
        return None
    try:
        rows = (sb.table("vsin_snapshots")
                .select("book,side,line,handle_pct,bets_pct,captured_at")
                .eq("market_id", market_id).eq("market_type", vmt)
                .order("captured_at", desc=True).limit(200).execute().data) or []
    except Exception:
        return None
    out, seen, last_at = {}, set(), None
    for r in rows:                      # desc → first per (book,side) is the latest
        k = (r["book"], r["side"])
        if k in seen:
            continue
        seen.add(k)
        out.setdefault(r["book"], {})[r["side"]] = {
            "handle": r.get("handle_pct"), "bets": r.get("bets_pct"),
            "line": r.get("line")}
        last_at = last_at or r.get("captured_at")
    if not out:
        return None
    out["captured_at"] = last_at
    return out


@app.route("/api/handicapper/pick", methods=["POST"])
@bot_required   # PER-USER: any bot_access user logs the bot's picks as their OWN bets (row stamped asked_by=g.uid). The dedup below is scoped to the caller so user B can still log a pick user A already logged.
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

    if body["market_type"] not in ("moneyline", "spread", "total", "nrfi"):
        return jsonify({"ok": False, "error": "bad market_type"}), 400
    # NRFI/YRFI uses yes/no sides; everything else uses home/away/over/under.
    if body["market_type"] == "nrfi":
        if body["side"] not in ("yes", "no"):
            return jsonify({"ok": False, "error": "nrfi side must be yes/no"}), 400
    elif body["market_type"] == "moneyline":
        # 'draw' is valid for 3-way (World Cup / soccer) moneyline only.
        if body["side"] not in ("home", "away", "draw"):
            return jsonify({"ok": False, "error": "bad side"}), 400
    elif body["side"] not in ("home", "away", "over", "under"):
        return jsonify({"ok": False, "error": "bad side"}), 400
    if body["confidence"] not in ("low", "medium", "high", "whale"):
        return jsonify({"ok": False, "error": "bad confidence"}), 400
    try:
        units_val = float(body["units"])
    except (TypeError, ValueError):
        units_val = None
    if units_val not in (0.25, 0.5, 1, 3, 5, 10):
        return jsonify({"ok": False, "error": "units must be 0.25/0.5/1/3/5/10"}), 400

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
            # LANDMINE (gotcha #24, multi-user rev): the 7-day dedup is
            # scoped to the CALLER (asked_by==g.uid). Without this, user B
            # couldn't log a pick user A already logged — the global match
            # would silently skip it. Each user has their own dedup window.
            existing = (sb.table("bot_picks").select("id")
                        .eq("market_id", body["market_id"])
                        .eq("market_type", body["market_type"])
                        .eq("side", body["side"])
                        .eq("asked_by", g.uid)
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
        "units":       units_val,
        "confidence":  body["confidence"],
        "fair_prob":   body.get("fair_prob"),
        "edge_pp":     body.get("edge_pp"),
        "sharp_score": body.get("sharp_score"),
        "analysis_md": body.get("analysis_md"),
        "reasons":     body.get("reasons"),
        "signal_blob": body.get("signal_blob"),
    }
    # Stamp the BET-TIME VSiN read (Circa + DK handle/bets, both sides) onto
    # the pick so the % handle-vs-% bets signal is actually tracked per pick.
    # Paired with the resolver's closing_vsin it answers "did sharp money hit
    # Circa late on my side?" Best-effort; a miss never blocks the log.
    try:
        bt_vsin = _bettime_vsin(sb, body["market_id"], body["market_type"])
        if bt_vsin:
            sblob = row.get("signal_blob")
            if not isinstance(sblob, dict):
                sblob = {}
            sblob["vsin"] = bt_vsin
            row["signal_blob"] = sblob
    except Exception:
        pass
    try:
        res = sb.table("bot_picks").insert(row).execute()
    except Exception as e:
        return jsonify({"ok": False, "error": f"insert: {e}"}), 500
    pick_id = (res.data or [{}])[0].get("id")
    return jsonify({"ok": True, "id": pick_id, "skipped": False}), 201


@app.route("/api/handicapper/pick/<int:pick_id>", methods=["DELETE"])
@bot_required   # PER-USER: admin deletes any pick; a bot_access user deletes only their own (asked_by==g.uid). The admin-OR-owner check is enforced inline below.
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


@app.route("/api/handicapper/pick/<int:pick_id>/edit", methods=["POST"])
@bot_required   # admin edits any pick; a bot_access user edits only their own (asked_by==g.uid)
def api_handicapper_pick_edit(pick_id: int):
    """Edit a PENDING pick's units (fix a fat-fingered stake). Body: {units}.
    Confidence is re-derived from units so the tier label/colour stays
    consistent. Auth mirrors DELETE (admin OR owner). Only pending picks —
    a settled pick's pnl is already booked, so editing units there would
    desync the stats; delete + re-log instead."""
    sb = get_supabase()
    if sb is None:
        return jsonify({"ok": False, "error": "Supabase not configured"}), 503

    body = request.get_json(silent=True) or {}
    # Allowed stakes (whale 10u disabled). Mirror the bot_picks CHECK set.
    _ALLOWED_UNITS = {0.25, 0.5, 1.0, 3.0, 5.0}
    try:
        units = float(body.get("units"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "units required"}), 400
    if units not in _ALLOWED_UNITS:
        return jsonify({"ok": False, "error": "units must be 0.25, 0.5, 1, 3 or 5"}), 400

    try:
        row = (sb.table("bot_picks").select("id,asked_by,status")
               .eq("id", pick_id).single().execute().data)
    except Exception:
        row = None
    if not row:
        return jsonify({"ok": False, "error": "pick not found"}), 404

    is_admin = g.user_data.get("role") == "admin"
    if not (is_admin or row.get("asked_by") == g.uid):
        return jsonify({"ok": False, "error": "not your pick"}), 403
    if row.get("status") != "pending":
        return jsonify({"ok": False, "error": "can only edit a pending pick"}), 409

    # Re-derive the confidence tier from units (1u→low, 3u→medium, 5u→high;
    # sub-unit NRFI stakes stay low). Keeps the chip colour honest.
    confidence = "low" if units <= 1.0 else ("medium" if units < 5.0 else "high")
    try:
        sb.table("bot_picks").update({"units": units, "confidence": confidence}) \
            .eq("id", pick_id).execute()
    except Exception as e:
        return jsonify({"ok": False, "error": f"edit failed: {e}"}), 500
    return jsonify({"ok": True, "id": pick_id, "units": units, "confidence": confidence})


@app.route("/api/handicapper/pick/<int:pick_id>/settle", methods=["POST"])
@admin_required   # MANUAL SETTLE IS ADMIN-ONLY. Non-admins rely on the auto-resolver for their grades; their un-gradeable picks (UFC method bets, postponed games) are settled by the admin from the global /handicapper-analytics page (admin can settle ANY user's row). This keeps users from mis-grading their own book while still giving every pick a path to resolution.
def api_handicapper_pick_settle(pick_id: int):
    """Manually settle a pending pick. Use case: UFC fights (no auto-grade
    for SPR/TOT method-of-victory bets), or any pick where the resolver
    can't reach ESPN reliably (rare). Body: {status: 'won'|'lost'|'push'}.
    Computes pnl_units via the same to-WIN math the resolver uses.

    Authorization: admin only (the decorator). The admin can settle ANY
    user's pick — the analytics page surfaces all users' pending rows with
    Won/Lost/Push buttons so un-gradeable picks across the whole user base
    still get resolved. The inline admin-OR-owner check below is retained
    as defense-in-depth / for any future per-owner settle path."""
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
# Pick Bot — admin GLOBAL analytics (all users' picks)
# ---------------------------------------------------------------------------

@app.route("/api/handicapper/analytics")
@admin_required   # GLOBAL view across EVERY user's bot_picks — the model-tuning signal. Per-user pages scope to asked_by; this one deliberately doesn't.
def api_handicapper_analytics():
    """Admin-only global rollup of ALL users' picks (the labeled dataset for
    model tuning). Returns global stat buckets (today/7d/30d), a per-user
    leaderboard (graded/won/lost/push/units/roi/hit_rate/avg_clv_pp +
    pending count), every user's pending picks (so the admin can settle any
    un-gradeable row), and today's settled across everyone. Each row carries
    asked_by + the asker's display name."""
    sb = get_supabase()
    if sb is None:
        return jsonify({"ok": False, "error": "Supabase not configured"}), 503

    now = datetime.now(timezone.utc)
    cutoff_30d = (now - timedelta(days=30)).isoformat()
    cutoff_7d = (now - timedelta(days=7)).isoformat()
    local_now = now.astimezone(ZoneInfo("America/Phoenix"))
    _today_start_local = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    today_start_iso = _today_start_local.astimezone(timezone.utc).isoformat()
    yesterday_start_iso = ((_today_start_local - timedelta(days=1))
                           .astimezone(timezone.utc).isoformat())

    cols = ("id,market_id,picked_at,asked_by,query_text,sport,event_name,event_start,"
            "market_type,side,entry_book,entry_price,entry_line,"
            "units,confidence,fair_prob,edge_pp,sharp_score,clv_pp,"
            "analysis_md,reasons,status,pnl_units,result_score,settled_at")
    try:
        pending = (sb.table("bot_picks").select(cols)
                   .eq("status", "pending")
                   .order("event_start", desc=False)
                   .limit(2000).execute().data) or []
        settled_30d = (sb.table("bot_picks").select(cols)
                       .in_("status", ["won", "lost", "push", "void"])
                       .gte("settled_at", cutoff_30d)
                       .order("settled_at", desc=True)
                       .limit(5000).execute().data) or []
    except Exception as e:
        return jsonify({"ok": False, "error": f"Supabase: {e}"}), 500

    # uid -> display name (one Firestore read of the users collection).
    names: dict = {}
    try:
        db = get_db()
        for u in db.collection("users").stream():
            ud = u.to_dict() or {}
            names[u.id] = ud.get("displayName") or ud.get("email") or u.id
    except Exception:
        names = {}

    def _bucket():
        return {"graded": 0, "won": 0, "lost": 0, "push": 0, "pnl": 0.0,
                "hit_rate": None, "roi": None,
                "clv_sum": 0.0, "clv_n": 0, "avg_clv_pp": None}

    def _add(b, st, pnl, clv):
        b["graded"] += 1
        b[st] += 1
        b["pnl"] += pnl
        if clv is not None:
            b["clv_sum"] += clv
            b["clv_n"] += 1

    def _finalize(s):
        decided = s["won"] + s["lost"]
        if decided > 0:
            s["hit_rate"] = round(s["won"] / decided, 4)
        if s["graded"] > 0:
            s["roi"] = round(s["pnl"] / s["graded"], 4)
        if s["clv_n"] > 0:
            s["avg_clv_pp"] = round(s["clv_sum"] / s["clv_n"], 2)
        s["pnl"] = round(s["pnl"], 3)

    g_today, g_yesterday, g_week, g_30d = _bucket(), _bucket(), _bucket(), _bucket()
    per_user: dict = {}
    for r in settled_30d:
        st = r.get("status")
        if st not in ("won", "lost", "push"):
            continue
        try:
            pnl = float(r.get("pnl_units") or 0)
        except (TypeError, ValueError):
            pnl = 0.0
        try:
            clv = r.get("clv_pp")
            clv = float(clv) if clv is not None else None
        except (TypeError, ValueError):
            clv = None
        es = r.get("event_start") or ""
        uid = r.get("asked_by") or "unknown"
        pu = per_user.setdefault(uid, _bucket())
        _add(pu, st, pnl, clv)
        _add(g_30d, st, pnl, clv)
        if es >= cutoff_7d:
            _add(g_week, st, pnl, clv)
        if es >= today_start_iso:
            _add(g_today, st, pnl, clv)
        elif es >= yesterday_start_iso:
            _add(g_yesterday, st, pnl, clv)

    # Pending counts per user.
    pending_count: dict = {}
    for r in pending:
        uid = r.get("asked_by") or "unknown"
        pending_count[uid] = pending_count.get(uid, 0) + 1

    leaderboard = []
    for uid, b in per_user.items():
        _finalize(b)
        leaderboard.append({
            "uid": uid, "name": names.get(uid, uid),
            "pending": pending_count.get(uid, 0), **b,
        })
    # Surface users who only have pending picks (no graded rows yet) too —
    # an all-zero bucket (hit_rate/roi/avg_clv_pp stay None).
    for uid, cnt in pending_count.items():
        if uid not in per_user:
            leaderboard.append({
                "uid": uid, "name": names.get(uid, uid), "pending": cnt,
                **_bucket(),
            })
    leaderboard.sort(key=lambda x: (x.get("pnl") or 0), reverse=True)

    for s in (g_today, g_yesterday, g_week, g_30d):
        _finalize(s)

    # Attach asker name to every pending + today's-settled row for the UI.
    def _name(r):
        r["asked_by_name"] = names.get(r.get("asked_by"), r.get("asked_by") or "—")
        return r
    pending = [_name(r) for r in pending]
    settled_today = [_name(r) for r in settled_30d
                     if (r.get("event_start") or "") >= today_start_iso]

    return jsonify({
        "ok": True,
        "now_iso": now.isoformat(),
        "stats_today": g_today,
        "stats_yesterday": g_yesterday,
        "stats_week": g_week,
        "stats_30d": g_30d,
        "leaderboard": leaderboard,
        "pending": pending,
        "settled": settled_today,
        "user_count": len(set(list(per_user.keys()) + list(pending_count.keys()))),
    })


@app.route("/handicapper-analytics")
def handicapper_analytics_page():
    """Admin-only GLOBAL Pick Bot analytics (all users' picks + outcomes/CLV).
    Client-gated via /api/me (bounces non-admins); the data endpoint is
    @admin_required. Lets the admin see the aggregate labeled dataset for
    model tuning AND settle any user's un-gradeable pending pick."""
    return render_template("handicapper_analytics.html")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    debug = os.getenv("FLASK_DEBUG", "0") == "1"
    app.run(host="0.0.0.0", port=port, debug=debug)
