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

import requests as http_requests
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
OWLS_INSIGHT_API_KEY = os.getenv("OWLS_INSIGHT_API_KEY", "")

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
    return render_template("dashboard.html")


@app.route("/budget")
def budget():
    return render_template("budget.html")


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

    client_tz = timezone(timedelta(minutes=-tz_offset_minutes))
    now_local = datetime.now(client_tz)
    today_str = now_local.strftime("%Y-%m-%d")
    yesterday_str = (now_local - timedelta(days=1)).strftime("%Y-%m-%d")

    for act in parsed_activities:
        has_pnl = act["pnl"] is not None
        is_resolution = act["type"] == "Position Resolution"
        is_trade_close = act["type"] == "Trade" and act.get("_is_close") and has_pnl

        if (is_resolution or is_trade_close) and has_pnl:
            realized_pnl += act["pnl"]
            resolved_total += 1
            if act["pnl"] > 0:
                resolved_wins += 1

        if (is_resolution or is_trade_close) and has_pnl:
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

    total_pnl = open_pnl + realized_pnl
    win_rate = (resolved_wins / resolved_total * 100) if resolved_total > 0 else None

    return {
        "total_positions": len([p for p in enriched if not p.get("expired")]),
        "total_invested": total_invested,
        "total_current": total_current,
        "total_pnl": total_pnl,
        "open_pnl": open_pnl,
        "realized_pnl": realized_pnl,
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
    }

    slug_to_title = {}
    parsed = []
    for act in activities:
        act_type = act.get("type", "unknown")
        detail_key = TYPE_KEY_MAP.get(act_type, "")
        detail = act.get(detail_key, {}) if detail_key else {}

        timestamp = detail.get("updateTime") or detail.get("timestamp") or ""
        market_slug = detail.get("marketSlug", "")

        market = ""
        side = ""
        price = None
        quantity = None
        pnl = None
        is_close = False

        if act_type == "ACTIVITY_TYPE_TRADE":
            price = _safe_float(detail.get("price"))
            quantity = _safe_float(detail.get("qty"))
            sdk_rpnl = _safe_float(detail.get("realizedPnl"))
            pnl = None

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

OWLS_BASE = "https://api.owlsinsight.com/api/v1"
OWLS_SPORTS = ["mlb", "nba", "nhl", "nfl", "ncaab", "ncaaf", "mma", "soccer", "tennis"]
OWLS_CACHE_TTL = 10  # seconds

_owls_cache = {}


def _owls_get(path, params=None):
    headers = {"Authorization": f"Bearer {OWLS_INSIGHT_API_KEY}"}
    resp = http_requests.get(f"{OWLS_BASE}{path}", headers=headers,
                             params=params, timeout=15)
    resp.raise_for_status()
    return resp.json()


def _owls_get_cached(sport, books):
    import time
    cache_key = f"{sport}:{books}"
    now = time.time()
    cached = _owls_cache.get(cache_key)
    if cached and (now - cached["ts"]) < OWLS_CACHE_TTL:
        return cached["data"], True
    params = {}
    if books:
        params["books"] = books
    raw = _owls_get(f"/{sport}/odds", params)
    _owls_cache[cache_key] = {"data": raw, "ts": now}
    return raw, False


def _normalize_owls_odds(sport, raw_data):
    books_data = raw_data.get("data", {})
    if not isinstance(books_data, dict):
        return []

    events_map = {}
    for book_key, book_events in books_data.items():
        if not isinstance(book_events, list):
            continue
        for ev in book_events:
            eid = ev.get("eventId") or ev.get("id", "")
            if not eid:
                continue
            if eid not in events_map:
                events_map[eid] = {
                    "id": eid,
                    "numeric_id": str(ev.get("id", "")),
                    "sport": sport,
                    "home_team": ev.get("home_team", ""),
                    "away_team": ev.get("away_team", ""),
                    "commence_time": ev.get("commence_time", ""),
                    "league": ev.get("league", ""),
                    "status": ev.get("status", ""),
                    "books": {},
                }
            for bk in ev.get("bookmakers", []):
                bk_key = bk.get("key", book_key)
                book_odds = {
                    "moneyline": {}, "spread": {}, "total": {},
                    "event_link": bk.get("event_link", ""),
                }
                for mkt in bk.get("markets", []):
                    mkt_key = mkt.get("key", "")
                    outcomes = mkt.get("outcomes", [])
                    if mkt_key in ("h2h", "moneyline"):
                        for o in outcomes:
                            book_odds["moneyline"][o["name"]] = o.get("price")
                    elif mkt_key == "spreads":
                        for o in outcomes:
                            book_odds["spread"][o["name"]] = {
                                "price": o.get("price"),
                                "point": o.get("point"),
                            }
                    elif mkt_key == "totals":
                        for o in outcomes:
                            book_odds["total"][o["name"]] = {
                                "price": o.get("price"),
                                "point": o.get("point"),
                            }
                events_map[eid]["books"][bk_key] = book_odds

    return sorted(events_map.values(), key=lambda e: e.get("commence_time", ""))


def _fetch_scores(sport):
    import time
    cache_key = f"scores:{sport}"
    now = time.time()
    cached = _owls_cache.get(cache_key)
    if cached and (now - cached["ts"]) < 30:
        return cached["data"], True
    try:
        raw = _owls_get(f"/{sport}/scores/live")
        _owls_cache[cache_key] = {"data": raw, "ts": now}
        return raw, False
    except Exception:
        try:
            raw = _owls_get("/scores/live")
            _owls_cache[cache_key] = {"data": raw, "ts": now}
            return raw, False
        except Exception:
            return {}, False


def _merge_scores(events, raw_scores, sport):
    sport_scores = raw_scores.get("events", [])
    if not sport_scores:
        raw_data = raw_scores.get("data", {})
        if isinstance(raw_data, list):
            sport_scores = raw_data
        elif isinstance(raw_data, dict):
            sport_scores = raw_data.get("sports", {}).get(sport, [])
    if not sport_scores:
        return events

    scores_lookup = []
    for game in sport_scores:
        away_info = game.get("away", {})
        home_info = game.get("home", {})
        away_team = away_info.get("team", {})
        home_team = home_info.get("team", {})
        away_name = away_team.get("displayName", "")
        home_name = home_team.get("displayName", "")

        status_obj = game.get("status", {})
        state = ""
        if isinstance(status_obj, dict):
            state = status_obj.get("state") or status_obj.get("type", {}).get("description", "")
        elif isinstance(status_obj, str):
            state = status_obj

        display_status = game.get("displayStatus") or ""
        period = game.get("period") or game.get("inning") or ""
        clock = game.get("displayClock") or game.get("clock") or ""

        scores_lookup.append({
            "teams": frozenset([away_name.lower(), home_name.lower()]),
            "away_name": away_name,
            "home_name": home_name,
            "away_score": away_info.get("score"),
            "home_score": home_info.get("score"),
            "state": state,
            "display_status": str(display_status),
            "period": str(period),
            "clock": str(clock),
        })

    for ev in events:
        ev_teams = frozenset([ev.get("away_team", "").lower(), ev.get("home_team", "").lower()])
        for sc in scores_lookup:
            if ev_teams == sc["teams"]:
                ev_away = ev.get("away_team", "").lower()
                base = {
                    "state": sc["state"],
                    "display_status": sc["display_status"],
                    "period": sc["period"],
                    "clock": sc["clock"],
                    "live": sc["state"] in ("in", "live", "In Progress"),
                }
                if sc["away_name"].lower() == ev_away:
                    ev["score"] = {**base,
                        "away_score": sc["away_score"],
                        "home_score": sc["home_score"],
                    }
                else:
                    ev["score"] = {**base,
                        "away_score": sc["home_score"],
                        "home_score": sc["away_score"],
                    }
                break

    return events


def _fetch_splits(sport):
    import time
    cache_key = f"splits:{sport}"
    now = time.time()
    cached = _owls_cache.get(cache_key)
    if cached and (now - cached["ts"]) < OWLS_CACHE_TTL:
        return cached["data"], True
    try:
        raw = _owls_get(f"/{sport}/splits")
        _owls_cache[cache_key] = {"data": raw, "ts": now}
        return raw, False
    except Exception as e:
        print(f"SPLITS ERROR: {e}")
        return {}, False


def _normalize_splits(raw_splits):
    splits_map = {}
    splits_by_teams = {}
    raw_data = raw_splits.get("data", [])
    if not isinstance(raw_data, list):
        return {}, {}

    for ev in raw_data:
        eid = ev.get("event_id") or ev.get("eventId") or ev.get("id", "")
        eid = str(eid) if eid else ""

        ev_splits = {}
        for sp in ev.get("splits", []):
            book = sp.get("book", "")
            if not book:
                continue
            ev_splits[book] = {
                "title": sp.get("title", book),
                "moneyline": sp.get("moneyline", {}),
                "spread": sp.get("spread", {}),
                "total": sp.get("total", {}),
            }

        if ev_splits:
            has_circa = "circa" in ev_splits
            if eid:
                if has_circa or eid not in splits_map:
                    splits_map[eid] = ev_splits
            away = ev.get("away_team", "").lower()
            home = ev.get("home_team", "").lower()
            if away and home:
                teams_key = frozenset([away, home])
                if has_circa or teams_key not in splits_by_teams:
                    splits_by_teams[teams_key] = ev_splits

    return splits_map, splits_by_teams


def _merge_splits(events, splits_map, splits_by_teams=None):
    if splits_by_teams is None:
        splits_by_teams = {}
    for ev in events:
        nid = str(ev.get("numeric_id", ""))
        eid = str(ev.get("id", ""))
        found = splits_map.get(nid) or splits_map.get(eid)
        if not found:
            away = ev.get("away_team", "").lower().strip()
            home = ev.get("home_team", "").lower().strip()
            if away and home:
                teams_key = frozenset([away, home])
                found = splits_by_teams.get(teams_key)
        ev["splits"] = found if found else {}
    return events


# ---------------------------------------------------------------------------
# Openers API (Firestore — replaces localStorage)
# ---------------------------------------------------------------------------

@app.route("/api/openers", methods=["GET"])
@firebase_auth_required
def api_openers_get():
    """Retrieve opening lines for a sport from Firestore (permanent per game ID)."""
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
# API routes — Odds
# ---------------------------------------------------------------------------

@app.route("/api/odds")
@firebase_auth_required
def api_odds():
    if not OWLS_INSIGHT_API_KEY:
        return jsonify({"ok": False, "error": "OWLS_INSIGHT_API_KEY not configured"}), 500

    sport = request.args.get("sport", "mlb")
    books = request.args.get("books", "")

    errors = []
    events = []
    from_cache = False
    meta_message = ""

    try:
        raw, from_cache = _owls_get_cached(sport, books)
        events = _normalize_owls_odds(sport, raw)
        meta = raw.get("meta", {})
        if meta.get("message"):
            meta_message = meta["message"]
    except http_requests.HTTPError as e:
        errors.append(f"{sport}: HTTP {e.response.status_code}")
    except Exception as e:
        errors.append(f"{sport}: {e}")

    splits_ts = None
    try:
        raw_splits, _ = _fetch_splits(sport)
        # Use our own fetch timestamp since OWLS doesn't provide one
        cache_key = f"splits:{sport}"
        cached = _owls_cache.get(cache_key)
        if cached:
            splits_ts = datetime.fromtimestamp(cached["ts"], tz=timezone.utc).isoformat()
        splits_map, splits_by_teams = _normalize_splits(raw_splits)
        events = _merge_splits(events, splits_map, splits_by_teams)
    except Exception as e:
        errors.append(f"splits: {e}")

    try:
        raw_scores, _ = _fetch_scores(sport)
        events = _merge_scores(events, raw_scores, sport)
    except Exception as e:
        errors.append(f"scores: {e}")

    active_books = set()
    leagues = set()
    for ev in events:
        active_books.update(ev.get("books", {}).keys())
        if ev.get("league"):
            leagues.add(ev["league"])

    return jsonify({
        "ok": True,
        "cached": from_cache,
        "sport": sport,
        "events": events,
        "books": sorted(active_books, key=lambda b: (0 if b == "circa" else 1 if b == "pinnacle" else 2, b)),
        "leagues": sorted(leagues),
        "meta_message": meta_message,
        "splits_timestamp": splits_ts,
        "errors": errors,
    })


@app.route("/api/my-bets")
@firebase_auth_required
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
@firebase_auth_required
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
                        or (a["type"] == "Trade" and a.get("_is_close") and a.get("pnl") is not None)]

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
# Debug routes (admin only)
# ---------------------------------------------------------------------------

@app.route("/api/odds/raw")
@admin_required
def api_odds_raw():
    if not OWLS_INSIGHT_API_KEY:
        return jsonify({"error": "no key"}), 500
    sport = request.args.get("sport", "mlb")
    books = request.args.get("books", "pinnacle,fanduel")
    try:
        raw = _owls_get(f"/{sport}/odds", {"books": books})
        return jsonify(raw)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/odds/debug-markets")
@firebase_auth_required
def api_odds_debug_markets():
    """Show all market keys per book for a sport — helps diagnose missing moneyline."""
    if not OWLS_INSIGHT_API_KEY:
        return jsonify({"error": "no key"}), 500
    sport = request.args.get("sport", "mlb")
    try:
        raw = _owls_get(f"/{sport}/odds")
        books_data = raw.get("data", {})
        result = {}
        for book_key, book_events in books_data.items():
            if not isinstance(book_events, list):
                continue
            market_keys = set()
            game_count = 0
            ml_count = 0
            for ev in book_events:
                game_count += 1
                for bk in ev.get("bookmakers", []):
                    for mkt in bk.get("markets", []):
                        mk = mkt.get("key", "")
                        market_keys.add(mk)
                        if mk in ("h2h", "moneyline"):
                            ml_count += 1
            result[book_key] = {
                "games": game_count,
                "market_keys": sorted(market_keys),
                "games_with_ml": ml_count,
            }
        return jsonify({"ok": True, "sport": sport, "books": result})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/splits/raw")
@admin_required
def api_splits_raw():
    if not OWLS_INSIGHT_API_KEY:
        return jsonify({"error": "no key"}), 500
    sport = request.args.get("sport", "mlb")
    try:
        raw = _owls_get(f"/{sport}/splits")
        return jsonify(raw)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/scores/raw")
@admin_required
def api_scores_raw():
    if not OWLS_INSIGHT_API_KEY:
        return jsonify({"error": "no key"}), 500
    sport = request.args.get("sport", "")
    try:
        if sport:
            try:
                raw = _owls_get(f"/{sport}/scores/live")
                return jsonify(raw)
            except Exception:
                raw = _owls_get(f"/scores/live", {"sport": sport})
        else:
            raw = _owls_get(f"/scores/live")
        return jsonify(raw)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/realtime/raw")
@admin_required
def api_realtime_raw():
    if not OWLS_INSIGHT_API_KEY:
        return jsonify({"error": "no key"}), 500
    sport = request.args.get("sport", "mlb")
    feed = request.args.get("feed", "realtime")
    try:
        raw = _owls_get(f"/{sport}/{feed}")
        return jsonify(raw)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


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
        entry = {
            "timestamp": detail.get("updateTime") or detail.get("timestamp"),
            "price": detail.get("price"),
            "qty": detail.get("qty"),
            "cost": detail.get("cost"),
            "realizedPnl": rpnl,
            "is_sell": rpnl is not None,
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
