# Kahla Scanner — Session Handoff

> **Last session ended:** pipeline pivoted to Owls Insight as the single
> ingest source. GH Actions cron runs `python -m scrapers.owls` every 5
> minutes, pulls odds for 9 books (POLY, PIN, DK, FD, CIR, MGM, CAE, HR,
> NVG) in one API call per sport, and writes to Supabase `book_snapshots`.
> Verified: 1,392 snapshots landed in one manual run covering NBA + MLB +
> NHL. The VPS is still running but is now redundant — cancel it any
> time before day 60 of the DigitalOcean free trial to pay $0.
>
> **Start next session by:** running the triage queries in §4 to confirm
> cron cadence is holding and book_snapshots are still accumulating.
> Then start looking at actual Poly→public divergence signals in the
> data once ~24h has passed.

---

## 1. Live infrastructure (as of 2026-04-18)

**Single ingest pipeline: GitHub Actions cron + Owls Insight API.**

- **GitHub Actions workflow:** [`.github/workflows/scanner-poll.yml`](../.github/workflows/scanner-poll.yml)
  - Cron: `*/5 * * * *` (every 5 minutes)
  - Step 1: `python -m scrapers.owls` — hits `/{sport}/odds` for each sport,
    persists book_snapshots for 9 books per game.
  - Step 2: `python -m analytics.resolve` — ESPN scoreboard → market_outcomes.
  - Budget: ~2.3K req/day = 70K/month. Owls MVP+ plan is 300K/month.
- **Supabase** — `xzzjpbervfoyaodduynb.supabase.co`. Schema loaded,
  `team_aliases` has 434 rows (MLB 99 / NBA 104 / NHL 117 / NFL 114).
- **Flask app on Vercel** — thekahlahouse.com/scanner reads from Supabase
  and renders activity / Brier / signals / matched / unmatched.

### Books captured per cron

| Code | Book |
|---|---|
| POLY | Polymarket |
| PIN | Pinnacle (sharp public) |
| DK | DraftKings |
| FD | FanDuel |
| CIR | Circa |
| MGM | BetMGM |
| CAE | Caesars |
| HR | Hardrock |
| NVG | Novig |

Skipped: wynn, westgate, south_point, stations (low-signal Vegas books;
add back if ever useful).

### Operating the workflow

```bash
# Trigger manually
gh workflow run scanner-poll.yml --ref main

# Check recent runs
gh run list --workflow=scanner-poll.yml --limit 5

# View a run's logs
gh run view <run-id> --log
```

### Required GH Actions secrets (already set)

- `OWLS_INSIGHT_API_KEY` (added 2026-04-18)
- `SUPABASE_URL`
- `SUPABASE_SERVICE_KEY`

---

## 2. What's working

- **Owls ingest** — confirmed in production: NBA 392 / MLB 590 / NHL 410
  snapshots per run. ~1,400 rows per 5-min cycle.
- **Brier scoring** — CLI and `/api/scanner/brier` endpoint. Should start
  producing meaningful T-24h / T-6h / T-1h / T-0 numbers after ~24h of
  accumulation.
- **ESPN resolver** — populates `market_outcomes` for MLB/NBA/NHL/NFL.
- **Team aliases** — 434 rows, matcher resolves abbreviations correctly.

## 3. What's broken / open

### DigitalOcean droplet — redundant, decide whether to keep

The droplet at `24.199.119.210` is still running the APScheduler-based
pipeline from before the Owls pivot. It writes `poly_ticks` directly via
the Polymarket SDK at 45s cadence. That's **higher resolution** than
Owls' 5-min cadence for Polymarket specifically.

**Keep it if** you want the finer-grained Poly tick data for modeling
short-term movement. Cost: $4/mo after day 60 of the free trial (around
2026-06-17).

**Cancel it if** the Owls 5-min Poly snapshots are sufficient. That's
likely enough for the T-24h / T-6h / T-1h / T-0 Brier horizons the
project cares about.

To cancel: DigitalOcean dashboard → Droplets → `kahla-scanner` →
Destroy. The systemd unit, code, and all data are on GitHub and
Supabase; destroying the droplet loses nothing irreplaceable.

Related: `kahla-scanner/scripts/install_vps.sh` is still useful if we
ever want to respin a droplet (e.g. if a future scraper needs residential
IP workarounds). Keep the file.

### FanDuel direct scraper — fully retired

`scrapers/fanduel.py` left in place for reference only; removed from the
scheduler in commit `49e6968`. Owls now provides FD odds as one of the
aggregated books, so there's no need to hit FD directly.

### Direct scrapers in general — retiring

`scrapers/polymarket.py`, `scrapers/draftkings.py`, `scrapers/fanduel.py`
are all superseded by `scrapers/owls.py`. The old VPS service still uses
them via `jobs/scheduler.py` + `main.py`. If we decommission the VPS,
we can also delete these files. For now, leave them — they're harmless.

### Stale `poly_ticks` warnings (low priority)

The VPS still logs `bbo(slug) failed: market not found` warnings for
stale slugs. These can be quieted by a one-time `UPDATE markets SET
status='inactive'` on slugs the SDK no longer returns. Irrelevant once
the VPS is retired.

---

## 4. Quick triage queries (Supabase)

```sql
-- Ingest cadence check: should be < 6 minutes ago
select max(captured_at) from book_snapshots;

-- Per-book coverage
select book, count(*), max(captured_at) from book_snapshots group by book order by count(*) desc;

-- Per-sport coverage in last hour
select m.sport, count(*)
from book_snapshots s
join markets m on m.id = s.market_id
where s.captured_at > now() - interval '1 hour'
group by m.sport
order by count(*) desc;

-- Markets total
select status, count(*) from markets group by status;

-- Outcomes resolving
select source, count(*), max(resolved_at) from market_outcomes group by source;

-- Open signals (once divergence scanning is on)
select count(*) from signals where status='open';
```

### Early analysis starter (Brier across horizons)

Once ~24h of data has accumulated:

```bash
cd kahla-scanner
python -m analytics.brier --sport MLB --days 7
# Or hit /api/scanner/brier from the Flask app
```

The `/scanner` page on thekahlahouse.com reads the same data and renders
Brier scores per (book, horizon) so you can eyeball Poly vs DK vs PIN.

---

## 5. Key files

| File | Purpose |
|---|---|
| `kahla-scanner/scrapers/owls.py` | **Primary ingest** — Owls → book_snapshots |
| `.github/workflows/scanner-poll.yml` | Cron workflow, every 5 min |
| `kahla-scanner/analytics/resolve.py` | ESPN → market_outcomes |
| `kahla-scanner/analytics/brier.py` | Brier scorer (CLI + API) |
| `kahla-scanner/signals/matcher.py` | Cross-venue name → canonical linkage |
| `kahla-scanner/storage/supabase_client.py` | Supabase helpers |
| `kahla-scanner/supabase/schema.sql` | DB schema (idempotent) |
| `kahla-scanner/supabase/team_aliases.sql` | 434 alias rows (idempotent) |
| `scanner.py` (root) | Flask-side reader for `/scanner` page |
| `templates/scanner.html` | `/scanner` UI |
| `kahla-scanner/scrapers/polymarket.py` | **Legacy** — used only by the (retiring) VPS systemd path |
| `kahla-scanner/scrapers/draftkings.py` | Legacy — 403s from datacenter IPs, use Owls instead |
| `kahla-scanner/scrapers/fanduel.py` | Legacy — FD gated its API; use Owls instead |
| `kahla-scanner/jobs/scheduler.py` | VPS APScheduler (retiring) |
| `kahla-scanner/main.py` | VPS entrypoint (retiring) |
| `kahla-scanner/systemd/kahla-scanner.service` | VPS systemd unit (retiring) |
| `kahla-scanner/scripts/install_vps.sh` | Keep — useful for any future VPS need |

## 6. Env vars required

### GitHub Actions secrets (for the cron workflow)

- `OWLS_INSIGHT_API_KEY`
- `SUPABASE_URL`
- `SUPABASE_SERVICE_KEY`

### Local `.env` (for ad-hoc CLI runs)

- Repo root `.env`: `OWLS_INSIGHT_API_KEY`, `POLYMARKET_KEY_ID`, `POLYMARKET_SECRET_KEY`
- `kahla-scanner/.env`: `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`

## 7. Open questions / next session

1. **Retire the VPS?** Decide after a few days of Owls data. If Brier
   scores are converging with just 5-min data, kill the droplet before
   day 60.
2. **Wire divergence signal emission.** Currently `book_snapshots` lands
   but nothing computes cross-book divergence and inserts rows into
   `signals`. Once we have 24-48h of data, turn on `job_scan_signals`
   (or a standalone script equivalent).
3. **Alerts.** Telegram fan-out exists but won't fire until `signals`
   starts populating. Subscribers table ready.
4. **Props later.** Owls exposes `/{sport}/props`. Not in M0 but the
   same pattern works when we're ready.
