# Kahla Scanner — Session Handoff

> **Last session ended:** mid-debugging discover. Scanner page live on
> thekahlahouse.com/scanner, backend plumbing verified, no data flowing
> yet because Polymarket market discovery hasn't succeeded.
>
> **Start next session by:** triggering the scanner-poll workflow and
> reading the autoseed + discover step output. See §3 below.

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

### Primary blocker: Polymarket market discovery returns 0 games

Last poll run showed:
```
MLB: gamma_events: 1500, matched: 0, seeded: 0,
     skipped_no_ml: 768, skipped_no_match: 732,
     slug_probe_seeded: 0
NBA: identical numbers
NHL: identical numbers
```

Root cause hypothesis: **`gamma-api.polymarket.com` serves Polymarket
global (international soccer, politics, crypto), not Polymarket US
sportsbook.** The first gamma market we logged was a Russian Premier
League soccer spread, not a US sport. None of the 1500 fetched markets
matched any of the 55 MLB games ESPN returned.

### FD scraper: 404 on all sports

FanDuel returns `404 {"error":true}` for every sport. curl_cffi
impersonation got past the TLS layer (no longer a 403), but the
endpoint path or _ak key is wrong. Haven't debugged yet.

### DK: confirmed working but untestable

DK returns 200s and parses events (we saw "CHA Hornets @ ORL Magic"
logged as unmatched). But with 0 markets seeded, every DK event
falls through to unmatched_markets. Can't verify the snapshot-insert
path until discover works.

---

## 3. Where to start next session

### Step 1 — check the last commit's poll run

I pushed commit `ad8c37a` at the end of last session. It adds:
- `autoseed` step to the scheduled poll (pulls Rob's existing Poly
  positions as a known-working data source)
- Updated slug-probe patterns based on a real slug format we saw:
  `rus-soc-kss-2026-04-21-spread-away-2pt5` → implies
  `<league>-<away>-<home>-<date>-ml` for moneyline.

Was NOT tested before session ended. Start with:

1. Open `github.com/Diavel78/kahla-house/actions/workflows/scanner-poll.yml`
2. Run workflow → Run workflow
3. When done (~2min), tap the run → `poll` job → look at:
   - **"Auto-seed from positions"** — did it seed anything from Rob's
     current positions? If yes, we have data flowing and `/scanner`
     will finally show non-zero.
   - **"Discover new markets"** — did slug-probe hit anything? Look
     for `slug-probe seeded ...` lines. If still 0, next pivot needed.
4. Refresh `thekahlahouse.com/scanner` — if activity > 0, M0 pipeline
   is fully live.

### Step 2 — if autoseed + slug-probe both returned 0

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
