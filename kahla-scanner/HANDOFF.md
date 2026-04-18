# Kahla Scanner — Session Handoff

> **Last session ended:** pipeline is live on a DigitalOcean droplet.
> Polymarket ticks flowing continuously, ESPN resolver capturing outcomes,
> team aliases loaded. DK matching untested end-to-end; FD endpoint still
> 404s and is the next thing to fix.
>
> **Start next session by:** running the triage queries in §4 to confirm
> recent `poly_ticks`/`book_snapshots` counts, then pivot to FanDuel.

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

### Primary next task: FanDuel scraper 404

`scrapers/fanduel.py` hits
`https://sbapi.az.sportsbook.fanduel.com/api/content-managed-page` with
`_ak=FhMFpcPWXMeyZxOx`. All sports return 404. Endpoint or auth token has
moved. Options in priority order:

1. Open FanDuel in a browser, watch Network tab, grab the real URL the
   current frontend is hitting. Likely same shape, different path or token.
2. Try other state subdomains (`nj`, `ny`, `pa`, `mi`, `va`, `co`, `tn`).
3. If the frontend has pivoted to GraphQL, swap the scraper to target that.

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

### Secondary: DK `book_snapshots` count still 0

Team aliases were loaded at `2026-04-18T15:30Z`, just before the scanner
came up. DK scrape should now match and insert `book_snapshots` — but
Supabase showed 0 at the time of this HANDOFF. Verify with the triage
query in §4. If still 0 after an hour, the matcher may need additional
aliases or the name-normalization step may have a bug for specific scrape
shapes.

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
| `kahla-scanner/scrapers/fanduel.py` | **Broken (404) — next task** |
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
