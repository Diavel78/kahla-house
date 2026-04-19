# Kahla Scanner — Session Handoff

> **Current state (end of 2026-04-18):** Scanner is fully live and autonomous.
> External cron-job.org fires `workflow_dispatch` every 5 minutes →
> GitHub Actions runs `scrapers/owls.py` → 9 books land in Supabase →
> signal detector scans for POLY+PIN sharp consensus vs public lag.
> Scanner page at [thekahlahouse.com/scanner](https://thekahlahouse.com/scanner)
> auto-refreshes (60s Activity+Signals, 5min Brier+tables). VPS is destroyed.
> $0 ongoing cost.
>
> **Next session should start by:** glancing at the scanner page. If
> book timestamps are all ≤5 min and Brier rows for POLY are filling in
> (was artifact-low due to the 24h-offset bug we just fixed, expected to
> normalize as new games settle), pipeline is healthy. Look at the
> signals table — any fires? If yes, review quality. If no after 24-48h,
> loosen threshold. Then pick from §8 open backlog.

---

## 1. Architecture at a glance

```
cron-job.org  ─(POST dispatch every 5min)─►  GitHub Actions workflow
                                                     │
                                              scanner-poll.yml runs:
                                              1. scrapers/owls.py
                                              2. analytics/resolve.py
                                              3. signals/divergence.py
                                                     │
                                                     ▼
                                              Supabase Postgres
                                              • markets, book_snapshots
                                              • market_outcomes, signals
                                              • team_aliases, unmatched
                                                     │
                                                     ▼
                                              Vercel Flask app
                                              • /scanner page (admin)
                                              • auto-refresh every 60s/5min
```

**Zero always-on infrastructure we own.** Everything runs on free tiers.

---

## 2. Live infrastructure

### Triggering (2-layer for reliability)

| Layer | Who fires it | Cadence | Purpose |
|---|---|---|---|
| **Primary** | cron-job.org | every 5 min | Reliable fine-grained cadence |
| **Fallback** | GitHub Actions native `schedule:` | every 30 min (drifts to 30-45) | Runs if cron-job.org is down |

The fallback lives in [.github/workflows/scanner-poll.yml](../.github/workflows/scanner-poll.yml)'s `schedule:` block. The primary lives in cron-job.org account (logged-in user's dashboard). Both trigger the same workflow. `concurrency.group: scanner-poll` prevents overlapping runs; dedup prevents duplicate writes either way.

**cron-job.org config:**
- URL: `https://api.github.com/repos/Diavel78/kahla-house/actions/workflows/scanner-poll.yml/dispatches`
- Method: POST, body `{"ref":"main"}`
- Headers: `Authorization: Bearer <github_pat>`, `Accept: application/vnd.github+json`, `X-GitHub-Api-Version: 2022-11-28`, `Content-Type: application/json`
- GitHub PAT: fine-grained, repo = Diavel78/kahla-house, permission = Actions (Read and write). **Token expires in ~1 year — rotate around 2027-04-18.**

### GitHub Actions workflow

[.github/workflows/scanner-poll.yml](../.github/workflows/scanner-poll.yml) runs 3 steps:

1. **Ingest odds (`python -m scrapers.owls`)** — hits Owls `/{sport}/odds` for each sport in `SPORTS_ENABLED`, collapses 13 books into unified games (with POLY 24h-offset normalization), dedupes against recent snapshots, persists `book_snapshots`.
2. **Resolve outcomes (`python -m analytics.resolve`)** — ESPN scoreboard → `market_outcomes` rows for settled MLB/NBA/NHL/NFL games.
3. **Signal scan (`python -m signals.divergence`)** — detects POLY+PIN consensus vs DK/FD/MGM public lag, writes to `signals` table. No Telegram yet.

Runtime: typically 45-60 seconds per fire. 5 sports × 288 fires/day = ~1,440 Owls API calls/day (well under the 300K/month Owls budget).

### Books tracked (9, in display order)

| Code | Book | Role |
|---|---|---|
| POLY | Polymarket | Prediction market — sharp thesis |
| PIN | Pinnacle | Sharp public |
| CIR | Circa | Sharp Vegas |
| DK | DraftKings | Public/retail |
| FD | FanDuel | Public/retail |
| MGM | BetMGM | Retail |
| CAE | Caesars | Retail |
| HR | Hardrock | Retail |
| NVG | Novig | Novel exchange |

Skipped (returned by Owls but low-signal): wynn, westgate, south_point, stations. Add back via `BOOK_CODES` in [scrapers/owls.py](scrapers/owls.py).

### Supabase — `xzzjpbervfoyaodduynb.supabase.co`

Free tier (500MB storage). Tables:

- `markets` — event records (sport, teams, start, poly_market_id).
- `book_snapshots` — odds snapshots. **Every Brier + signal query reads this.** Dedup-throttled to ~10MB/day (free tier runway ~50 days; set up retention before hitting limits).
- `market_outcomes` — ESPN-resolved (home/away/void).
- `signals` — sharp_consensus + future signal types. Empty until thresholds are hit.
- `team_aliases` — 434 rows, maps book shorthand to canonical team names.
- `unmatched_markets` — games the matcher couldn't link; mostly pre-Owls-era backlog.
- `subscribers` — Telegram chat_ids + per-user filters. Empty; used in Phase 2.
- `alerts_log` — Telegram send dedup. Empty; used in Phase 2.
- `poly_ticks` — **legacy**, last VPS-era rows from 2026-04-18 afternoon. ~2MB archive. Not read by any current code. Safe to `DROP TABLE` at any time.

### Vercel Flask app

Serves `thekahlahouse.com`. Scanner-specific routes:

- `GET /scanner` — admin-gated page, renders from `templates/scanner.html`
- `GET /api/scanner/activity` — counts + last_seen per book + tracking breakdown
- `GET /api/scanner/brier` — per-book Brier at T-24h/T-6h/T-1h/T-0
- `GET /api/scanner/signals` — recent signals
- `GET /api/scanner/matches` — recently linked markets
- `GET /api/scanner/unmatched` — needs-review backlog

Auto-refresh behavior:
- Activity + Signals: every 60 seconds
- Brier + Matches + Unmatched: every 5 minutes
- Paused when tab is in background; immediate catch-up refresh on tab visibility restore

### External services + tokens

| Service | Purpose | Credentials | Rotation |
|---|---|---|---|
| **Supabase** | Postgres + REST API | `SUPABASE_URL` + `SUPABASE_SERVICE_KEY` | only if suspected leak |
| **Owls Insight** | Aggregated odds feed for 13 books | `OWLS_INSIGHT_API_KEY` | managed by user's MVP+ subscription ($50/mo, already paid) |
| **GitHub PAT** | cron-job.org → workflow dispatch | Stored in cron-job.org job headers only | **~2027-04-18** (1 year from creation) |
| **cron-job.org** | Reliable 5-min trigger | Email/password account | — |
| **Firebase Auth** | Admin gate on /scanner | `FIREBASE_SERVICE_ACCOUNT` | only if suspected leak |

---

## 3. What's working

- **Ingest at 5-min cadence** — cron-job.org verified firing; GH workflow success rate 100% during testing.
- **All 9 books writing to `book_snapshots`**. Dedup ~85-90% reduction in no-op writes.
- **POLY + PIN consensus merge** — after fixing the 24h offset bug in `parse_games`, POLY and PIN for the same real-world game now land on the same `markets` row.
- **Brier scoring** covers all 9 books at 4 horizons; min-n=5 to qualify as "winner" prevents lucky-single-game artifacts.
- **Signal detector (Phase 1)** live: `sharp_consensus` type. Requires POLY+PIN agreement within 1.5pp, edge ≥3pp vs public avg, game within next 2h, 30-min dedup. Thresholds env-overridable.
- **ESPN resolver** — `market_outcomes` actively populating.
- **Auto-refreshing scanner page** + "Currently tracking" banner showing in-flight games.

---

## 4. Brier methodology (non-obvious stuff)

### What we compute per game × book × checkpoint

1. Fetch latest `book_snapshots` moneyline (home + away) within ±30 min of checkpoint time.
2. `devig_two_way(home_prob, away_prob)` — removes book margin, returns clean home-win probability.
3. Squared error vs. outcome: `(predicted − actual)²` where `actual = 1.0` if home won, else `0.0`. Skip voids.
4. Mean over games = book's Brier at that checkpoint. Lower is sharper.

### Why table can look empty even with live ingest

Brier only scores **settled** markets (game has ended + `market_outcomes` row exists). Games in progress are tracked (pre-game snapshots captured) but don't appear in the table until ESPN resolver writes their outcome.

The "Currently tracking" banner shows the in-flight count by sport — those games will populate Brier rows over the next few hours as they settle.

### Why winner requires n ≥ 5

One lucky game can produce Brier ≈ 0.03. Without a minimum, it'd beat a book with 50 games at Brier 0.17. Requires at least 5 scored games before a book can "win" a checkpoint.

### Known artifact: POLY under-represented initially

Before 2026-04-18 ~18:00 UTC, POLY snapshots landed on different market rows than PIN/DK/FD for the same game (Owls uses Polymarket's resolution date = game date + 1 as commence_time, and our parse was keying by eventId). Fixed in commit `cf422de`. Games that settled before the fix show POLY n=0 at most horizons. New games settling post-fix will have normal POLY coverage — expect parity with other books by end of 2026-04-20.

### Configuration

- `CHECKPOINTS_HOURS = [24, 6, 1, 0]` in both `scanner.py` and `analytics/brier.py`
- Tolerance window: ±30 min around each checkpoint
- Books scored: `BRIER_BOOKS` in `scanner.py`, `BOOKS` in `analytics/brier.py`. **Keep in sync.**

---

## 5. Signal methodology (Phase 1)

### sharp_consensus signal

Emits when ALL of these are true:

| Condition | Default | Env var |
|---|---|---|
| `event_start` within next 2 hours | 2.0 hr | `SHARP_CONSENSUS_LOOKAHEAD_HOURS` |
| POLY + PIN both have fresh ML snapshots (within 10 min) | 10 min | `SHARP_CONSENSUS_FRESHNESS_MIN` |
| `\|POLY - PIN\|` ≤ 1.5pp | 1.5 pp | `SHARP_CONSENSUS_AGREE_MAX_PCT` |
| ≥2 of {DK, FD, MGM} have fresh ML snapshots | 2 books | `MIN_PUBLIC_BOOKS` (code) |
| `\|sharp_avg - public_avg\|` ≥ 3pp | 3.0 pp | `SHARP_CONSENSUS_EDGE_MIN_PCT` |
| No prior signal for this market in last 30 min | 30 min | `SHARP_CONSENSUS_DEDUP_MIN` |

All env vars set via GitHub Actions **Variables** (Settings → Secrets and variables → Actions → Variables tab) — edit there, no code change needed.

### Signal row shape

- `signal_type = "sharp_consensus"`
- `fade_side` = which side to bet (home if sharp > public, away otherwise)
- `public_prob` = public avg on fade side
- `sharp_prob` = sharp avg on fade side
- `edge_pct` = abs percentage-point gap
- `notes` = full drilldown (per-book probs, POLY-PIN agreement pp, minutes to event)

### CLI

```bash
cd kahla-scanner
python -m signals.divergence               # real insert
python -m signals.divergence --dry-run     # log would-fire, don't insert
```

---

## 6. Triage queries (Supabase SQL editor)

```sql
-- Ingest cadence: max captured_at should be < 6 min ago
select max(captured_at) from book_snapshots;

-- Per-book 7-day volume
select book, count(*) as rows_7d, max(captured_at) as last_seen
from book_snapshots
where captured_at > now() - interval '7 days'
group by book order by rows_7d desc;

-- Games currently tracking (in flight, no outcome yet)
select m.sport, count(*)
from markets m
left join market_outcomes o on o.market_id = m.id
where m.event_start between now() - interval '6 hours' and now() + interval '48 hours'
  and m.status = 'active' and o.market_id is null
group by m.sport order by count(*) desc;

-- Signals fired today
select signal_type, count(*), min(triggered_at), max(triggered_at)
from signals where triggered_at > now() - interval '24 hours'
group by signal_type;

-- Storage footprint
select pg_size_pretty(pg_total_relation_size('book_snapshots')) as book_snaps,
       pg_size_pretty(pg_total_relation_size('markets')) as markets,
       pg_size_pretty(pg_database_size(current_database())) as total;
```

### Starting Brier analysis

Once ~24h of post-Owls data has accumulated:

```bash
cd kahla-scanner
python -m analytics.brier --sport MLB --days 3
python -m analytics.brier --days 7
python -m analytics.brier --days 30 --csv /tmp/brier.csv
```

The `/scanner` page renders the same info.

---

## 7. Common operations

### Tune a signal threshold without deploying code

GitHub → Settings → Secrets and variables → Actions → **Variables** tab → set e.g. `SHARP_CONSENSUS_EDGE_MIN_PCT=2.5`. Takes effect on the next cron fire (within 5 min).

### Pause the entire scanner

- Turn off cron-job.org job (toggle to disabled — takes effect immediately).
- GitHub schedule fallback still fires every 30 min unless you also comment out the `schedule:` block in scanner-poll.yml.

### Resume after pause

- Flip cron-job.org back on. Data resumes flowing on next fire.

### Trigger a one-off run manually

```bash
gh workflow run scanner-poll.yml --ref main
gh run list --workflow=scanner-poll.yml --limit 2
```

### Rotate the GitHub PAT

1. Generate new fine-grained PAT at https://github.com/settings/personal-access-tokens/new (repo = Diavel78/kahla-house, Actions: Read and write).
2. In cron-job.org → Edit job → Headers → update `Authorization` value to `Bearer <new_token>`.
3. Revoke old PAT.

### Rename a book or add a new one

1. Add the book to `BOOK_CODES` in [scrapers/owls.py](scrapers/owls.py).
2. Add the book code to `BRIER_BOOKS` in [scanner.py](../scanner.py) and `BOOKS` in [analytics/brier.py](analytics/brier.py).
3. Add a CSS color class in [templates/scanner.html](../templates/scanner.html) (`.src-tag.xxx`).
4. Commit, push. No Supabase changes needed.

### Retire old data (when nearing Supabase free tier)

Not yet wired. When needed:

```sql
delete from book_snapshots where captured_at < now() - interval '30 days';
```

Run once, or schedule as a weekly Supabase edge function. Don't enable until Brier pipeline has been validated for at least 2 weeks.

### Destroy the VPS (already done 2026-04-18)

Was at `24.199.119.210`. Destroyed via DO dashboard. Credit card charge: $0 (within $200/60-day trial). Artifacts remaining: `poly_ticks` table in Supabase (~2MB legacy archive).

---

## 8. Open decisions + next session backlog

### 1. Phase 2 — Telegram alerts (unblocks once signals accumulate)

Once the `signals` table has fired a few entries and you've reviewed them:

1. Create a Telegram bot (`@BotFather` → `/newbot`, save token).
2. DM the bot once, find your chat_id via `https://api.telegram.org/bot<TOKEN>/getUpdates`.
3. Insert subscriber row:
   ```sql
   insert into subscribers (telegram_chat_id, display_name, sports, min_edge_pct)
   values (<your_chat_id>, 'me', array['MLB','NBA','NHL','NFL','CBB'], 3.0);
   ```
4. Add `TELEGRAM_BOT_TOKEN` to GH Actions secrets.
5. Wire `alerts/telegram.py::fan_out()` into the signal-scan step (or as a 4th step). Loop over open signals, fan out, log to `alerts_log` for dedup.

Estimate: ~1 hour.

### 2. Signal type expansion (Phase 3)

- **Velocity / movement**: POLY moved >2pp in last 30 min, public hasn't followed. Useful for today's last-hour-before-game moves where POLY may be the only sharp source available (PIN lines open same-day).
- **RLM (reverse line movement)**: Line moved against Circa handle %. Needs `/{sport}/splits` ingest into a new `splits_snapshots` table.
- **Poly-only signal variant**: For games where PIN hasn't opened yet (tomorrow's slate before morning), emit a weaker `poly_vs_public` signal using POLY alone. Would catch pre-game-day divergence the strict `sharp_consensus` misses.

### 3. Storage retention

Decide in ~40 days when free tier is ~50% full. Options: 30-day retention cron, or upgrade to Supabase Pro ($25/mo, 8GB).

### 4. Per-game Brier breakdown view

Current Brier table is aggregate. A per-game drill-down ("for this specific MLB game, POLY said 60%, DK said 58%, home lost — here's each book's miss") would help validate calibration. Data already exists in the CSV dump — just needs a new Flask endpoint + modal.

### 5. Settle / outcome audit

`market_outcomes.source` tracks where outcome came from (currently only "espn"). Worth building a "check for disagreement" job that queries Polymarket's resolution API and flags any outcome where Poly resolved differently than ESPN — these are either voids, weird conditions, or data errors.

### 6. Props (later)

Owls exposes `/{sport}/props`. Same ingest pattern as odds. Not in M0 scope; revisit once moneyline Brier + signals are proven.

### 7. Cleanup / tech debt

- `poly_ticks` table can be `DROP`ped at any time (~2MB, zero code reads it).
- `kahla-scanner/scrapers/polymarket.py`, `draftkings.py`, `fanduel.py` are legacy VPS-era direct scrapers, fully superseded by `scrapers/owls.py`. Delete whenever.
- `kahla-scanner/main.py`, `kahla-scanner/jobs/scheduler.py`, `systemd/kahla-scanner.service` — VPS entry points, no longer reachable. Delete whenever.
- `kahla-scanner/scripts/install_vps.sh` + `install_launchd.sh` — keep; useful reference if we ever need a VPS or local scheduler again.

---

## 9. Key files reference

### Active path (ingest → persist → query)

| File | Purpose |
|---|---|
| [kahla-scanner/scrapers/owls.py](scrapers/owls.py) | **Primary ingest** — Owls → book_snapshots. Handles POLY 24h offset. |
| [kahla-scanner/signals/divergence.py](signals/divergence.py) | Signal detector: sharp_consensus |
| [kahla-scanner/analytics/resolve.py](analytics/resolve.py) | ESPN → market_outcomes |
| [kahla-scanner/analytics/brier.py](analytics/brier.py) | Brier scorer (CLI), N books |
| [kahla-scanner/signals/matcher.py](signals/matcher.py) | Cross-venue name linkage |
| [kahla-scanner/signals/normalize.py](signals/normalize.py) | Odds conversion (american↔prob, devig) |
| [kahla-scanner/storage/supabase_client.py](storage/supabase_client.py) | DB helpers |
| [kahla-scanner/storage/models.py](storage/models.py) | Row dataclasses |
| [kahla-scanner/supabase/schema.sql](supabase/schema.sql) | DB schema (idempotent) |
| [kahla-scanner/supabase/team_aliases.sql](supabase/team_aliases.sql) | 434 alias rows (idempotent) |
| [.github/workflows/scanner-poll.yml](../.github/workflows/scanner-poll.yml) | Cron workflow, 3 steps |
| [scanner.py](../scanner.py) | Flask-side read layer for /scanner page |
| [templates/scanner.html](../templates/scanner.html) | /scanner UI + auto-refresh JS |
| [app.py](../app.py) | Flask app mounting /api/scanner/* |

### Legacy / retiring (safe to delete)

| File | Why retained |
|---|---|
| [kahla-scanner/main.py](main.py) | VPS entry point — VPS destroyed, not reachable |
| [kahla-scanner/jobs/scheduler.py](jobs/scheduler.py) | APScheduler for VPS |
| [kahla-scanner/scrapers/polymarket.py](scrapers/polymarket.py) | Direct Poly SDK scraper (replaced by Owls) |
| [kahla-scanner/scrapers/draftkings.py](scrapers/draftkings.py) | Direct DK scraper (403s from datacenter IPs) |
| [kahla-scanner/scrapers/fanduel.py](scrapers/fanduel.py) | Direct FD scraper (FD gated their API) |
| [kahla-scanner/systemd/kahla-scanner.service](systemd/kahla-scanner.service) | VPS systemd unit |
| [kahla-scanner/scripts/install_vps.sh](scripts/install_vps.sh) | Useful if we ever want a VPS again |
| [kahla-scanner/scripts/install_launchd.sh](scripts/install_launchd.sh) | Useful if we ever want Mac scheduling |

---

## 10. Env vars / secrets map

### GitHub Actions secrets (for cron workflow)

- `OWLS_INSIGHT_API_KEY`
- `SUPABASE_URL`
- `SUPABASE_SERVICE_KEY`

### GitHub Actions variables (tunable without code push)

- `SPORTS_ENABLED` (default: `NFL,NBA,MLB,NHL,CBB`)
- `SCANNER_MODE` (default: `log_only`)
- any `SHARP_CONSENSUS_*` threshold

### Vercel env vars (Flask)

- `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`
- `OWLS_INSIGHT_API_KEY` (used by /odds page + scanner page)
- `POLYMARKET_KEY_ID`, `POLYMARKET_SECRET_KEY` (for /dashboard P&L)
- `FIREBASE_SERVICE_ACCOUNT`, `FLASK_SECRET_KEY`

### Local `.env` files

- Repo root `.env`: `OWLS_INSIGHT_API_KEY`, `POLYMARKET_*`
- `kahla-scanner/.env`: `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`

### External services (not in env vars)

- **cron-job.org account** — owns the 5-min trigger. Credentials = user's email/password.
- **GitHub PAT** — stored only in cron-job.org job headers. Rotate ~2027-04-18.

---

## 11. Session log (commit-by-commit, 2026-04-18)

Chronological, newest last:

| Commit | Topic |
|---|---|
| `1152eff` | `install_vps.sh` bug fixes (swap, perms, tty reads) |
| `49e6968` | Drop FanDuel direct scraper from scheduler |
| `df35bcb` | **Owls Insight pivot** — 5-min cron, 13 books per call |
| `3f9413e` | HANDOFF reflecting Owls architecture |
| `67a2d52` | `owls.py` dedup logic (85-90% write reduction) |
| `f239a64` | **N-book Brier refactor** (all 9 books, min-n=5 winner) |
| `4771a0e` | Don't hide empty Brier rows |
| `9a86331` | "Currently tracking" banner |
| `c6cdc8a` | HANDOFF comprehensive rewrite |
| `cf422de` | **POLY 24h-offset fix** + Phase 1 signal detector |
| `d041a3a` | Staggered auto-refresh (60s/5min) |
| `a2915cb` | Drop `poly_ticks` from UI (legacy, archive-only) |
| `7b8d09f` | Slow GH schedule to `*/30` (cron-job.org is primary) |
| _this commit_ | HANDOFF end-of-session rewrite |

Also applied today (not in git):
- `team_aliases.sql` seeded into Supabase (434 rows).
- `OWLS_INSIGHT_API_KEY` added to GH Actions secrets.
- DO droplet spun up, configured, then destroyed (net cost $0 within 60-day credit).
- `.claude/settings.json` allowlist populated (gitignored).
- **cron-job.org account created** and scanner-poll dispatch job configured.
- **GitHub fine-grained PAT** created, stored only in cron-job.org.

---

## 12. Change log template (for future sessions)

When you modify the architecture, append a brief entry here so the running doc stays honest:

```
### YYYY-MM-DD — topic
- What changed
- Why
- Key commit(s)
- Any operational steps taken outside git (DB migrations, secrets added, etc.)
```

### 2026-04-18 — initial Owls-based pipeline stood up
- Migrated from VPS direct scrapers → Owls aggregator (9 books)
- Destroyed VPS, moved to GH Actions + external cron-job.org trigger
- Brier refactored to N books, Phase 1 signal detector live
- See commits in §11

---

## Quick-reference checklist for next session

- [ ] Refresh thekahlahouse.com/scanner — are all 9 book timestamps ≤5 min?
- [ ] `gh run list --workflow=scanner-poll.yml --limit 5` — at least 4 of last 5 should be `workflow_dispatch` (cron-job.org)
- [ ] Supabase: `select book, max(captured_at) from book_snapshots group by book` — all within the last 5-10 min?
- [ ] Supabase: `select count(*) from signals where triggered_at > now() - interval '24 hours'` — anything? Review if yes.
- [ ] If signals have fired and look reasonable → consider Phase 2 (Telegram).
- [ ] If signals are empty after 48h of live games → loosen `SHARP_CONSENSUS_EDGE_MIN_PCT` to 2.0 via GH Actions variables.
