# Kahla Scanner — Session Handoff

> **Last session ended:** mid-debugging discover. Scanner page live on
> thekahlahouse.com/scanner, backend plumbing verified, no data flowing
> yet because Polymarket market discovery hasn't succeeded.
>
> **Start next session by:** running `kahla-scanner/scripts/poll.sh` locally
> and reading the autoseed + discover step output. See §3 below.

---

## 1. What's working end-to-end

- **thekahlahouse.com/scanner** — admin-gated page, reads Supabase,
  reports activity/signals/Brier/unmatched. Renders zeros right now
  because nothing is seeded. Deployed on Vercel.
- **Supabase project** — `xzzjpbervfoyaodduynb.supabase.co`, schema loaded
  with 9 tables + RLS. Creds in Vercel env + GitHub secrets.
- **GitHub Actions workflows** — 4 total:
  - `scanner-poll.yml` — scheduled cron every 30min. Runs discover,
    autoseed, Poly poll, DK scrape, FD scrape, ESPN resolve.
  - `scanner-seed.yml` — manual workflow_dispatch form for one-at-a-time
    seeding.
  - `scanner-autoseed.yml` — manual workflow_dispatch to seed from
    current Poly positions (also auto-runs inside poll).
  - `scanner-discover.yml` — manual workflow_dispatch for ad-hoc
    discover runs with different sports/days.
- **DK scraper** — confirmed 200 OK via curl_cffi chrome impersonation.
  Parses sportscontent + legacy shapes. Couldn't test end-to-end
  because 0 markets seeded to match against.
- **ESPN resolver** — fetches scores for MLB/NBA/NHL/NFL/NCAAF. Skips
  sports with no tracked markets so it doesn't spam unmatched_markets.
- **Brier scorer** — `python -m analytics.brier --sport MLB --days 30`
  works as a CLI; `/api/scanner/brier` endpoint serves the same from
  the Flask side. Waiting on data.

---

## 2. What's broken / unknown

### Primary blocker: gamma-api ≠ Polymarket US — confirmed

First local `./scripts/poll.sh` run (2026-04-17) confirmed HANDOFF's hypothesis:

```
MLB: gamma_events: 1500, matched: 0, seeded: 0,
     skipped_no_ml: 761, skipped_no_match: 738, slug_probe_seeded: 0
NBA / NHL: identical shape
```

First raw gamma market logged was **League of Legends (LCK Challengers)** —
gamma-api.polymarket.com serves Polymarket global (esports, international
soccer, politics), not Polymarket US sportsbook. Slug-probe *did* hit 6 NHL
slugs on gamma (e.g. `nhl-ott-car-2026-04-18`) and got them into `markets`,
but the subsequent BBO poll against `gateway.polymarket.us/v1/markets/{slug}/bbo`
returned 404 for every one of those slugs. Two different ecosystems; same-named
slugs don't carry across.

### Supabase schema drift: `markets.notes` column missing

`schema.sql` has had `notes jsonb` since M1, but the live Supabase project
wasn't re-migrated. Slug-probe inserts succeeded (201 Created), then the
follow-up `PATCH ... notes = {...}` returned 400:
`Could not find the 'notes' column of 'markets' in the schema cache`.
**Fix**: paste this into Supabase SQL editor (idempotent):

```sql
alter table markets add column if not exists notes jsonb default '{}'::jsonb;
```

Or re-run the whole `kahla-scanner/supabase/schema.sql` — it's safe to re-run.

### Autoseed: 0 positions returned

`GET https://api.polymarket.us/v1/portfolio/positions` returned 200 OK but
empty. Either (a) portfolio genuinely has no open positions right now, or
(b) creds point to a different account than the dashboard uses. Check
`thekahlahouse.com/dashboard` — if it shows positions, creds mismatch; if
also empty, just no data. Low-priority given discovery needs pivoting anyway.

### FD scraper: 404 on all sports (unchanged)

Same as before. Deferred.

### DK: scraper works end-to-end — no matches (expected)

DK NBA/MLB/NHL all return real event lists (e.g. `CHA Hornets @ ORL Magic`,
`LA Dodgers @ COL Rockies`). All log as `unmatched dk event` because the
`markets` table has no rows to match against. Will start matching once
discover seeds real Poly US markets.

---

## 3. Where to start next session

### Step 1 — fix Supabase `markets.notes` column

One SQL statement in the Supabase SQL editor, then move on:

```sql
alter table markets add column if not exists notes jsonb default '{}'::jsonb;
```

### Step 2 — probe the Polymarket US SDK to find a real discovery method

`scripts/probe_sdk.py` is a safe, read-only introspection script. Run once:

```bash
cd kahla-scanner
source venv/bin/activate
python scripts/probe_sdk.py
```

It prints every namespace/method on the `PolymarketUS` client, tries a
dozen likely discovery call names (`markets.list`, `events.list`,
`sports.markets(...)`, `catalog.markets`, etc.), and summarizes each
response shape. Whatever returns something useful is the new discovery path.

### Step 3 — implement `discover_via_sdk(sport)` using whatever probe found

Replace (or front-end) `scrapers/discover.py`'s gamma-api path with a new
`discover_via_sdk(sport)` that calls the SDK method(s) the probe identified.
Once implemented, the existing ESPN cross-reference and upsert path should
work as-is.

Alternative if the SDK has no list method: scrape `polymarket.com/sports/<league>`
HTML. More brittle, but last resort.

### Step 4 — re-run the poll

```bash
./scripts/poll.sh
```

Look for:

- `Discover new markets` — non-zero `seeded` count.
- `Poll Polymarket BBO` — ticks inserted (not all 404s).
- `DK scrape: <sport>` — `matched > 0` (was 0 before because markets table
  was empty; once seeded, DK should start matching).

Then refresh `thekahlahouse.com/scanner` — if activity > 0, M0 pipeline
is live.

### Aside — if Step 2 reveals there's no SDK list method either

Pivot strategy (not yet implemented): use the authenticated Polymarket
US SDK (`polymarket_us.PolymarketUS`) to list active sports markets
directly, instead of gamma-api.

Candidate SDK methods to try (guessing based on Python SDK conventions):
```python
client.markets.list()
client.markets.list(status='active', sport='mlb')
client.markets.search('mlb')
client.sports.markets('mlb')   # if a .sports namespace exists
```

Approach: write `scrapers/polymarket.py::discover_via_sdk(sport)` that
tries each method defensively with try/except, logs what works. Once
we find the right method, use it as the primary discover path and
retire gamma entirely.

Alternative if SDK has no list method: scrape polymarket.com's own
page HTML for sport pages (e.g. `polymarket.com/sports/mlb`), parse
market links, extract slugs. More brittle but works.

### Step 3 — fix FanDuel

After discover works, come back to FD. Current state: 404 on
`sbapi.az.sportsbook.fanduel.com/api/content-managed-page`. Options:
- Try different state subdomain (NJ, NY, PA, MI, VA, CO, TN)
- Try different endpoint path (FanDuel has changed this historically)
- Use Playwright to inspect live FD site and grab the exact URL
  their frontend hits

---

## 4. Commits on main (chronological)

```
ad8c37a  scanner-poll: also run autoseed; slug-probe: match real Poly format
ed1564d  discover: flip sort order + add slug-probe fallback for ESPN games
cef41b0  discover: unconditional sample logging + multi-field date filter
f4fbed0  discover: drop tag_slug filter, query gamma by endDate only
1378470  discover: pivot to gamma /markets endpoint, filter by endDate
c4d4456  Fix discover gamma filtering + quiet ESPN unmatched bootstrap noise
3a7a956  Bypass DK/FD TLS fingerprinting via curl_cffi; drop discover date filter
770d9fc  Auto-discover every active MLB/NBA/NHL market via gamma + ESPN
f9ac8c2  Auto-seed scanner markets from existing Polymarket positions
e822bd1  Add GitHub Actions workflows so scanner runs from phone
e671849  M6: FanDuel scraper via content-managed-page
111259c  M2: DraftKings scraper with dual-shape parser
3288cf2  M1: Polymarket BBO poller + market seeding CLI
9790b9b  Add M0 calibration gate: log-only mode + Brier scorer
bc36517  Scaffold kahla-scanner: DK/FD vs Polymarket edge scanner
```

---

## 5. Key files / entry points

| File | Purpose |
|---|---|
| `kahla-scanner/scrapers/polymarket.py` | Poll (working), seed CLI, autoseed (needs position data) |
| `kahla-scanner/scrapers/discover.py` | Discover via gamma (currently failing), slug-probe fallback |
| `kahla-scanner/scrapers/draftkings.py` | curl_cffi + Chrome impersonation, 200 OK confirmed |
| `kahla-scanner/scrapers/fanduel.py` | 404 — needs endpoint fix |
| `kahla-scanner/analytics/brier.py` | M0 scorer (CLI) |
| `kahla-scanner/analytics/resolve.py` | ESPN scoreboard → market_outcomes |
| `kahla-scanner/supabase/schema.sql` | DB schema; idempotent, safe to re-run |
| `scanner.py` (root) | Flask-side Supabase reader + Brier computation |
| `templates/scanner.html` | /scanner page UI |
| `.github/workflows/scanner-*.yml` | 4 cron/dispatch workflows |

---

## 6. Env vars required

### In Vercel (for `/scanner` page to render data)
- `SUPABASE_URL` = `https://xzzjpbervfoyaodduynb.supabase.co`
- `SUPABASE_SERVICE_KEY` = service_role JWT

### In GitHub Actions secrets (for cron jobs to write data)
- `SUPABASE_URL`
- `SUPABASE_SERVICE_KEY`
- `POLYMARKET_KEY_ID`
- `POLYMARKET_SECRET_KEY`

All 4 GH secrets confirmed present at end of last session.

---

## 7. Milestone status

| # | Milestone | Status |
|---|---|---|
| M0 | Log-only calibration + Brier | **Done** (waiting on data) |
| M1 | Polymarket poller | **Done** — BBO-mid poller, needs seeded markets |
| M2 | DK scraper | **Done** — curl_cffi fixes TLS fingerprint |
| M3 | Divergence engine | **Done** (logic), gated on data |
| M4 | Telegram alerter | **Done** (code), gated on M0 results |
| M5 | Multi-user fan-out | **Done** (merged with M4) |
| M6 | FD scraper | **Broken** — 404s, needs endpoint fix |
| M7 | Dashboard /scanner | **Done** — live on thekahlahouse.com |
| M8 | Expand sports | N/A (pending M2 validation) |
| M9 | Per-market detail | TODO |
| M10 | Kalshi | TODO |
| **NEW** | **Market discovery** | **BLOCKED** — gamma returns 0 games, pivot needed |

---

## 8. Things to remember for next session

- `SCANNER_MODE=log_only` is still the default. Signal scan + Telegram
  stay off until M0 Brier shows Poly is sharper. Flip to `live` via
  GitHub repo variable, not code.
- GitHub Actions free tier on private repo is 2,000 min/month. The
  30-min cron uses ~1,500 min/month — fits, but not forever. VPS
  is the long-term home.
- Scanner page is admin-gated client-side. First signup on
  thekahlahouse.com auto-promotes to admin.
- The 39 stale ESPN unmatched rows self-cleaned on the first poll
  after commit `c4d4456`. That "needs review" counter should now
  be 0 or very low.
- `markets.notes` column holds `poly_home_side` per market (yes/no)
  — set at seed time, used by the divergence engine to compute
  HOME probability.
