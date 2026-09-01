# Phase 2 — Local Postgres on the box (the round-trip tax removal)

Status: **RUNBOOK — prepared Aug 31 2026** (the night the lap-limits
conversation happened). Executes §12c/§12e of `cellar-migration-spec.md`.
Nothing here runs automatically; each step is a box command Rob (or a
session on the box) runs deliberately.

## Why now

Every DB call from the box is an HTTPS round trip to Supabase's
PostgREST (~60-120ms + connection weather). A busy lap makes hundreds of
them — that IS a large share of the 200-260s laps, and today's four
"Server disconnected" incidents (paperlog ×3, the incentives sync crash
that double-counted Friday's rent) were all this wire. On a local socket
the tax is ~0.1ms and the disconnect class disappears.

## What already exists (do not rebuild)

- **Local Postgres already runs on the box** — the nightly backup
  (`kahla-scanner/scripts/db_backup.sh`, 3:30am AZ launchd) restores the
  full live DB into `kahla_shadow` EVERY night and stamps
  `exec_probe_runs` kind=db_backup. The restore test §12e demands has
  been passing nightly.
- The app speaks **PostgREST** (supabase-py), not raw SQL. The migration
  seam is therefore: run a local **PostgREST** binary in front of local
  Postgres and swap `SUPABASE_URL` — zero app-code changes.

## The plan (strangler pattern, reversible at every step)

## ✅ STEP A EXECUTED — Aug 31 2026, FULL PASS

The production client stack (supabase-py → Caddy shim → PostgREST 16.2
+ JWT → local Postgres 18/Postgres.app) returned live picks + the
restore-moment snapshot timestamp off the box's SSD. Cutover night is
now paint-by-numbers. **Four findings, all solved and parked on the box:**

1. **Binaries, not brew.** brew wanted to COMPILE both (GHC + Go
   toolchains) and upgrade openssl on the production box — refused.
   Official release binaries live in `~/kahla-rehearsal/`
   (postgrest v16.2 macos-x86-64, caddy 2.11.4 mac_amd64). PostgREST
   needs `DYLD_FALLBACK_LIBRARY_PATH=/Applications/Postgres.app/
   Contents/Versions/latest/lib` (Postgres.app's libpq — there is no
   brew libpq on the box); the "built for macOS 15" warning is benign.
2. **The /rest/v1 shim is mandatory.** supabase-py hardwires
   `{url}/rest/v1/...`; Caddy `handle_path /rest/v1/*` →
   `reverse_proxy 127.0.0.1:3010` solves it (`~/.kahla/Caddyfile`,
   serving :3011 — the URL the app gets is `http://localhost:3011`).
3. **jwt-secret is REQUIRED** — a secretless PostgREST rejects every
   Bearer token (`PGRST300 Server lacks JWT secret`), and the client
   always sends one. Secret is appended to `~/.kahla/postgrest.conf`;
   the signed token (HS256, `{"role":"robkahla"}`, minted with the
   stdlib snippet below) is at `~/.kahla/rehearsal.jwt` — it IS the
   `SUPABASE_SERVICE_KEY` value on cutover night.
4. **Bind loopback in the real config**: PostgREST defaulted to
   `0.0.0.0:3010` — add `server-host = "127.0.0.1"` (and keep Caddy
   on `:3011` loopback via `bind 127.0.0.1` if exposing anything).

Cutover night therefore reduces to: final dump → restore into a new
`kahla` db → point db-uri at it → two launchd plists (postgrest with
the DYLD var, caddy) → `.env`: `SUPABASE_URL=http://localhost:3011`,
`SUPABASE_SERVICE_KEY=<the jwt>` → restart daemon → watch. Rollback =
revert `.env`, restart. The split-brain caveat below (Actions +
Vercel still write Supabase) REMAINS the blocker for doing this while
anyone is away — it is why Step C waits for a watched evening.

### Step A — local PostgREST in front of the shadow (read-only rehearsal)
1. `brew install postgrest` (single binary).
2. Config `~/.kahla/postgrest.conf`:
   ```
   db-uri = "postgres://<local_user>@localhost:5432/kahla_shadow"
   db-schemas = "public"
   db-anon-role = "<local_user>"      # solo box — no RLS locally
   server-port = 3010
   jwt-secret = "<32+ char random string>"
   ```
3. Mint a service JWT for supabase-py (role claim matching
   db-anon-role; any JWT tool with the secret above works).
4. Smoke test: `curl localhost:3010/markets?limit=1` returns rows.
5. Rehearsal: point a THROWAWAY shell at it —
   `SUPABASE_URL=http://localhost:3010 SUPABASE_SERVICE_KEY=<jwt>` and
   run a read-only script (e.g. `python -c` selecting from bot_picks).
   Confirms the whole client stack works locally. **No daemon change.**

### Step B — carve the READ-HEAVY, LOW-RISK consumers over
The dedup maps + history reads (pm_snapshots/prop tape reads) tolerate
one-night staleness of the shadow. But DON'T bother building split-brain
plumbing — Step C is close enough that a partial cutover only adds a
seam to debug. Skip B unless C gets delayed by weeks.

### Step C — the real cutover (one evening, spec §12c)
Preconditions (all already true or nightly-verified):
- [x] Cellar is sole writer for every money lane
- [x] Nightly pg_dump + restore test green (check the stamp first!)
- [ ] Rob says go
1. Stop the daemon + pause cron writers (scanner-poll fill_ping is
   Vercel-side — leave it; its endpoints will fail loudly against the
   old URL for the ~20 min window, self-heal after).
2. Final `pg_dump` from Supabase → restore into a NEW local db
   `kahla` (not the shadow — the shadow keeps being the backup target).
3. Start PostgREST against `kahla` (port 3010).
4. `~/dev/kahla-house/.env`: `SUPABASE_URL=http://localhost:3010`,
   `SUPABASE_SERVICE_KEY=<local jwt>`. Restart daemon.
5. **Vercel keeps pointing at Supabase** during the trial window — the
   site reads slightly stale data (dash cache is box-written… see the
   caveat below). Watch one full evening of laps.
6. Rollback = revert `.env`, restart. Nothing else moved.

### The Vercel caveat (the real design decision left)
After C, box and Vercel write DIFFERENT databases. That's fine for a
trial evening but not steady state. Options, decided with Rob:
- **C1 (spec's end state):** move the remaining Vercel jobs (paperlog
  route body, serve paths) box-side / behind a box-exposed API, retire
  Vercel Pro (§12b). The site then reads a small box-pushed cache.
- **C2 (transition):** expose local PostgREST to Vercel via a tunnel
  (cloudflared) and swap Vercel's SUPABASE_URL too. One DB again,
  Supabase fully retired, Vercel stays temporarily.
Either way Supabase Pro (~$25/mo) dies at this step; the nightly backup
script repoints its SOURCE to the local db and keeps dumping to
`~/kahla-backups/` (now also push a copy off-box — S3/B2/iCloud — since
the box is no longer "the copy").

### Measurement (before/after, honest)
- Lap times: `cellar_ticks` avg/max duration_ms per lane, 24h before vs
  after.
- Disconnect class: count of "Server disconnected"/RemoteProtocolError
  in cellar_ticks.error + poly_incentive_sync.note, before vs after.

## Order tonight
1. Pull + restart (delivers OMS/Phase 1 + today's fixes) — verify
   first OMS ticks (`desired_orders` filling, `oms_*` tick stats).
2. Step A (~20 min, zero risk).
3. Step C scheduled for a quiet evening this week, Rob present.

## ✅ SOAK + WRITE PATH + THE RECEIPT (Aug 31 night, after Step A)

- **launchd soak RUNNING**: `com.kahla.postgrest` + `com.kahla.caddy`
  (user LaunchAgents, KeepAlive, logs in `~/.kahla/logs/`) serve the
  shadow all week — cutover night inherits proven plists; the DYLD var
  works under launchd. Unload with `launchctl unload` if ever needed.
- **Write path proven**: insert/update/delete on exec_probe_runs and
  the `poly_gameday_pnl` RPC all correct through the full client stack.
  Finding #5: the shadow is yesterday's copy, so tables created TODAY
  don't exist in it until the next 3:30am restore — harmless for the
  real flip (it starts from a fresh final dump).
- **THE LATENCY RECEIPT (30-call bench, same library, same query):**
  LOCAL 3.9 ms/call vs SUPABASE 121.1 ms/call — **31×**. A 300-call
  lap: 36.3s of DB wire → 1.2s. This is the measured source of the
  ~200s repeg laps and the quantified prize of Step C.
- dotenv gotcha: `load_dotenv()` asserts under stdin scripts — pass the
  .env path explicitly.
