#!/usr/bin/env python3
"""Polymarket Fill Alerts → Telegram.

Single-purpose Flask app deployed on Vercel. One endpoint —
/api/polymarket/check-fills — pinged every minute by cron-job.org.
Each tick:

  1. Pulls your currently-open Polymarket orders via the Polymarket
     US SDK.
  2. Diffs them against the polymarket_fill_state table in Supabase
     to detect partial-fill milestone crossings (25 / 50 / 75 / 100).
  3. Also detects orders that vanished from the SDK response since
     last tick (= filled or canceled); confirms fill by looking for
     a matching trade activity within the last 3 minutes.
  4. Sends a Telegram message via your dedicated "Filled Bot" for
     each newly-crossed milestone. Buys → ✅ FILLED / 📈 partial,
     Sells → 💰 SOLD / 📤 partial.

See README.md for end-to-end setup."""

import os
import re
import json
import secrets
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

from flask import Flask, request, jsonify
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

# ── Config ─────────────────────────────────────────────────────────
POLYMARKET_KEY_ID     = os.getenv("POLYMARKET_KEY_ID", "").strip()
POLYMARKET_SECRET_KEY = os.getenv("POLYMARKET_SECRET_KEY", "").strip()
SUPABASE_URL          = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_SERVICE_KEY  = os.getenv("SUPABASE_SERVICE_KEY", "").strip()
FILLED_BOT_TOKEN      = os.getenv("FILLED_BOT_TOKEN", "").strip()
FILLED_BOT_CHAT_ID    = os.getenv("FILLED_BOT_CHAT_ID", "").strip()
FILLS_CRON_SECRET     = os.getenv("FILLS_CRON_SECRET", "").strip()


# ── Lazy clients ───────────────────────────────────────────────────
_supabase_client = None
def get_supabase():
    """Return Supabase client (lazy init). Returns None if env vars
    aren't set — the route will surface a 503 in that case."""
    global _supabase_client
    if _supabase_client is not None:
        return _supabase_client
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        return None
    from supabase import create_client
    _supabase_client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
    return _supabase_client


def get_polymarket_client():
    """Return an authenticated Polymarket US SDK client."""
    from polymarket_us import PolymarketUS
    if not POLYMARKET_KEY_ID or not POLYMARKET_SECRET_KEY:
        raise RuntimeError("Polymarket API credentials not configured")
    return PolymarketUS(key_id=POLYMARKET_KEY_ID, secret_key=POLYMARKET_SECRET_KEY)


# ── Constants ──────────────────────────────────────────────────────
_INTENT_LABEL = {
    "ORDER_INTENT_BUY_LONG":   "BUY YES",
    "ORDER_INTENT_BUY_SHORT":  "BUY NO",
    "ORDER_INTENT_SELL_LONG":  "SELL YES",
    "ORDER_INTENT_SELL_SHORT": "SELL NO",
}

# SDK states that mean "this order will never fill more". Fast-skip
# once we mark a row terminal.
_TERMINAL_ORDER_STATES = {
    "ORDER_STATE_FILLED",
    "ORDER_STATE_CANCELED",
    "ORDER_STATE_EXPIRED",
    "ORDER_STATE_REJECTED",
}

# Fill-progress milestones. 100% has an extra state gate to prevent a
# partial sitting at 99.7% from being misclassified as full.
_FILL_MILESTONES = (("25", 25.0), ("50", 50.0), ("75", 75.0), ("100", 100.0))

# First-sight terminal orders less than this old fire an alert (an
# instant fill between cron ticks). Older = treated as historical.
_FILL_FRESH_TERMINAL_SECONDS = 600

# When an order disappears from the SDK response, we look for a
# matching trade activity within this window to confirm "filled" vs
# "canceled". Tight enough to exclude unrelated historical trades on
# the same outcome.
_TRADE_MATCH_WINDOW_MINUTES = 3


# ── Helpers ────────────────────────────────────────────────────────
def _safe_float(val):
    """Extract a float from an SDK value, handling Amount dicts."""
    if val is None:
        return None
    if isinstance(val, dict) and "value" in val:
        val = val["value"]
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _fmt_pmm_price(p):
    """Polymarket prices are 0-1 probabilities. Render as $0.42."""
    if p is None:
        return "?"
    try:
        return f"${float(p):.2f}"
    except (TypeError, ValueError):
        return "?"


def _send_telegram(text):
    """POST to Telegram sendMessage. No-op (False) when bot env vars
    aren't set."""
    if not FILLED_BOT_TOKEN or not FILLED_BOT_CHAT_ID:
        return False
    url = f"https://api.telegram.org/bot{FILLED_BOT_TOKEN}/sendMessage"
    payload = json.dumps({
        "chat_id": FILLED_BOT_CHAT_ID,
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


def _crossed_milestones(curr_pct, curr_state, already_sent):
    """Return list of milestone keys newly crossed this tick."""
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
    """True iff order createTime is within the fresh window."""
    if not order_created_at:
        return False
    try:
        s = order_created_at.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds() <= _FILL_FRESH_TERMINAL_SECONDS
    except (ValueError, AttributeError):
        return False


def _format_alert(row, milestone, fill_pct):
    """Build the Telegram message for a fill milestone. Visually
    distinguishes buys (✅ FILLED / 📈 partial) from sells
    (💰 SOLD / 📤 partial)."""
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


# ── Routes ─────────────────────────────────────────────────────────
@app.route("/")
def index():
    """Health check. Confirms the app is deployed and reachable."""
    return jsonify({"ok": True, "service": "polymarket-fill-alerts"})


@app.route("/api/polymarket/check-fills")
def check_fills():
    """Polled every ~1 min by cron-job.org. Returns JSON describing
    what was processed this tick — visible in cron-job.org's response
    history for debugging."""
    if not FILLS_CRON_SECRET or not secrets.compare_digest(
            (request.args.get("key") or "").strip(), FILLS_CRON_SECRET):
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    sb = get_supabase()
    if sb is None:
        return jsonify({"ok": False, "error": "supabase unavailable"}), 503

    # Pull orders. Failure = bail with no state mutation.
    try:
        client = get_polymarket_client()
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
            rows = (sb.table("polymarket_fill_state").select("*")
                      .in_("order_id", order_ids).execute().data) or []
            for r in rows:
                state_map[r["order_id"]] = r
        except Exception as e:
            return jsonify({"ok": False, "error": f"db read: {e}"}), 500

    processed = 0
    alerts_fired = 0
    skipped_historical = 0
    upserts = []
    now_iso = datetime.now(timezone.utc).isoformat()

    # ── Path A: partial fills on still-open orders ──────────────
    for o in raw:
        def _g(key, default=None, _o=o):
            if isinstance(_o, dict): return _o.get(key, default)
            return getattr(_o, key, default)

        oid = _g("id") or ""
        if not oid:
            continue

        state = _g("state") or ""
        prev = state_map.get(oid)
        if prev and prev.get("terminal"):
            continue

        qty = _safe_float(_g("quantity")) or 0
        cum_qty = _safe_float(_g("cumQuantity")) or 0
        raw_price = _safe_float(_g("price"))
        intent = _g("intent") or ""
        # SDK `price` is YES-canonical. For *_SHORT (user picked NO),
        # the real fill price is 1 - price.
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

        side_label = _INTENT_LABEL.get(
            intent, intent.replace("ORDER_INTENT_", "").replace("_", " "))
        order_created_at = _g("createTime") or _g("insertTime") or ""

        fill_pct = (cum_qty / qty * 100) if qty else 0
        already_sent = (prev or {}).get("alerts_sent") or []
        is_terminal_now = state in _TERMINAL_ORDER_STATES

        new_milestones = []
        if not prev:
            # First-sight order:
            #   fresh terminal FILLED   → fire 100% (instant fill caught between ticks)
            #   any other terminal      → snapshot only (historical)
            #   open + un-filled        → snapshot only (no fill yet)
            if is_terminal_now and state == "ORDER_STATE_FILLED" \
                    and _is_fresh_terminal(order_created_at):
                new_milestones = ["100"]
            elif is_terminal_now:
                skipped_historical += 1
        else:
            new_milestones = _crossed_milestones(
                curr_pct=fill_pct, curr_state=state, already_sent=already_sent)

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
        # first_seen_at omitted on purpose — the DB default handles
        # initial insert; omitting it from update preserves the
        # original value across upserts.

        if new_milestones:
            top = new_milestones[-1]
            msg = _format_alert(row_snapshot, top, fill_pct)
            if _send_telegram(msg):
                alerts_fired += 1

        upserts.append(row_snapshot)
        processed += 1

    # ── Path B: disappeared orders (full fills) ──────────────────
    # Polymarket SDK's orders.list() only returns currently-open
    # orders. Once an order fully fills, it vanishes from the list,
    # so Path A above never sees the 100% milestone. Here we look up
    # rows from our state table that are still marked terminal=false
    # but aren't in this tick's SDK response — those are the
    # "disappeared" orders. Cross-reference recent trade activities
    # to distinguish fill (alert) vs cancel (silent).
    disappeared_filled = 0
    disappeared_canceled = 0
    try:
        known_active = (sb.table("polymarket_fill_state").select("*")
                          .eq("terminal", False).execute().data) or []
    except Exception:
        known_active = []

    disappeared = [r for r in known_active if r["order_id"] not in seen_order_ids]

    if disappeared:
        recent_trades = []
        trade_cutoff = datetime.now(timezone.utc) - timedelta(minutes=_TRADE_MATCH_WINDOW_MINUTES)
        try:
            act_resp = client.portfolio.activities(params={"limit": 100})
            for act in act_resp.get("activities", []):
                if act.get("type") != "ACTIVITY_TYPE_TRADE":
                    continue
                # SDK returns trade detail under "trade" key (NOT
                # under the full type name). Defensive fallback below
                # for shape drift.
                detail = act.get("trade")
                if not isinstance(detail, dict):
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
                    # Activities are most-recent-first; once we cross
                    # the cutoff, everything else is older too.
                    break
                recent_trades.append(detail)
        except Exception:
            recent_trades = []

        for row in disappeared:
            slug = row.get("slug") or ""
            qty = _safe_float(row.get("quantity")) or 0

            # Consume the first matching trade so two simultaneous
            # orders on the same outcome don't both match the same
            # trade.
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
                if "100" not in sent:
                    row_snapshot["last_cum_quantity"] = qty
                    row_snapshot["last_state"] = "ORDER_STATE_FILLED"
                    row_snapshot["alerts_sent"] = sent + ["100"]
                    msg = _format_alert(row_snapshot, "100", 100.0)
                    if _send_telegram(msg):
                        alerts_fired += 1
            else:
                disappeared_canceled += 1

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
