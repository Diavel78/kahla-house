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
| M1 | Polymarket poller + Supabase writes | TODO — port existing tracker |
| M2 | DK scraper + event matcher (NFL) | TODO — needs live endpoint probe |
| M3 | Divergence engine + signals table | **Done (logic)** |
| M4 | Telegram alerter (single user) | **Done (send path)** |
| M5 | Multi-user subscriber system | **Done (fan-out + filters)**; `/start` bot command TODO |
| M6 | FD scraper | TODO |
| M7 | Dashboard on thekahlahouse.com | TODO — separate route in `kahla-house` repo |
| M8 | Expand sports beyond NFL | TODO |
| M9 | Per-market detail page | TODO |
| M10 | Kalshi integration | TODO |

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
