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

### Step C — the real cutover (REWRITTEN Aug 31 post-rehearsal — this
### is the exact procedure; everything named here already exists and
### was proven that night)
Preconditions:
- [x] Cellar is sole writer for every money lane
- [x] Nightly pg_dump + restore test green (check the stamp first!)
- [x] Step A rehearsal full pass (client stack, writes, RPC, launchd,
      31× receipt)
- [ ] Rob present for the evening; says go
1. Stop the daemon (`sudo launchctl kickstart` is the restart later;
   stop via `sudo launchctl bootout system/com.kahlahouse.cellard` or
   just leave it — the ~20 min of failed laps self-heal, Vercel
   standby covers).
2. Final dump → new local db (NOT the shadow — it stays the backup
   target):
   `bash kahla-scanner/scripts/db_backup.sh` (fresh dump), then
   `createdb kahla && pg_restore --no-owner --no-privileges -d kahla
   ~/kahla-backups/<newest>.dump`
   (psql/createdb come from Postgres.app's bin if not on PATH:
   `/Applications/Postgres.app/Contents/Versions/latest/bin`).
3. Repoint PostgREST: edit `~/.kahla/postgrest.conf` db-uri →
   `postgres://robkahla@localhost:5432/kahla`, then
   `launchctl kickstart -k gui/$(id -u)/com.kahla.postgrest`.
   (Both services already run under launchd from the Aug 31 soak —
   nothing to install or start.)
4. `~/dev/kahla-house/.env`:
   `SUPABASE_URL=http://localhost:3011`   ← the CADDY SHIM port, NOT
   3010 — supabase-py needs the /rest/v1 rewrite (rehearsal finding #2)
   `SUPABASE_SERVICE_KEY=<contents of ~/.kahla/rehearsal.jwt>`
   Restart daemon: `sudo launchctl kickstart -k system/com.kahlahouse.cellard`.
5. Watch one evening: boot banner, lap times collapsing (~200s → tens),
   `oms_*`/repeg stats flowing, zero PGRST errors in
   `~/.kahla/logs/postgrest.log`.
6. **Vercel + Actions keep writing Supabase** during the trial — the
   split-brain window. Acceptable for ONE watched evening only; the
   C1/C2 decision below closes it. Rollback at any moment = revert the
   two `.env` lines, restart the daemon. Nothing else moved.
7. Also flip ON: Postgres.app "Start on login" (post-cutover it is
   load-bearing, not just the backup target).

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
