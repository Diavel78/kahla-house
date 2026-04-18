# Kahla Scanner — Session Handoff

> **Current state (2026-04-18 evening):** Scanner is live and self-running.
> GitHub Actions cron runs `python -m scrapers.owls` every 5 minutes,
> pulls odds for 9 books in a single API call per sport, and persists to
> Supabase. The N-book Brier pipeline is in place and reads from
> `book_snapshots` for every book including Polymarket. Scanner page at
> [thekahlahouse.com/scanner](https://thekahlahouse.com/scanner) renders
> Activity + "Currently Tracking" + 9-book Brier table + Signals +
> Matched/Unmatched.
>
> **Start next session by:** running the triage queries in §5 to confirm
> cron cadence + book_snapshot growth + first settled-game Brier numbers
> from tonight's slate. Then look at actual Poly→public divergence on the
> dashboard.

---

## 1. Architecture at a glance

```
Owls Insight API  ─(/{sport}/odds every 5min)─►  GitHub Actions cron
                                                       │
                                                       ▼
                                          scrapers/owls.py
                                          • parse 13 books per game
                                          • match/create markets row
                                          • dedup: skip no-op inserts
                                                       │
                                                       ▼
                                               Supabase Postgres
                                               • markets (upserts)
                                               • book_snapshots (append)
                                               • market_outcomes (ESPN)
                                                       │
                                                       ▼
                                            Vercel Flask app
                                            • /scanner page (admin)
                                            • /api/scanner/* endpoints
                                            • reads Supabase, renders UI
```

**No VPS dependency in the active path.** The DigitalOcean droplet at
`24.199.119.210` is still running but writes to `poly_ticks` separately
(legacy); nothing on the dashboard depends on it. Safe to cancel any
time before day-60 of the DO free trial.

---

## 2. Live infrastructure (as of 2026-04-18)

### GitHub Actions workflow — primary ingest

- File: [`.github/workflows/scanner-poll.yml`](../.github/workflows/scanner-poll.yml)
- Cron: `*/5 * * * *` (every 5 min)
- Two steps:
  1. `python -m scrapers.owls` — hits `/{sport}/odds` for each sport,
     dedupes, persists `book_snapshots`.
  2. `python -m analytics.resolve` — ESPN scoreboard → `market_outcomes`.
- Runtime: ~50 seconds per run.
- Secrets set: `OWLS_INSIGHT_API_KEY`, `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`.

**Budget check:** 5 sports × 288 runs/day = **1,440 Owls API calls/day ≈
44K/month**. Owls MVP+ plan = 300K/month. ~15% utilization.

### Books tracked (9 total, in display order)

| Code | Book | Role |
|---|---|---|
| POLY | Polymarket | Prediction market (thesis: sharpest) |
| PIN | Pinnacle | Sharp public (sharp baseline) |
| CIR | Circa | Sharp Vegas |
| DK | DraftKings | Big retail |
| FD | FanDuel | Big retail |
| MGM | BetMGM | Retail |
| CAE | Caesars | Retail |
| HR | Hardrock | Retail |
| NVG | Novig | Novel/exchange |

Books returned by Owls but **deliberately skipped**: wynn, westgate,
south_point, stations (low-signal Vegas). Add back via `BOOK_CODES` in
`scrapers/owls.py` if ever useful.

### Supabase — `xzzjpbervfoyaodduynb.supabase.co`

- `markets` — event records, upserted by matcher. 220+ active.
- `book_snapshots` — append-only odds snapshots. Primary Brier source.
- `market_outcomes` — ESPN-resolved outcomes. 17+ rows and growing.
- `team_aliases` — 434 rows (MLB 99 / NBA 104 / NHL 117 / NFL 114).
  Applied to live DB earlier today via Supabase SQL editor.
- `unmatched_markets` — 340 open; pre-Owls accumulation, mostly harmless.
- `poly_ticks` — legacy, written only by the (retiring) VPS.

### Operating

```bash
# Trigger a run manually
gh workflow run scanner-poll.yml --ref main

# Check recent runs
gh run list --workflow=scanner-poll.yml --limit 5

# View the latest run's logs
gh run view $(gh run list --workflow=scanner-poll.yml --limit 1 --json databaseId --jq '.[0].databaseId') --log
```

---

## 3. What's working (end-to-end)

- **Owls ingest** — confirmed in production: NBA 392 + MLB 590 + NHL 410
  snapshots per run in initial testing. Dedup cuts steady-state writes
  by ~85-90% (run 2 after run 1 wrote 0 rows).
- **Dedup logic** — `_dedup_unchanged()` in `scrapers/owls.py` suppresses
  inserts where `(price_american, line)` equals the last recorded value
  per `(market_id, book, market_type, side)` within a 30-min lookback.
  Drops storage from ~80MB/day to ~10MB/day.
- **Market matching** — SDK-based discover retired; `_find_or_create_market`
  uses existing team aliases + ±30 min window to reuse existing `markets`
  rows when possible, falls back to creating a fresh row.
- **ESPN resolver** — `analytics/resolve.py` writes `market_outcomes` for
  MLB/NBA/NHL/NFL hourly (via the same cron, piggybacked).
- **Brier pipeline (N-book)** — `scanner.py::brier()` (Flask side) and
  `kahla-scanner/analytics/brier.py` (CLI) both score all 9 books at
  T-24h / T-6h / T-1h / T-0 from `book_snapshots`. Winner at each
  checkpoint requires `n ≥ 5` (prevents lucky-one-game spurious winners).
- **Scanner page** ([thekahlahouse.com/scanner](https://thekahlahouse.com/scanner)):
  - Activity card: totals, last-seen per book (all 9 + legacy poly_ticks)
  - "Currently tracking" banner: count of in-flight games by sport
  - Brier table: 9 rows × 4 horizons, green = lowest Brier at that horizon
  - Recent signals, matches, unmatched — all live
- **Team aliases** — 434 rows seeded; matcher resolves abbreviations
  correctly (`NY Mets` → `new york mets`).

---

## 4. Brier methodology (non-obvious stuff to remember)

### What we compute

For each settled market, for each of 9 books, at each of 4 checkpoints:

1. **Fetch the latest book_snapshot moneyline** (home + away) in a
   ±30 min window around the checkpoint time.
2. **Devig** using `devig_two_way(home_prob, away_prob)` to remove the
   book's margin, yielding a clean home-win probability.
3. **Squared error** vs. outcome: `(predicted − actual)²`, where
   `actual = 1.0` if home won, `0.0` if away won, skip voids.
4. **Mean over games** = that book's Brier at that checkpoint. Lower is
   sharper.

### Why it can look empty when data is flowing

Brier runs only on **settled** markets (games with an outcome). A game in
progress has pre-game snapshots being captured but doesn't appear in the
table until ESPN resolves it. The "Currently tracking" banner shows games
in the pipeline.

The 17 settled markets that existed at Owls cutover had zero pre-game
snapshots (they finished before Owls turned on), so they'll show `—`
in every cell forever. Games settling from 2026-04-18 evening onward
will populate real Brier numbers.

### Why winner requires `n ≥ 5`

Without the minimum, a book with a single game where it got the outcome
right would "win" at Brier ≈ 0.03, overshadowing books with 50 games and
Brier 0.18. That's meaningless. Once a book has 5+ games it's at least
a statistically-comparable baseline.

### Configuration

- Checkpoints: `[24, 6, 1, 0]` hours before `event_start`
- Tolerance window around each checkpoint: ±30 min
- Books scored: `BRIER_BOOKS` in `scanner.py`, `BOOKS` in
  `kahla-scanner/analytics/brier.py`. Keep these lists in sync.

---

## 5. Triage queries (Supabase SQL editor)

```sql
-- Ingest cadence: max captured_at should be < 6 min ago
select max(captured_at) from book_snapshots;

-- Per-book freshness + volume
select book, count(*) as rows_7d, max(captured_at) as last_seen
from book_snapshots
where captured_at > now() - interval '7 days'
group by book
order by rows_7d desc;

-- Per-sport coverage last hour
select m.sport, count(*)
from book_snapshots s
join markets m on m.id = s.market_id
where s.captured_at > now() - interval '1 hour'
group by m.sport
order by count(*) desc;

-- Total storage footprint
select pg_size_pretty(pg_total_relation_size('book_snapshots'));

-- Markets state
select status, count(*) from markets group by status;

-- Games currently tracking (in flight, no outcome yet)
select m.sport, count(*)
from markets m
left join market_outcomes o on o.market_id = m.id
where m.event_start between now() - interval '6 hours' and now() + interval '48 hours'
  and m.status = 'active'
  and o.market_id is null
group by m.sport;

-- Outcome flow
select source, count(*), max(resolved_at)
from market_outcomes
group by source;
```

### Starting Brier analysis once settled games accumulate

```bash
cd kahla-scanner
python -m analytics.brier --sport MLB --days 3
python -m analytics.brier --days 7            # all sports
python -m analytics.brier --days 30 --csv /tmp/brier.csv   # dump per-game
```

Or just refresh [thekahlahouse.com/scanner](https://thekahlahouse.com/scanner)
and let the Flask endpoint render the table.

---

## 6. Open decisions + next session backlog

### 1. Decision pending: keep or cancel the VPS

DigitalOcean droplet `24.199.119.210` is still running the old APScheduler
pipeline writing `poly_ticks` at 45s cadence. Nothing on the dashboard
reads `poly_ticks` anymore. Keeping it costs $4/mo after day 60 of the
DO free trial (~2026-06-17).

- **Cancel it** if you trust 5-min Owls snapshots for Poly (fine for
  T-24h/T-6h/T-1h/T-0 horizons).
- **Keep it** if you specifically want high-resolution Poly tick data for
  short-term movement modeling later.

My lean: cancel. The scanner works without it.

### 2. Storage retention (not urgent)

- Current rate: ~10MB/day with dedup.
- Free-tier runway: ~50 days.
- Plan when we get there: either (a) drop a `delete from book_snapshots
  where captured_at < now() - interval '30 days'` daily SQL cron, or
  (b) upgrade Supabase to Pro ($25/mo, 8GB).
- Do NOT enable retention until confident Brier pipeline is correct —
  leave raw data intact for the first 2 weeks of validation.

### 3. Signal emission is off

`signals` table is still empty. `jobs/scheduler.py::job_scan_signals`
exists but runs on the VPS only. Once we have 3-5 days of book_snapshot
coverage, turn on a divergence scan job (probably as a new GH Actions
workflow, not the VPS) that:

- For each market where POLY and DK/FD/PIN disagree by N percentage
  points, insert a row into `signals`.
- Optionally fan out to Telegram via `alerts/telegram.py`.

Config lives in `config.py::scanner_mode`. Currently `log_only`. Flip to
`alert_enabled` once we trust the Brier calibration.

### 4. Render: settled-markets list with per-book Brier per game

The current Brier table is an aggregate. When we have ~50 settled markets
it'd be useful to show the per-game breakdown so we can spot-check
calibration: "this game, POLY said 60% home, DK said 58%, home lost —
both books wrong by about the same amount." That's the CSV dump the CLI
already produces. A page-side table would be natural to add.

### 5. Props (later)

Owls exposes `/{sport}/props`. Same pattern as odds. Not in M0 scope.

---

## 7. Session log — what changed today (2026-04-18)

Chronological, newest last:

| Commit | Topic |
|---|---|
| `1152eff` | install_vps.sh fixes (swap, perms, tty reads) + HANDOFF refresh |
| `49e6968` | Drop FanDuel direct scraper from scheduler (FD gated behind authed GraphQL) |
| `df35bcb` | Pivot ingest to Owls Insight — 5-min cron, 13 books per call |
| `3f9413e` | HANDOFF reflects Owls architecture + VPS-retire decision |
| `67a2d52` | owls.py dedup — skip no-op inserts, 85-90% write reduction |
| `f239a64` | Brier grades all 9 books (POLY/PIN/CIR/DK/FD/MGM/CAE/HR/NVG) |
| `4771a0e` | scanner.html always renders all 9 rows (don't hide empty) |
| `9a86331` | "Currently tracking" banner above Brier table |
| _this commit_ | HANDOFF comprehensive rewrite |

Also applied today (not in git):
- `team_aliases.sql` seeded into Supabase (434 rows).
- `OWLS_INSIGHT_API_KEY` added to GitHub Actions secrets.
- DigitalOcean droplet spun up + installer run (then rendered redundant
  by the Owls pivot — VPS still running, retire when ready).
- Permissions allowlist added to `.claude/settings.json` (gitignored).

---

## 8. Key files reference

| File | Purpose |
|---|---|
| `kahla-scanner/scrapers/owls.py` | **Primary ingest** — Owls → book_snapshots |
| `.github/workflows/scanner-poll.yml` | 5-min cron workflow |
| `kahla-scanner/analytics/brier.py` | Brier CLI (9 books) |
| `kahla-scanner/analytics/resolve.py` | ESPN → market_outcomes |
| `kahla-scanner/signals/matcher.py` | Cross-venue name linkage |
| `kahla-scanner/signals/normalize.py` | Odds conversion helpers |
| `kahla-scanner/storage/supabase_client.py` | DB helpers |
| `kahla-scanner/storage/models.py` | Row dataclasses |
| `kahla-scanner/supabase/schema.sql` | DB schema (idempotent) |
| `kahla-scanner/supabase/team_aliases.sql` | 434 alias rows (idempotent) |
| `scanner.py` (root) | Flask-side read layer for `/scanner` page |
| `templates/scanner.html` | `/scanner` UI |
| `app.py` (root) | Flask app (routes `/api/scanner/*`) |
| **Legacy (retiring with VPS):** | |
| `kahla-scanner/main.py` | VPS entrypoint |
| `kahla-scanner/jobs/scheduler.py` | APScheduler wiring |
| `kahla-scanner/scrapers/polymarket.py` | SDK-based Poly poller |
| `kahla-scanner/scrapers/draftkings.py` | Direct DK scraper (403s from VPS) |
| `kahla-scanner/scrapers/fanduel.py` | Direct FD scraper (authed API now) |
| `kahla-scanner/systemd/kahla-scanner.service` | VPS systemd unit |
| `kahla-scanner/scripts/install_vps.sh` | VPS installer (keep for future use) |

---

## 9. Env vars & secrets

### GitHub Actions secrets (cron workflow)

- `OWLS_INSIGHT_API_KEY`
- `SUPABASE_URL`
- `SUPABASE_SERVICE_KEY`

### Vercel env vars (Flask app)

- `SUPABASE_URL`
- `SUPABASE_SERVICE_KEY`
- `OWLS_INSIGHT_API_KEY` (used by `/api/odds` etc, not scanner)
- `POLYMARKET_KEY_ID`, `POLYMARKET_SECRET_KEY` (dashboard page)
- `FIREBASE_SERVICE_ACCOUNT`, `FLASK_SECRET_KEY`

### Local `.env` files

- Repo root `.env`: `OWLS_INSIGHT_API_KEY`, `POLYMARKET_*`
- `kahla-scanner/.env`: `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`
