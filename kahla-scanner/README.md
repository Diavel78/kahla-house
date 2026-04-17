# Kahla Scanner

Prediction-market edge scanner. Compares public sportsbook lines (DK, FD) against
Polymarket sharp prices; alerts via Telegram when an edge clears configurable
thresholds; surfaces live signals on thekahlahouse.com.

See the full build spec in the parent `kahla-house` repo / CLAUDE's task prompt.

---

## Status

Scaffold only. Milestones:

| # | Milestone | Status |
|---|---|---|
| **M0** | **Log-only calibration + Brier score** | **Done (tooling)**; run for 2–3 weeks before M4 |
| M1 | Polymarket poller + Supabase writes | **Done** — BBO-mid poller + seed CLI |
| M2 | DK scraper + event matcher (NFL) | **Done** — robust parser (sportscontent + legacy shapes) |
| M3 | Divergence engine + signals table | **Done (logic)** |
| M4 | Telegram alerter (multi-user fan-out) | **Done**; gated behind M0 results |
| M6 | FD scraper | **Done** — content-managed-page parser |
| M7 | Dashboard on thekahlahouse.com | TODO — separate route in `kahla-house` repo |
| M8 | Expand sports beyond NFL | TODO |
| M9 | Per-market detail page | TODO |
| M10 | Kalshi integration | TODO |

> **Gate:** `SCANNER_MODE=log_only` is the default. Signal scan + Telegram
> fan-out stay off until M0 shows Poly actually predicts outcomes better
> than DK/FD devig'd consensus.

---

## Layout

```
kahla-scanner/
├── main.py                  # entrypoint: starts APScheduler and blocks
├── config.py                # dataclass reading .env
├── scrapers/                # DK, FD, Polymarket, Kalshi ingest
├── signals/                 # normalize (odds→prob), matcher, divergence
├── storage/                 # Supabase client + models
├── alerts/                  # Telegram send + in-memory dedup
├── jobs/scheduler.py        # APScheduler job wiring
├── supabase/schema.sql      # Postgres schema (run once in Supabase SQL editor)
├── systemd/                 # kahla-scanner.service
├── .env.example
└── requirements.txt
```

---

## Mobile-only quickstart (no Mac, no VPS)

GitHub Actions runs the scanner on a cron so you can drive the whole thing
from your phone.

### 1 — Add 4 GitHub secrets (one-time)

`github.com/Diavel78/kahla-house/settings/secrets/actions` → New repository secret:

| Name | Value |
|---|---|
| `SUPABASE_URL` | `https://xzzjpbervfoyaodduynb.supabase.co` |
| `SUPABASE_SERVICE_KEY` | your Supabase service_role key |
| `POLYMARKET_KEY_ID` | same as in Vercel |
| `POLYMARKET_SECRET_KEY` | same as in Vercel |

### 2 — Seed markets

**Easiest — auto-seed from your Poly positions.**
Actions tab → **Scanner — Auto-seed from Positions** → **Run workflow**.
Pulls every market you currently hold a Polymarket position in and registers
them for tracking (same credentials the Kahla House dashboard already uses).
Run it again whenever you make new bets.

**Or seed one at a time** → **Scanner — Seed Market** → Run workflow. Fill
in slug, sport, event, start, home_side. Useful for tracking games you
haven't bet on.

### 3 — Wait for the cron

The **Scanner — Poll** workflow fires every 30 min automatically. Or hit
Run workflow on that one to trigger immediately. It polls Poly BBO mids,
scrapes DK + FD, and runs the ESPN resolver.

### 4 — Watch `/scanner`

thekahlahouse.com/scanner starts filling in. "Last seen — Poly: 2m ago"
once the first workflow finishes.

Caveats:
- GitHub Actions private-repo free tier = 2,000 minutes/month. The 30-min
  cron uses ~1,500 min/month which fits. If you hit the limit, either
  stretch cron to hourly or enable metered billing (~$20/mo for unlimited).
- Cron resolution is "best effort" — runs may be delayed 5–15 min under
  GitHub load. Fine for M0 Brier calibration; not great for live alerts.
- For live alerting (`SCANNER_MODE=live`) once M0 results confirm the
  thesis, move to a VPS for reliable sub-minute cadence.

---

## Local dev

```bash
cd kahla-scanner
python3.11 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# fill in .env, then:
python main.py
```

The scheduler runs indefinitely. Ctrl-C to stop.

---

## Supabase setup

1. Create a new Supabase project (free tier).
2. Paste `supabase/schema.sql` into the SQL editor and run it.
3. Grab the service role key and project URL into `.env`.
4. The anon key is what thekahlahouse.com `/markets` page will use — only
   SELECT policies are exposed on public tables; `subscribers` and
   `alerts_log` remain service-only.

---

## Telegram bot setup

1. Create a bot via @BotFather, grab the token into `TELEGRAM_BOT_TOKEN`.
2. (Optional) DM the bot yourself to get your own `chat_id`, put it in
   `TELEGRAM_OPS_CHAT_ID` — ops failures (scraper down, etc.) alert only
   that chat.
3. For each subscriber, insert a row into `subscribers`:

```sql
insert into subscribers (telegram_chat_id, handle, display_name, sports,
                         min_edge_pct, min_liquidity_usd, timezone)
values (123456789, '@rob', 'Rob', array['NFL','CBB','MLB'],
        3.0, 500, 'America/Phoenix');
```

A `/start` command handler to self-register subscribers is planned but not
yet built.

---

## VPS deploy

See the build spec Section 10. Quick sketch:

```bash
# on the VPS
sudo useradd -m -s /bin/bash scanner
sudo mkdir -p /opt/kahla-scanner && sudo chown scanner:scanner /opt/kahla-scanner
sudo -u scanner git clone <repo> /opt/kahla-scanner
cd /opt/kahla-scanner/kahla-scanner     # the scanner subdir
sudo -u scanner python3.11 -m venv venv
sudo -u scanner ./venv/bin/pip install -r requirements.txt
sudo -u scanner cp .env.example .env && sudo -u scanner vi .env

sudo cp systemd/kahla-scanner.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now kahla-scanner
journalctl -u kahla-scanner -f
```

---

## M0 workflow — does the edge thesis hold?

The whole scanner rests on one premise: **Polymarket prices predict outcomes
better than DK/FD devig'd consensus** (at some time horizon before the event).
Structural prior is strong — sharps get limited on DK/FD, so pros migrate to
prediction markets — but we measure it instead of assuming.

### Step 1 — run log-only mode

Default `.env`:

```
SCANNER_MODE=log_only
```

The scheduler ingests everything (Poly ticks, DK/FD snapshots, market
linkage) but does NOT compute signals or send alerts. Let it run 2–3 weeks
across whatever sports are enabled. ~200 settled games per sport is the
rough minimum for Brier statistics to be meaningful.

### Step 2 — load outcomes

Populate `market_outcomes` for each settled game. Two ways:

**Manual / CSV** (fine for M0):

```bash
# outcomes.csv:
# market_id,winning_side
# 8f2a...,home
# 9b1c...,away
python -m analytics.outcomes from-csv outcomes.csv
```

**Automated**: wire a scores API or Polymarket resolution feed to
`db.upsert_outcome(market_id, winning_side, source=...)`. Out of scope for M0.

### Step 3 — score

```bash
python -m analytics.brier --sport NFL --days 30
```

Output looks like:

```
Brier scores — 214 settled markets

POLY  T-24h: 0.2180 (n=203)  T-6h: 0.2091 (n=210)  T-1h: 0.2045 (n=213)  T-0h: 0.2031 (n=214)
  DK  T-24h: 0.2245 (n=198)  T-6h: 0.2162 (n=207)  T-1h: 0.2088 (n=212)  T-0h: 0.2049 (n=214)
  FD  T-24h: 0.2251 (n=195)  T-6h: 0.2168 (n=205)  T-1h: 0.2094 (n=211)  T-0h: 0.2053 (n=213)

Best source per checkpoint:
  T-24h: POLY (Brier 0.2180, n=203)
  T-6h:  POLY (Brier 0.2091, n=210)
  T-1h:  POLY (Brier 0.2045, n=213)
  T-0h:  POLY (Brier 0.2031, n=214)
```

### Step 4 — decide

- **Poly wins every checkpoint by a meaningful margin (>0.005)**: thesis
  confirmed. Flip `SCANNER_MODE=live`. Tune `MIN_EDGE_PCT_GLOBAL` so only
  the top ~10% of divergences (by Brier residual) would have fired.
- **Poly ties DK/FD**: they're parallel sharp markets. Divergence scanner is
  hunting noise. Pivot to a different thesis (cross-venue arb,
  DK-specific book lag, news-latency).
- **Poly loses**: edge is the other direction — fade Poly, take DK/FD. Rare
  but possible on low-volume markets with inelastic order books.

Dump per-market CSV to inspect which games dominate the score:

```bash
python -m analytics.brier --sport NFL --days 30 --csv nfl_brier.csv
```

---

## Design notes

- **Single process, single scheduler.** Keeps deploy and observability simple.
  Scale out only when one of the scrapers starts missing its interval — cheap
  VPS handles NFL-scale easily.
- **Append-only tick / snapshot tables.** Makes backtesting trivial later and
  avoids update contention. Truncate via scheduled Supabase function if size
  becomes a cost issue.
- **Dedup has two layers.** `alerts/dedup.py` is an in-memory guard for
  scheduler retries within one process; `alerts_log`'s unique index on
  `(signal_id, subscriber_id)` is the durable cross-restart guard.
- **Depth injection.** `scrapers/polymarket.py` registers a closure on
  `signals.divergence.poly_book_depth` so the divergence engine can read live
  Poly order-book depth without importing the scraper (avoids cycles).
- **Quiet hours are per-subscriber.** Respect subscriber timezone, not the
  VPS clock.
