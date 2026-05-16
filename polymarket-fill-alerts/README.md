# Polymarket Fill Alerts → Telegram

The Polymarket US app does not send notifications when your orders fill. This is a small piece of software that fixes that: it watches your account in the background and pings a Telegram bot every time one of your orders fills (partial or full, buy or sell).

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

## Setup walkthrough

Do these steps in order. Each one is short — most are just copying values from one tab to another.

### Step 1 — Get the code onto your own GitHub

Two options. Pick whichever is easier for you:

**Option A: GitHub web UI (no terminal needed).**
1. Go to [github.com/new](https://github.com/new). Create a new empty repo. Name it whatever — `polymarket-fill-alerts` works. Set it to **Private**. Don't initialize with a README.
2. Download the five files in this folder (`app.py`, `requirements.txt`, `vercel.json`, `schema.sql`, `.gitignore`) to your computer.
3. In your new GitHub repo, click "uploading an existing file" and drag in all five files.
4. Commit.

**Option B: Terminal (faster if you know git).**
```bash
mkdir polymarket-fill-alerts && cd polymarket-fill-alerts
# Copy the five files from this folder into the current directory.
git init
git add .
git commit -m "Initial commit"
gh repo create polymarket-fill-alerts --private --source=. --push
```

### Step 2 — Get your Polymarket API credentials

You need a **key ID** and a **secret key** from Polymarket so the app can read your orders.

1. Log into your Polymarket US account (mobile app or web).
2. Go to **Settings** → **API Keys** (or **Developer Settings** — Polymarket has moved this around).
3. Click **Create new API key**. Name it "Fill Alerts" or similar.
4. Polymarket will show you the **Key ID** and **Secret Key** — copy both into a temporary note. **The secret is shown only once.** If you lose it, you'll have to revoke the key and make a new one.

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
4. Open the `schema.sql` file from this repo, copy its entire contents, paste into the SQL editor, and click **Run** (or Cmd/Ctrl + Enter).
5. You should see "Success. No rows returned."
6. Now grab the two values the app needs:
   - Click **Project Settings** (gear icon, bottom left) → **API**.
   - Copy the **Project URL** (looks like `https://abcxyz123.supabase.co`) into your note.
   - Copy the **`service_role` secret key** (NOT the `anon` one — the service_role one is below it and is labeled "secret"). **This bypasses Row-Level Security**, which is what we need. Treat it like a password.

### Step 6 — Deploy to Vercel

1. Go to [vercel.com](https://vercel.com), sign up using your GitHub account (this auto-grants Vercel access to your repos).
2. Click **Add New → Project**.
3. Find your `polymarket-fill-alerts` repo and click **Import**.
4. On the configure screen: leave **Framework Preset** as "Other", leave everything else at defaults.
5. Click **Deploy**.

The first deploy will succeed but the app won't actually work yet — it has no env vars. Fix that next.

### Step 7 — Add the env vars in Vercel

In your Vercel project: **Settings** → **Environment Variables**. Add **all seven** of these (one at a time), making sure each one is scoped to **Production** (and ideally Preview + Development too — toggle all three checkboxes).

| Name | Value |
|---|---|
| `POLYMARKET_KEY_ID` | From Step 2 |
| `POLYMARKET_SECRET_KEY` | From Step 2 |
| `SUPABASE_URL` | From Step 5 (the Project URL) |
| `SUPABASE_SERVICE_KEY` | From Step 5 (the service_role key) |
| `FILLED_BOT_TOKEN` | From Step 3 |
| `FILLED_BOT_CHAT_ID` | From Step 4 |
| `FILLS_CRON_SECRET` | Make up any long random string — e.g. `a1b2c3d4e5f6...` (~32 chars). This is the password that protects your fill-alert endpoint from being hit by randos. **Save it — you'll need the same value in Step 8.** |

After saving them all, **redeploy** so the env vars actually load: **Deployments** tab → click the three-dot menu on the latest deployment → **Redeploy** → confirm. Takes ~30s.

### Step 8 — Test the endpoint manually

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

### Step 9 — Set up cron-job.org

1. Go to [cron-job.org](https://cron-job.org), sign up (free).
2. Click **Create cronjob**.
3. Fill in:
   - **Title:** `Polymarket fill alerts` (or whatever)
   - **URL:** the same URL you tested in Step 8 (including the `?key=...` part)
   - **Schedule:** every 1 minute (the smallest option on the free tier)
   - **Request method:** GET
   - **Enabled:** yes
4. Save.

Within 60 seconds you should see a "Successful — 200 OK" entry in the cron's execution history.

> If you see "Failed (HTTP error) 307 Temporary Redirect": your URL is using a domain that redirects. Try the `https://your-app.vercel.app` form Vercel gave you directly (not a custom domain).

### Step 10 — Real test: place a tiny order

Place any small order on Polymarket — even $0.10 worth. When it fills (whether that's instantly or hours later), within ~60s you'll get a `✅ FILLED` (or `💰 SOLD`) message from your Filled Bot.

**You're done.**

---

## After setup: things to know

- **The first cron tick snapshots all your existing open orders WITHOUT alerting.** This is intentional — otherwise the first run would spam you with every order already on the books. The *next* time any of those orders fills, you'll get a real alert.
- **New orders placed after setup** get the full milestone treatment: 25% / 50% / 75% / 100% partial-fill alerts as they chip away, or one ✅ FILLED message if they fill in a single shot.
- **Sells use different emoji:** 💰 SOLD (full) and 📤 partial, so you can tell at a glance whether a notification is an entry or an exit.
- **Cancellations are silent.** Polymarket apps already tell you when you cancel an order — you don't need a Telegram ping for it.
- **Edge case:** an order placed AND fully filled inside a single 60-second window may not generate an alert (the cron never saw it in an "open" state). The Polymarket app itself shows the fill in that case, so you'd see it there anyway.

---

## Troubleshooting

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

---

## Cost and limits at a glance

- **Vercel Hobby:** 100 GB-hours/month of serverless execution. This app uses ~5 GB-hours/month (1 tick/min × ~2s each). Plenty of headroom.
- **cron-job.org free:** unlimited jobs, 1-minute minimum interval, no cost.
- **Supabase free:** 500 MB database. This app's table stores ~200 bytes per order. You'd need to place ~2.5M orders before you'd care.
- **Telegram:** no cost, no rate limits at this volume.
- **Polymarket SDK:** no published rate limits, 1 call/min is well below anything that would matter.

**Vercel Hobby tier note:** Vercel's Terms of Service technically prohibit "commercial use" on the Hobby tier. A personal bet-tracker bot for yourself sits squarely in personal-use territory, but you should know the line exists.

---

## Files in this folder

- `app.py` — the Flask app (one endpoint plus health check)
- `requirements.txt` — Python dependencies
- `vercel.json` — Vercel deployment config
- `schema.sql` — Supabase table definition
- `.env.example` — env-var template for local testing
- `.gitignore` — don't commit `.env`
- `README.md` — this file
