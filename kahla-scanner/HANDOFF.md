# Kahla Scanner — Session Handoff

> **Last session ended:** pipeline is live on a DigitalOcean droplet.
> Polymarket ticks flowing continuously, ESPN resolver capturing outcomes,
> team aliases loaded, DK matching confirmed working (34 markets already
> linked via `dk_event_id`). FD dropped from the scheduler — see §3.
>
> **Start next session by:** running the triage queries in §4. If DK
> `book_snapshots` is non-zero, start analyzing Poly→DK divergence in the
> `signals` table. If still zero, debug DK insert path (match works; log
> lines show `"DK {sport}: N events, M matched, X snapshots"` — investigate
> if matched > 0 but snapshots = 0).

---

## 1. Live infrastructure (as of 2026-04-18)

- **VPS** — DigitalOcean droplet `24.199.119.210` (Ubuntu 24.04, $4/mo).
  Hostname `kahla-scanner`. 2GB swap enabled (512MB RAM wasn't enough for
  `curl_cffi` compile).
- **systemd unit** — `kahla-scanner.service`, auto-starts on boot, runs
  `/opt/kahla-scanner/kahla-scanner/venv/bin/python main.py` as user
  `scanner`. APScheduler inside `main.py` runs:
  - Polymarket BBO poll every 45s
  - DK/FD scrape every 3 min
  - ESPN resolver every hour
  - Autoseed every 15 min
  - Discover (SDK-based) every 30 min
- **Supabase** — project `xzzjpbervfoyaodduynb.supabase.co`. Schema loaded,
  `markets.notes` column exists, RLS on. `team_aliases` table seeded with
  434 rows (MLB 99 / NBA 104 / NHL 117 / NFL 114).
- **thekahlahouse.com/scanner** — admin-gated review page on Vercel,
  reads Supabase live. Activity + Brier + signals all populate from
  the scanner's writes.

### Operating the droplet

```bash
# SSH in
ssh root@24.199.119.210

# Live logs
journalctl -u kahla-scanner -f

# Restart / stop / status
systemctl restart kahla-scanner
systemctl stop kahla-scanner
systemctl status kahla-scanner

# Update scanner to latest main
sudo -u scanner bash -c "cd /opt/kahla-scanner && git pull"
systemctl restart kahla-scanner
```

### GitHub Actions cron — now redundant

`scanner-poll.yml` still fires every 30 min, but the VPS covers the same
ground at 45s resolution. Leave it enabled as a backup, or disable in the
GitHub Actions UI to save minutes. Not urgent either way.

---

## 2. What's working

- **Polymarket poll** — `poly poll: 67 ticks across 147 markets` per cycle.
  Writes `poly_ticks` rows continuously. Verified via Supabase REST.
- **SDK-based discover** — `scrapers/discover.py` uses the authenticated
  Polymarket US SDK as the primary path (gamma-api fallback is vestigial).
- **ESPN resolver** — captures `market_outcomes` for MLB/NBA/NHL/NFL. 17+
  outcomes already recorded.
- **Brier scoring** — CLI and `/api/scanner/brier` endpoint both work.
  Needs ~24h of tick accumulation before T-24h numbers mean anything.
- **Team aliases** — 434 rows loaded. Matcher can now resolve abbreviations
  (e.g. `LA Dodgers` → `los angeles dodgers`).

## 3. What's broken / open

### FanDuel — dropped from the scheduler (2026-04-18)

**Removed from `jobs/scheduler.py`.** `scrapers/fanduel.py` left in place
for a future rewrite. Investigation summary:

- Old endpoint (`sbapi.{state}.sportsbook.fanduel.com/api/content-managed-page`
  with `_ak=FhMFpcPWXMeyZxOx`) returns `{"error":true}` across 15 state
  subdomains. Static token and path both gone.
- FD migrated to a unified host `api.sportsbook.fanduel.com` (paths
  `/sbapi`, `/chapi`, `/config`) and a **GraphQL endpoint at
  `pir.{region}.sportsbook.fanduel.com/graphql`** — but the GraphQL
  endpoint returns **401 `UnauthorizedException: Valid authorization
  header not provided`**. Requires a real user session.
- Bundle grep for a fresh `_ak` token returned zero matches. FD removed
  the static-token auth mechanism entirely.

**Rationale for dropping:** FD odds track DK within ~5-10 bps — redundant
second lagging book. The real M0 calibration question (sharp Poly vs
lagging public) is fully answered by DK vs Poly. Adding FD would not
materially improve signal.

**Re-enable path (later):** either (a) add Pinnacle as a sharp-public
scrape (new kind of signal, not a redundant one), or (b) run headless
Chrome + Playwright for FD login + XHR capture if a user-session-backed
FD feed becomes worth the infrastructure cost. Neither is M0-blocking.

### Secondary: stale slug noise in `poly_ticks` cycles

Every poll emits ~80 `bbo(<slug>) failed: market not found` warnings.
Caused by slugs in the `markets` table that no longer exist on the
Polymarket US API (left over from slug-probe fallbacks and day-of
markets that never listed). Cleanup path:

- In `fetch_bbo`, track consecutive 404s per slug; after N in a row, mark
  the market `status='inactive'` and stop polling it.
- Or run a one-off probe + bulk update: `select id, poly_market_id from
  markets where status='active'`, check each against SDK, mark dead ones.

Low priority — pipeline works, it's just log volume.

### Secondary: discover gap for today's games

~15 `unmatched espn event` lines per ESPN resolve cycle
(`Washington Nationals @ Pittsburgh Pirates`, etc.) — ESPN sees live games
but no Polymarket market was seeded for them. Discover runs every 30 min
so should fill in, but worth spot-checking whether the SDK actually
returns those matchups.

### Monitor: DK `book_snapshots` count

As of 2026-04-18 16:15 UTC, `book_snapshots` = 0. But matching itself is
working — **34 markets have `dk_event_id` populated**, and the last
`unmatched_markets` row for DK was from 04:58 UTC (~11h before this
HANDOFF, pre-alias-load). Expected reason for 0 snapshots: the scanner
only came up at 15:43 UTC, and `scrape_books` runs every 3 min — it may
just be that a scrape cycle hadn't completed a successful full flow yet
by the time the query ran. Verify after an hour:

```sql
select book, count(*), max(captured_at) from book_snapshots group by book;
```

If still 0 after a few hours, check the VPS logs
(`journalctl -u kahla-scanner -g 'DK ' -f`) for insert failures after
`"DK {sport}: N events, M matched"` lines.

---

## 4. Quick triage queries (Supabase)

```sql
-- Is the VPS writing ticks?
select max(captured_at) from poly_ticks;
-- should be within the last minute

-- DK/FD snapshots landing?
select book, count(*), max(captured_at) from book_snapshots group by book;

-- How many markets are in discover-churn?
select status, count(*) from markets group by status;

-- Unmatched backlog?
select source, count(*) from unmatched_markets where resolved=false group by source;

-- Outcomes resolving?
select source, count(*), max(resolved_at) from market_outcomes group by source;
```

---

## 5. Key files

| File | Purpose |
|---|---|
| `kahla-scanner/main.py` | Entry point — APScheduler loop |
| `kahla-scanner/jobs/scheduler.py` | Job registration + cadences |
| `kahla-scanner/scrapers/polymarket.py` | SDK-based BBO poll + autoseed + seed CLI |
| `kahla-scanner/scrapers/discover.py` | SDK discover (primary), gamma (fallback), slug-probe |
| `kahla-scanner/scrapers/draftkings.py` | curl_cffi chrome-impersonated scraper |
| `kahla-scanner/scrapers/fanduel.py` | Disabled in scheduler 2026-04-18 (FD gated GraphQL, 401). File kept for future rewrite. |
| `kahla-scanner/signals/matcher.py` | Cross-venue name → canonical linkage |
| `kahla-scanner/analytics/resolve.py` | ESPN → market_outcomes |
| `kahla-scanner/analytics/brier.py` | Brier scorer (CLI + API) |
| `kahla-scanner/systemd/kahla-scanner.service` | VPS systemd unit |
| `kahla-scanner/scripts/install_vps.sh` | One-shot VPS installer (swap, perms, tty-reads fixed) |
| `kahla-scanner/scripts/poll.sh` | Local one-shot poll runner |
| `kahla-scanner/supabase/schema.sql` | DB schema (idempotent) |
| `kahla-scanner/supabase/team_aliases.sql` | 434 alias rows (idempotent) |
| `scanner.py` (root) | Flask-side reader for `/scanner` page |
| `templates/scanner.html` | `/scanner` UI |

## 6. Env vars required on the VPS

Written to `/opt/kahla-scanner/kahla-scanner/.env` by the installer:

- `SUPABASE_URL`
- `SUPABASE_SERVICE_KEY`
- `POLYMARKET_KEY_ID`
- `POLYMARKET_SECRET_KEY`
- `SPORTS_ENABLED` (default: `NFL,NBA,MLB,NHL,CBB`)
