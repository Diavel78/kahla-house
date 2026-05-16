# Polymarket Fill Alerts → Telegram — Complete Setup Bundle

Everything you need is in this one file. Save it, then either follow the steps yourself or paste this entire file into Perplexity / ChatGPT and ask it to walk you through it.

At the end you will have a free, self-hosted bot that pings your Telegram every time one of your Polymarket orders fills (partial or full, buy or sell).

You will get messages like:

> ✅ **FILLED**
> *NFL Champion 2026*
> **Eagles · BUY YES @ $0.23**
> 100/100 shares

> 💰 **SOLD**
> *NFL Champion 2026*
> **Eagles · SELL YES @ $0.41**
> 100/100 shares

> 📈 **50% FILLED**
> *NFL Champion 2026*
> **Eagles · BUY YES @ $0.23**
> 50/100 shares (50%)

---

## What this costs

**$0/month.** Every service used has a free tier that covers this workload with plenty of headroom.

## What you'll need (all free, no credit card)

1. A **Polymarket US account** (you presumably already have one)
2. A **GitHub account**
3. A **Vercel account** (sign up with GitHub)
4. A **Supabase account**
5. A **Telegram account** (the mobile app)
6. A **cron-job.org account**

Total setup time: ~30 minutes if it's your first time touching these services.

---

## How it works (skim if you want, skip if you don't care)

A tiny Python web app runs on Vercel. Every 60 seconds, cron-job.org pings one URL on that app. When pinged, the app:

1. Asks Polymarket what your currently-open orders are.
2. Compares to a snapshot stored in Supabase from 60s ago.
3. Detects two things: (a) **partial fills** — open orders whose filled-share count went up; (b) **vanished orders** — orders that used to be open but aren't anymore, cross-referenced against your recent trade history to confirm they actually filled (vs. you canceling them).
4. Sends a Telegram message via your bot for each newly-detected fill.

The Supabase table keeps a flag per order so the same fill is never alerted twice.

---

# Part 1 — Setup walkthrough

Do these steps in order. Each one is short — most are just copying values from one tab to another.

The code itself comes later in **Part 2**. Read Part 1 first to understand what's happening before you start creating files.

### Step 1 — Create your GitHub repo

You'll need a private repo on GitHub to hold seven small files (provided in Part 2 below).

1. Go to [github.com/new](https://github.com/new). Sign up if you haven't.
2. Repository name: `polymarket-fill-alerts` (or whatever you want).
3. Set it to **Private**.
4. Do NOT check "Add a README file" or any other init options.
5. Click **Create repository**.

You now have an empty repo. We'll fill it after the walkthrough.

### Step 2 — Get your Polymarket API credentials

You need a **Key ID** and a **Secret Key** from Polymarket so the app can read your orders.

1. Log into your Polymarket US account (mobile app or web).
2. Go to **Settings** → **API Keys** (or **Developer Settings** — Polymarket has moved this around).
3. Click **Create new API key**. Name it "Fill Alerts" or similar.
4. Polymarket will show you the **Key ID** and **Secret Key** — copy both into a temporary note (Notes app, sticky note, whatever). **The secret is shown only once.** If you lose it, you'll have to revoke the key and make a new one.

> If you genuinely can't find an API Keys section in your Polymarket account, message their support — the regulated US app sometimes hides this behind a request flow.

### Step 3 — Create the Telegram "Filled Bot"

1. Open Telegram on your phone (or [web.telegram.org](https://web.telegram.org)).
2. Search for **@BotFather** and open the chat.
3. Send `/newbot`.
4. BotFather will ask for a **display name** — call it something like "Filled Bot" or "Polymarket Fills".
5. Then a **username** — must end in `bot`. e.g. `your_name_filled_bot`.
6. BotFather replies with a **token** that looks like `1234567890:ABCdefGhiJklmnoPQR-stuVWxyz1234567`. **Copy it into your temporary note.**
7. **Important:** open a chat with your new bot (tap its name in BotFather's message) and send it any message — `hi` is fine. This activates the chat so it shows up in the next step.

### Step 4 — Get your Telegram chat ID

This is just your numeric Telegram user ID. The app needs it to know who to message.

In a browser, paste this URL — **replace `<YOUR_BOT_TOKEN>` with the token from Step 3**:

```
https://api.telegram.org/bot<YOUR_BOT_TOKEN>/getUpdates
```

The literal word `bot` goes right before your token (no space, no slash). Example:
`https://api.telegram.org/bot1234567890:ABCdefGhi.../getUpdates`

You'll see a JSON response. Find `"chat":{"id":12345678,...}`. That number (without quotes) is your **chat ID**. Copy it into your temporary note.

> If you see `{"ok":true,"result":[]}` (empty), the bot hasn't received any messages yet. Go back to Telegram and send the bot another message, then refresh the URL.
>
> If you see `Not Found` or `Unauthorized`, your token is wrong — re-copy it from BotFather (`/mybots` → your bot → **API Token**).

### Step 5 — Create the Supabase project + table

Supabase is a hosted Postgres database. We use it to remember what we've already alerted on.

1. Go to [supabase.com](https://supabase.com), sign up.
2. **New project** → name it whatever, pick a region near you, set a database password (you won't need it for this), wait ~2 min for it to provision.
3. Once provisioned, click **SQL Editor** in the left sidebar → **New query**.
4. Copy the entire contents of `schema.sql` from Part 2 below, paste into the SQL editor, and click **Run** (or Cmd/Ctrl + Enter).
5. You should see "Success. No rows returned."
6. Now grab the two values the app needs:
   - Click **Project Settings** (gear icon, bottom left) → **API**.
   - Copy the **Project URL** (looks like `https://abcxyz123.supabase.co`) into your note.
   - Copy the **`service_role` secret key** (NOT the `anon` one — the service_role one is below it and is labeled "secret"). **This bypasses Row-Level Security**, which is what we need. Treat it like a password.

### Step 6 — Add the code to your GitHub repo

From your empty GitHub repo (created in Step 1):

1. Click **uploading an existing file** (or **Add file → Upload files**).
2. For each of the seven files in **Part 2** below: create the file locally on your computer (any text editor — Notes, TextEdit, Notepad, VS Code, whatever), paste the code, save with the filename as shown, then drag it into the GitHub upload area.
3. After uploading all seven, scroll down and click **Commit changes**.

Alternative if you're comfortable with terminal:
```bash
mkdir polymarket-fill-alerts && cd polymarket-fill-alerts
# Create each of the 7 files from Part 2 with the right name and content.
git init
git add .
git commit -m "Initial commit"
git remote add origin https://github.com/YOUR_USERNAME/polymarket-fill-alerts.git
git branch -M main
git push -u origin main
```

### Step 7 — Deploy to Vercel

1. Go to [vercel.com](https://vercel.com), sign up using your GitHub account (this auto-grants Vercel access to your repos).
2. Click **Add New → Project**.
3. Find your `polymarket-fill-alerts` repo and click **Import**.
4. On the configure screen: leave **Framework Preset** as "Other", leave everything else at defaults.
5. Click **Deploy**.

The first deploy will succeed but the app won't actually work yet — it has no env vars. Fix that next.

### Step 8 — Add the env vars in Vercel

In your Vercel project: **Settings** → **Environment Variables**. Add **all seven** of these (one at a time), making sure each one is scoped to **Production** (and ideally Preview + Development too — toggle all three checkboxes).

| Name | Value |
|---|---|
| `POLYMARKET_KEY_ID` | From Step 2 |
| `POLYMARKET_SECRET_KEY` | From Step 2 |
| `SUPABASE_URL` | From Step 5 (the Project URL) |
| `SUPABASE_SERVICE_KEY` | From Step 5 (the service_role key) |
| `FILLED_BOT_TOKEN` | From Step 3 |
| `FILLED_BOT_CHAT_ID` | From Step 4 |
| `FILLS_CRON_SECRET` | Make up any long random string — e.g. `a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6` (~32 chars). This is the password that protects your fill-alert endpoint from being hit by randos. **Save it — you'll need the same value in Step 10.** |

After saving them all, **redeploy** so the env vars actually load: **Deployments** tab → click the three-dot menu on the latest deployment → **Redeploy** → confirm. Takes ~30s.

### Step 9 — Test the endpoint manually

Find your Vercel deployment URL — it's listed under **Deployments** or under **Domains** in Settings. Looks like `https://polymarket-fill-alerts-xxxx.vercel.app`.

In your browser, visit:

```
https://YOUR-VERCEL-URL.vercel.app/api/polymarket/check-fills?key=YOUR_FILLS_CRON_SECRET
```

(Replace `YOUR-VERCEL-URL` and `YOUR_FILLS_CRON_SECRET` with your actual values.)

You should see JSON like:

```json
{"alerts_fired":0,"disappeared_canceled":0,"disappeared_filled":0,"ok":true,"processed":N,"skipped_historical":N}
```

Where `processed` is the number of open orders you currently have on Polymarket. `alerts_fired: 0` is correct — on the very first run, every existing order gets snapshotted **without alerting** so you don't get spammed by your entire current order book.

> If you get `{"ok":false,"error":"unauthorized"}` — your `FILLS_CRON_SECRET` env var doesn't match what you put in the URL.
>
> If you get a 5xx error or `{"ok":false,"error":"sdk: ..."}` — your Polymarket credentials are wrong or Vercel hasn't picked up env-var changes yet (re-redeploy).

### Step 10 — Set up cron-job.org

1. Go to [cron-job.org](https://cron-job.org), sign up (free).
2. Click **Create cronjob**.
3. Fill in:
   - **Title:** `Polymarket fill alerts` (or whatever)
   - **URL:** the same URL you tested in Step 9 (including the `?key=...` part)
   - **Schedule:** every 1 minute (the smallest option on the free tier)
   - **Request method:** GET
   - **Enabled:** yes
4. Save.

Within 60 seconds you should see a "Successful — 200 OK" entry in the cron's execution history.

> If you see "Failed (HTTP error) 307 Temporary Redirect": your URL is using a domain that redirects. Try the `https://your-app.vercel.app` form Vercel gave you directly (not a custom domain).

### Step 11 — Real test: place a tiny order

Place any small order on Polymarket — even $0.10 worth. When it fills (whether that's instantly or hours later), within ~60s you'll get a `✅ FILLED` (or `💰 SOLD`) message from your Filled Bot.

**You're done.**

---

# Part 2 — The seven files

Create each of these files exactly as shown (including the filename). The content goes inside the file. Code blocks below are clearly labeled with the filename.

---

## File 1 of 7: `app.py`

This is the Flask web app — the brain of the whole thing.

````python
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
     Sells → 💰 SOLD / 📤 partial."""

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
    global _supabase_client
    if _supabase_client is not None:
        return _supabase_client
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        return None
    from supabase import create_client
    _supabase_client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
    return _supabase_client


def get_polymarket_client():
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

_TERMINAL_ORDER_STATES = {
    "ORDER_STATE_FILLED",
    "ORDER_STATE_CANCELED",
    "ORDER_STATE_EXPIRED",
    "ORDER_STATE_REJECTED",
}

_FILL_MILESTONES = (("25", 25.0), ("50", 50.0), ("75", 75.0), ("100", 100.0))
_FILL_FRESH_TERMINAL_SECONDS = 600
_TRADE_MATCH_WINDOW_MINUTES = 3


# ── Helpers ────────────────────────────────────────────────────────
def _safe_float(val):
    if val is None:
        return None
    if isinstance(val, dict) and "value" in val:
        val = val["value"]
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _fmt_pmm_price(p):
    if p is None:
        return "?"
    try:
        return f"${float(p):.2f}"
    except (TypeError, ValueError):
        return "?"


def _send_telegram(text):
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
    return jsonify({"ok": True, "service": "polymarket-fill-alerts"})


@app.route("/api/polymarket/check-fills")
def check_fills():
    if not FILLS_CRON_SECRET or not secrets.compare_digest(
            (request.args.get("key") or "").strip(), FILLS_CRON_SECRET):
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    sb = get_supabase()
    if sb is None:
        return jsonify({"ok": False, "error": "supabase unavailable"}), 503

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

        if new_milestones:
            top = new_milestones[-1]
            msg = _format_alert(row_snapshot, top, fill_pct)
            if _send_telegram(msg):
                alerts_fired += 1

        upserts.append(row_snapshot)
        processed += 1

    # ── Path B: disappeared orders (full fills) ──────────────────
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
                    break
                recent_trades.append(detail)
        except Exception:
            recent_trades = []

        for row in disappeared:
            slug = row.get("slug") or ""
            qty = _safe_float(row.get("quantity")) or 0

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
````

---

## File 2 of 7: `requirements.txt`

Python dependencies. Vercel reads this and installs them automatically.

```
flask>=3.0.0
polymarket-us>=0.1.0
supabase>=2.7.0
python-dotenv>=1.0.0
```

---

## File 3 of 7: `vercel.json`

Tells Vercel this is a Python serverless function.

```json
{
  "builds": [{ "src": "app.py", "use": "@vercel/python" }],
  "routes": [{ "src": "/(.*)", "dest": "app.py" }]
}
```

---

## File 4 of 7: `schema.sql`

Run this once in Supabase's SQL Editor to create the table.

```sql
-- Polymarket fill-alert state table.
--
-- One row per Polymarket order we've ever seen. The check-fills
-- endpoint diffs the SDK response against this table every minute to
-- decide when to fire a Telegram alert.
--
-- Milestones tracked per order (in the `alerts_sent` jsonb array):
--   "25"  — partial fill crossed 25%
--   "50"  — partial fill crossed 50%
--   "75"  — partial fill crossed 75%
--   "100" — fully filled
--
-- Run once in your Supabase project's SQL Editor.

create table if not exists polymarket_fill_state (
  order_id           text primary key,
  market_name        text,
  pick               text,
  slug               text,
  intent             text,
  side_label         text,
  quantity           numeric,
  price              numeric,
  last_cum_quantity  numeric not null default 0,
  last_state         text,
  alerts_sent        jsonb not null default '[]'::jsonb,
  order_created_at   text,
  first_seen_at      timestamptz not null default now(),
  last_seen_at       timestamptz not null default now(),
  terminal           boolean not null default false
);

-- Partial index so the per-tick "what's still open?" lookup stays
-- tight even as historical rows accumulate.
create index if not exists polymarket_fill_state_active_idx
  on polymarket_fill_state (last_seen_at desc)
  where terminal = false;

-- Lock down anon access — only the service-role key (used by the
-- Flask app via SUPABASE_SERVICE_KEY) can read/write.
alter table polymarket_fill_state enable row level security;
```

---

## File 5 of 7: `.env.example`

Template only. You don't need to fill this in unless you want to run the app locally for testing. Vercel sets the real env vars from its own dashboard.

```
# Local development env vars. Copy this file to `.env` (which IS
# gitignored) and fill in real values. Vercel sets these from its own
# dashboard — this file is only for local testing.

POLYMARKET_KEY_ID=
POLYMARKET_SECRET_KEY=

SUPABASE_URL=https://your-project-ref.supabase.co
SUPABASE_SERVICE_KEY=

FILLED_BOT_TOKEN=
FILLED_BOT_CHAT_ID=

# Random secret matched by the cron URL. Generate one with:
#   openssl rand -hex 32
# or just pick any long random string.
FILLS_CRON_SECRET=
```

---

## File 6 of 7: `.gitignore`

Keeps your real env vars out of git.

```
.env
.env.local
__pycache__/
*.pyc
.vercel
```

---

## File 7 of 7: `README.md` (optional)

A short readme to put in your repo so future-you remembers what this is. Optional but nice.

````markdown
# Polymarket Fill Alerts → Telegram

Self-hosted bot that pings my Telegram when a Polymarket order fills.
- One endpoint: `/api/polymarket/check-fills`
- Pinged every minute by cron-job.org
- State stored in Supabase (`polymarket_fill_state` table)
- Messages sent via Telegram bot ("Filled Bot")

See setup bundle for end-to-end instructions.
````

---

# Part 3 — After setup: things to know

- **The first cron tick snapshots all your existing open orders WITHOUT alerting.** This is intentional — otherwise the first run would spam you with every order already on the books. The *next* time any of those orders fills, you'll get a real alert.
- **New orders placed after setup** get the full milestone treatment: 25% / 50% / 75% / 100% partial-fill alerts as they chip away, or one ✅ FILLED message if they fill in a single shot.
- **Sells use different emoji:** 💰 SOLD (full) and 📤 partial, so you can tell at a glance whether a notification is an entry or an exit.
- **Cancellations are silent.** Polymarket apps already tell you when you cancel an order — you don't need a Telegram ping for it.
- **Edge case:** an order placed AND fully filled inside a single 60-second window may not generate an alert (the cron never saw it in an "open" state). The Polymarket app itself shows the fill in that case, so you'd see it there anyway.

---

# Part 4 — Troubleshooting

**I'm getting no alerts even though orders are filling.**
1. Hit `https://YOUR-VERCEL-URL.vercel.app/api/polymarket/check-fills?key=YOUR_SECRET` manually. Does the JSON have `disappeared_filled: 0` even though you know an order filled? If yes, something's wrong with the Polymarket SDK auth — check `POLYMARKET_KEY_ID` and `POLYMARKET_SECRET_KEY` in Vercel.
2. Check the cron-job.org execution history. Are calls succeeding (200 OK)? If failing, fix the URL or the secret.
3. Make sure you placed an order in your Polymarket account that's actually authenticated with the API key you set up. Some users have multiple Polymarket accounts.

**I'm getting too many alerts / false alerts.**
- If you cancel and re-place orders rapidly, sometimes the system can briefly classify a canceled order as a fill. The system has a 3-minute trade-matching window that minimizes this, but it's not perfect.

**Vercel deployment is failing.**
- Open the deployment logs in Vercel. If you see `ModuleNotFoundError`, your `requirements.txt` is missing a dependency. Make sure all four lines are present: `flask`, `polymarket-us`, `supabase`, `python-dotenv`.

**Polymarket SDK is returning "credentials not configured" or similar.**
- The `POLYMARKET_KEY_ID` and `POLYMARKET_SECRET_KEY` env vars in Vercel either aren't set or weren't picked up. Redeploy after setting them — env-var changes do NOT auto-rebuild.

**Telegram messages aren't arriving but everything else looks right.**
- Go back to Step 3 and Step 4. Double-check the token by visiting `https://api.telegram.org/bot<TOKEN>/getMe` — you should see JSON describing your bot. If 404, the token is wrong.
- Make sure you sent the bot a message at some point (Step 3 #7) — Telegram won't deliver bot messages if you haven't initiated the chat.

**cron-job.org shows "Failed (HTTP error) 307 Temporary Redirect".**
- Your URL is using a domain that redirects. Use the `https://your-app.vercel.app` form Vercel gave you directly instead of any custom domain.

---

# Part 5 — Cost and limits at a glance

- **Vercel Hobby:** 100 GB-hours/month of serverless execution. This app uses ~5 GB-hours/month (1 tick/min × ~2s each). Plenty of headroom.
- **cron-job.org free:** unlimited jobs, 1-minute minimum interval, no cost.
- **Supabase free:** 500 MB database. This app's table stores ~200 bytes per order. You'd need to place ~2.5M orders before you'd care.
- **Telegram:** no cost, no rate limits at this volume.
- **Polymarket SDK:** no published rate limits, 1 call/min is well below anything that would matter.

**Vercel Hobby tier note:** Vercel's Terms of Service technically prohibit "commercial use" on the Hobby tier. A personal bet-tracker bot for yourself sits squarely in personal-use territory, but you should know the line exists.

---

That's everything. Good luck.
