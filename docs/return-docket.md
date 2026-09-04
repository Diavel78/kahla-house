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

## 1. ✅ STEP C EXECUTED — Sep 3 2026, ~5:00-5:30pm AZ, FULL PASS
Rob's first night home. Fresh dump 16:57 (92MB, verified fresh via
max(cellar_ticks)=16:56:29), restored into `kahla` (8 ignored errors =
vault extension ×3 + anon-role policies ×5, all expected), PostgREST
repointed, .env flipped (backup at .env.bak-supabase), daemon
re-bootstrapped (⚠ bootout UNLOADS — kickstart can't restart it;
`launchctl bootstrap system /Library/LaunchDaemons/com.kahlahouse.cellard.plist`).
RECEIPT: pm_snapshot laps 60s+overruns → completes EVERY minute;
paperlog 181-190s overruns → green; repeg 607s first lap → GREEN second
lap (acted=1 through the full local stack); alerts+kalshi_autolog green
round two. NEW OPS FACTS: (a) Supabase cellar_lease rows PINNED
(heartbeat now()+7d, note says why) so Vercel NEVER claims the money
lanes against the frozen Supabase — unpin ONLY on rollback or at C1/C2;
(b) ~/.kahla/db_url repointed to the local socket (Supabase URI
preserved at ~/.kahla/db_url.supabase — needed for the farewell dump);
(c) SUPABASE IS FROZEN as of ~16:56 Sep 3 — run_sql.sh reads STALE
state for cellar_ticks/bot_picks/poly_activities/exec_probe_runs; box
observability is psql-on-the-box until C2. NEXT: the C1/C2 decision
with Rob (close the split, kill Supabase Pro ~$25/mo); sheets pipeline
+ resolver + paperlog/alerts Vercel jobs still point at frozen Supabase
until then.

## 1-old. STEP C — the Postgres cutover (original plan, executed above)
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
- ~~The two `failed:?` desired rows~~ SOLVED Sep 3: `_autobet_execute`
  returns the STRING "placed" but its docstring said "Returns True" —
  FIVE call sites compared `r is True`, so successful placements read
  as failures (the seeder's "failed:?" rows WERE seeded bets), the
  fbprop per-tick bound never counted, the two-rung loop never counted
  its spend against the $20 cap, and the ML side-glance stamped
  bet_gate="failed" on bets it placed (DET@BUF). All five fixed + the
  docstring corrected; live at the pull.

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

## 7. Sep 1 incident — closing lessons (written after Rob won two
## arguments against the trade mirror)
The venue POSITION CARD is settlement truth for any position born from
the over-sell class. The trade mirror's cost field on overflow/short-
crossing rows is CORRUPTED (proven: a 97.68c-per-contract "fill" on a
~34c market) — gotchas #7/#8's complement poisoning extends to `cost`
on these rows, so no cash reconstruction from the mirror is valid for
short-side economics. Sequence of my errors, both caught by Rob from
his phone: read his STL-LAD close in the wrong side's currency (+$3.90
claimed, ~+$0.97 real), then defended a "36c true basis" on the Angels
short against the venue's own $14.45 card. Rule going forward: mirror
= quantities and timestamps; venue card/settlement credits = money.
The final incident total gets read from settlement credits only.

## 8c-DECIDED (Sep 2 late night — Rob ruled on every audit item, built
## same night, ALL box-side → live at the pull):
- #1 venue-is-the-schedule: BUILT (a3e0f04) — stamp-or-mint from the
  PMM event, rent_key fast path.
- #2 new families: CFB ML + UFC ML BUILT (d8f8b7f, `_gridiron_try_ml`);
  Rob explicitly REFUSED MLB run lines, NFL ML, tennis/soccer/table-
  tennis/esports/darts. Live rent on aec-cfb/aec-ufc still unverified —
  placement gate decides.
- #3 two rungs: middle + model's neighbor, $20/event pending-book cap
  (e8f8197). Known v1 edge: a 1-of-2 placement marks the OMS row placed.
- #4 NFL props: model gate STAYS until first fills prove turn rate.
- #5 CFB tape window 72h→168h (a3e0f04).
PULL-DAY VERIFICATION adds: ML desired rows appear (market_type
moneyline) with sane verdicts; ensure_stamped/ensure_minted in opener
stats; two-rung picks stamp rung_role; no dup rows for stamped games.

## 8d. DUPLICATE-KILL, three layers (Sep 3 — Rob: "gotta put an end of
## these duplicates. That's a huge risk for buying and selling.")
Root cause found: the tenst-ga twin picks came through _autobet_execute's
pick-insert RETRY (first insert times out client-side but commits; retry
books a copy — same order_id, 0.8s apart), and the scalp then placed one
ask PER PICK ROW per lap off its stale start-of-tick orders snapshot
(2/lap × 2 laps = the 4 stacked 20-lot sells = naked-short risk). Fixes:
1. DB (LIVE NOW, blocks even the old box code): migration 018 —
   `bot_picks_football_slug_uniq` (gridiron_autobet + fbprop_autobet,
   pending, slug-keyed) replaces the ad-hoc gridiron index; with the
   existing machine_slug_uniq every machine source is now covered.
2. Scalp (live at pull): in-pass (slug, side) candidate dedup + DUP-SELL
   REPAIR — >1 AUTO ask on a slug cancels down to the oldest, even in
   shadow mode, and the survivor re-sizes to held next pass.
3. Reconcile (live at pull): automated dup-order sweep BOTH intents,
   every 15 min — AUTO + own-pick-slug + 6-cancel cap, keeps oldest,
   re-reads orders after canceling so the zombie logic can't act on the
   stale snapshot. The manual dedup-orders/reset-sells become forensics
   tools, not the healer.
Pull-day check: reconcile stats show `dup_canceled` only if something
was actually stacked (expect absent/0 on a healthy day); no 🧹 pings.

## 8c. THE RULE-1 VISIBILITY AUDIT (Sep 2 night — Rob: "if it's paying
## rent, I want it bet… are we seeing it, or are we missing it?")
Walked every layer between "the venue pays it" and "the machine sees
it." Findings, biggest first:
1. **CFB rent-join blindness** — of 254 enrolled CFB games, only 143
   reached the OMS board. Fixed in two parts: 13 code aliases
   (92ff424, +18 games incl Alabama@Kentucky — venue codes the popular
   abbreviation, ESPN short-names the school) and the STRUCTURAL rest:
   ~92 games, mostly FCS-vs-FCS, have NO ESPN `markets` row at all
   (groups=80 = FBS scoreboard only). **ROOT FIX (build on return, or
   bless remotely): VENUE-IS-THE-SCHEDULE for football** — mint
   `markets` rows from rent_list_slugs exactly as `_pmm_ensure_markets`
   does for MLB (Rob's own Aug-10 doctrine: "Polymarket's list IS the
   schedule — a third-party spine can only subtract"). Kills the
   code-decode problem too (slug becomes the identity).
2. **~2,900 enrolled slugs in families with ZERO machinery**: aec-cfb
   CFB moneylines (254), asc-mlb RUN LINES (212 rungs — MLB spreads,
   never had a lane), aec-ufc UFC MLs (40), tennis ATP/WTA/ITF
   (~1,240), soccer EPL/UCL/LaLiga/SerieA/Bundes/Ligue1 (~1,486),
   Setka table tennis (~317), CS2 (59), Modus darts (50). Caveat:
   enrolled ≠ paying NOW (the NFL catalog-stale lesson) — live
   rent-check probes per family are the first step before building
   anything. MLB run lines are the nearest-in: same sport, same spine,
   same scalp machinery; the July spread-model shadow verdict barred
   the MODEL, not a rent vehicle.
3. **Single-rung occupancy**: one bet per (game, market_type) while
   EVERY paying rung is its own reward pool — on a mid±1 window of 3
   paying rungs we occupy 1 and leave 2 pools empty. Multi-rung needs
   a Master-Rule call (3×20×~50¢ ≈ $30/event vs the $13 cap).
4. **fbprop vs Rule-1**: when NFL prop rent flips on, the 4-10pp model
   band bets ~9 of 540 paying props. If astatc-nfl really pays EARLY
   (the recon's "largest early pool"), that's 98% of a paying pool
   unquoted — Rob decides: rent-first props (rest on all payers, model
   picks side) vs keep the betting gate.
5. Smaller fences: pm-snapshot NCAAF watch window 72h (CFB tape/props
   only inside 3 days; Saturday slate swamp was the reason); the PMM
   event search returned exactly 200 events in a 1-day window on the
   Merrimack probe — POSSIBLE page cap; verify Saturday that big-slate
   lookups aren't truncated (a truncated page defeats any matcher).
NFL game lanes verified CLEAN: all 16 Week-1 games on the board.

## 8b. RUNG WINDOW shipped Sep 2 — INERT UNTIL THE BOX PULLS
Rob's rule from the road (after seeing Furman@Tenn Over 80.5 held to the
whistle): "if multiple paying rungs are available, you can only select
the middle or 1 to either side of middle… delta +/- 1". Implemented in
`_gridiron_try_bet` (mid±1 over the paying ladder, booked+virgin; empty
windowed booked set → seeder quotes a windowed virgin rung; else verdict
`rung_window`, hourly retry) — pushed to main, but the executor runs on
the BOX, so far-rung bets continue until the next pull. On pull day:
verify new gridiron picks stamp `rung_window: true` and that
`desired_orders` grows `rung_window` verdicts instead of tail bets.

## 8. OMS executor ordering: tries-first starves the retry tail (Sep 2)
The tries-first fix (3e0c059) let never-tried rows drain but STARVED
high-tries retries — BAL-IND's rent rows sat 12h overdue while fresh
rows ate every slice. Proper fix (box): order the due-row fetch by
next_try_at ASC NULLS FIRST (the natural due queue; tries as tiebreak).
Interim all week: the daily check resets tries=0 on pending rows >1h
overdue (reorders only — next_try_at still gates, so no early retries).
Also confirmed Sep 2 with rent-check + a known-good control: the NFL
Week-1 rungs' catalog rows (synced Aug 31, early-active) are STALE —
the venue's LIVE answer is market_pays [] on the same rung while the
MLB control reads early/day_of/live. The catalog is a map, never the
verdict; placement's live per-market read is the only truth (rule #1's
default-deny was RIGHT for all 11 refusals).
