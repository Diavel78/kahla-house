# THE RETURN DOCKET — Rob back from the Sep 1-~7 2026 trip

The lined-up work, in order, written the night before departure. Each
item is READY — spec'd or scripted, no design left, just watched hands.
The daily check (9am AZ Routine, absence mode) owns the machine until
then; its milestones feed item 0.

## 0. First hour back — read the week
- `desired_orders`: never_tried should have hit ~0 within a day of
  departure and stayed there as new enrollments land (left at 150 and
  falling ~30/hr the last night). If it plateaued: the executor stalled —
  check opener-lane `oms_touch` in cellar_ticks, then the two `failed:?`
  rows' pattern (Texas State@Texas + Baylor@Auburn totals were the first
  two — an unnamed executor exit worth a real diagnosis if it spread).
- Rent by day on the dashboard vs the Aug 31 baseline ($21.87/16 football
  markets): the whole week's thesis is that number climbing as the board
  filled. That's the "was it worth it" read.
- NFL props turn rate (fills vs scalp exits on astatc-nfl slugs) — the
  experiment Rob defined; the daily check reports the ratio whatever it
  says.
- First seed_quote fills (signal_blob->>'seed_quote') — alone-in-window
  pricing sanity.

## 1. STEP C — the Postgres cutover (first watched evening)
`docs/local-postgres-runbook.md` Step C is paint-by-numbers: fresh dump →
`createdb kahla` + restore → repoint PostgREST db-uri → launchd kickstart
→ two `.env` lines (SUPABASE_URL=http://localhost:3011 — the CADDY SHIM,
not 3010 — and SUPABASE_SERVICE_KEY=<~/.kahla/rehearsal.jwt>) → restart
daemon → watch. Rollback = revert two lines. The soak (postgrest+caddy
launchd agents) ran all week; check `~/.kahla/logs/` are clean first.
Receipt to expect: 121ms → 4ms per DB call (measured 31×); repeg/opener
laps collapse; the disconnect class (incl. the NO SIGNAL flicker's root)
dies. Then the C1/C2 Vercel decision per the runbook.

## 2. WS cap probe + quote table (same week, after cutover)
`docs/ws-quote-table-spec.md` — Rob's call ("why can't that be
websocket?"), researched Sep 1: the 400-slug cap is OUR constant, venue
documents no limit; concurrent per-requestId group subscriptions kill the
rotation/baseline-flood machinery. Order: 15-min cap probe (standalone
connection, procedure in the spec) → quote table → executor prices from
memory. This is the bigger prize than the cutover; cutover goes first
only because it's finished engineering.

## 3. Dials to revisit once 1-2 land
- `_OMS_BUDGET_S` (40): after cutover, laps shrink — likely SHRINK the
  budget back; if never_tried plateaued during the week, RAISE it via
  box .env (OMS_BUDGET_S) as the interim lever.
- Executor throughput after the quote table: `_OMS_MAX_CREATES` and the
  serial-writes review (parallel per-slug VENUE READS are fine; writes
  stay serial — the Cloudflare lesson).
- The two `failed:?` desired rows if still recurring — name the exit
  (executor fail_tag came back "?", meaning an unnamed return path).

## 4. Standing questions Rob never answered (ask, don't assume)
- Gridiron stakes: 20 spread / 20 total contracts at tripled market
  width — still right? (Flagged Aug 31; Master Rule bounds it meanwhile.)
- Sheets pipeline: Week 1 packs were published from a separate session;
  the Monday/Friday Routines now self-clone the repo (fixed Sep 1 after
  the no-repo death) — first unattended test is Fri Sep 4 / Mon Sep 7.
  Verify their completion notifications actually arrived.

## Context for a fresh session
The week this docket ends: rent-list-as-the-queue went live end to end
(sheet harvest → desired_orders board → typed verdicts → executor), the
OMS executor got three stacked throughput fixes (tries-first + lookup
backoff; producer throttle; per-rung tick/rent HTTP tax removed), the
dashboard NO SIGNAL flicker was root-caused (empty-block cache poisoning,
fixed three layers deep), and NFL props went live at 5 contracts under
the turn-rate experiment. CLAUDE.md's OMS section has the durable map.

## 5. db_backup.sh vs the soak (added Sep 1 morning)
The nightly restore-test fails while the rehearsal soak's PostgREST holds
kahla_shadow open ("createdb kahla_shadow" — cannot drop a DB with live
connections). The DUMP half stays healthy (~98MB); the dashboard verdict
knows the signature and shows ok+note instead of a week of false amber
(app.py backup block + the serve-time repair). ROOT FIX on return, in
db_backup.sh: terminate shadow connections before the restore
(psql -c "select pg_terminate_backend(pid) from pg_stat_activity where
datname='kahla_shadow'") or stop/start the postgrest LaunchAgent around
it — then remove the display special-case. Post-cutover this reshapes
anyway (backup source repoints to local per the runbook).

## 6. Ghost-position adoption for football (added Sep 1 midday — the
## Fresno State +3.5 case)
The Sep 1 maintenance storm proved the class: reconcile false-kills a
pick during a venue wobble, the order fills AFTER the kill, and the
position rides naked — no pick, no scalp ask, no grading. The MLB
autolog (`_pmm_autolog`) adopts orderless positions and caught the ML
shorts within the hour; football positions are OUTSIDE its scope, so
Fresno sat invisible for 8 hours until a human query found it. Interim
(absence week): the daily check runs a positions-vs-book audit every
morning and adopts ghosts by hand (proven recipe → pick 4879; source
'pmm_autolog' so _scalp_adopted_ok qualifies it off the tape). ROOT FIX
on return: extend position→pick adoption to the football families
(asc-/tsc- cfb+nfl, astatc-nfl) — either widen _pmm_autolog's slug
index past MLB or add the venue-position sweep to _reconcile_tick,
which already reads positions. Then delete the manual recipe from the
daily-check prompt.
