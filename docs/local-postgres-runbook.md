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
