# THE RETURN DOCKET — Rob back from the Sep 1-~7 2026 trip

The lined-up work, in order, written the night before departure. Each
item is READY — spec'd or scripted, no design left, just watched hands.
The daily check (9am AZ Routine, absence mode) owns the machine until
then; its milestones feed item 0.

---

# ⚡ SESSION HANDOFF — Sat Sep 5 2026, ~8am AZ (read this FIRST)

Written closing the GIL-cliff-Friday session (Rob at the box all
evening + Saturday morning status). Everything below was verified this
morning unless marked open. CFB Saturday slate starts ~9am AZ.

## Where the machine is (verified ~7:50am AZ)
- **Box on LOCAL Postgres since Thu ~5pm (Step C full pass, §1).
  Cloud Supabase is FROZEN** except two mirrored tables
  (`poly_dash_cache`, `poly_incentive_earnings`). ⚠ `run_sql.sh` reads
  STALE state for everything else (cellar_ticks, bot_picks,
  poly_activities, pm_snapshots, desired_orders…) — box truth is
  `psql kahla` ONLY, until C2. A session that forgets this will
  mis-diagnose exactly like Fri night's Reds story (below).
- **All 11 lanes completing** (roster restored 22:12 Fri:
  batch,pm_snapshot,vsin,kalshi_autolog,ledger,opener,paperlog,repeg,
  alerts,grader,scalp). Nothing wedged overnight. Lap times **17-28
  min** at Saturday inventory — the GIL ceiling, fixes queued (#8/#9),
  not a fire. Grader grades every minute; repeg acting (work=2);
  vsin caught up (work=172).
- **Scalp coverage: backlog DEAD.** 28 naked at Fri bedtime → 0
  backlog; steady state 1-4/lap with `naked == placed` every lap
  (same-lap coverage of new fills). Dead-scalp tripwire armed on
  `uncovered`. Gap: a new fill can sit naked up to one lap (~26 min
  today) — levers are #8 and the WS POSITION wake, which still pokes
  repeg only (scalp is its own lane now; re-wire the wake).
- **Repeg accounting honest** (8677b58): every silent continue counts
  as `skipped`; tripwire chaseable = cands − walled − skipped;
  `deferred` separate and NOT subtracted (all-deferred+acted=0 = the
  $100 starve). Fri false CHASE DEAD alarm was ~3 phantom skips/lap.
- **Quote table LIVE** (phases 1-2, ws-quote-table-spec.md): opener
  962s→253s measured Fri night; MKTS_MAX_SLUGS=4000 probe-measured.
- **DNS is on Cloudflare** (Fri night, watched): zone ACTIVE,
  nameservers bill/paityn.ns.cloudflare.com, ALL records grey-cloud
  (DNS-only — orange breaks Vercel SSL), site serving all-200s.
  Rollback = GoDaddy NS ns21/ns22.domaincontrol.com. Resend email
  records imported intact.
- **Rent: record days.** Sep 3 accrued **$416.82**/137 markets (top:
  Week-3 NFL totals seeded ALONE — NO-BAL 45.5 $31.74, DET-BUF 52.5
  $28.40…), Sep 4 $155.81+. ~$570 pending. Rob doubts it; the verify
  is Monday's payout credits (the ledger has matched credits within
  pennies 3-for-3). Sep 4 bets-day was −$40 (−$18.56 Reds), net
  ~+$115 with rent.
- ⚠ **Venue 1015 bans Fri:** home IP (two manual 25-page activities
  walks — NEVER bulk-walk the venue by hand again) and Vercel IPs
  (earlier). Both lifted. Venue forensics go through the box's LOCAL
  poly_activities (ledger lane mirrors it), never live walks.

## The split-brain wrinkle (audit finding, Sat am — contained)
Vercel request-path helpers the dashboard polls (fill-status →
`_pmm_autolog` + entry auto-sync) READ THE LIVE VENUE and WRITE the
frozen cloud bot_picks — creating/adopting rows, syncing entries. No
orders place from that side (lease pins hold, +7d, until C2). So the
cloud copy is a half-alive zombie: it fooled two reads Fri night. The
"corrupted −1233 entry" on the lost Reds pick was actually this sync
writing the venue's REAL 92.8¢ basis (matches the app's "93%"). C2
closes the whole class.

## Fri-night facts a fresh session needs
- **Reds Sep 4 game: Rob won the $100 argument.** Venue: −$18.56 at a
  93% cost basis. My −$7.84 came from the frozen cloud blob. HOW a
  ~40¢ dog reached a 93¢ avg basis is UNRESOLVED (in-play buys would
  violate the live-game guard — root-cause from LOCAL poly_activities
  when Rob cares; he said "fuck the reds" but the guard question is
  real). Today's MIL@CIN (Sep 5): 20 CIN ML @ 49.7¢ filled — the
  challenge bet, settles tonight.
- A's@SEA: side-flip left an away+127 live pick (order 44¢ rests) and
  a home−156 orphan (no order) — reconcile should have swept the
  orphan; verify via local psql.
- Rob hand-cancelled some NCAAF orders Fri night before the rent data
  landed — self-heal (reconcile+OMS) re-places them; verify happened.
- Manual asks resting = user takeover: Furman O80.5, WAS-DAL U34.5.
- OSU rung challenge answered with data: all 4 OSU bets enrolled but
  earning $0.05-$0.39 vs Missouri@Kansas's $11.87 — "enrolled ≠
  earning" is the cull criterion (below), never ladder geometry
  (convicted twice Fri: TSU-UGA −46.5 and NT-Indiana −39.5 were real
  lines; OU-Mich U39.5 was flagged junk and is the best open winner).

## TODAY'S QUEUE (Rob-ordered, Sat)
1. **Rent-per-bet cull**: rank ALL pending football bets by rent
   earned (join `poly_incentive_earnings` on `signal_blob->>'pmm_slug'`,
   LOCAL db) + capital tied; kill the bottom at CONFIG level
   (machine_flags / lane config) so it stays dead. NEVER hand-cancel
   in the app — self-heal re-places.
2. **C2 — the swap. Rob ruled Fri: TODAY, not Sunday** ("we keep
   pushing shit off... shit is broken anyway"). Build: cloudflared on
   the box → tunnel `db.thekahlahouse.com` → localhost:3011 (the Caddy
   shim, same JWT). Swap: Vercel env SUPABASE_URL → tunnel URL; disable
   the Actions twins' cloud writes (disable, never delete); unpin the
   cloud cellar_lease rows AT the swap per §1. Swap in a slate lull,
   rollback one paste away. Trade-off Rob accepted: site up only when
   box+home internet up.
3. **#8 WS phase 3**: repeg pegging + scalp walk from the quote table
   (the 28-min-lap fix) + re-wire the WS POSITION wake to poke scalp.
4. **Scalp coverage invariant** (Rob's Sat-morning demand, design
   agreed): scalp lap stamps `coverage {positions, covered, exempt:
   {nrfi, props, manual}, naked, naked_slugs}` from the venue snapshot
   (venue = denominator, never pick rows); health card prints it; RED
   when non-exempt naked survives 2 consecutive laps; one batched TG
   line when naked RISES. Exemptions = doctrine: NRFI never sells,
   pitcher props don't scalp, manual ask = user takeover.
5. **#9 remainder — memory diet**: daemon RSS 1.5-1.8GB on the 8GB
   box; unbounded caches to bound/evict (_LADDER_STRUCT, WS_QUOTES,
   _base_seen, book caches). Subprocess isolation LAST — re-measure
   after #8+C2 before deciding how much is still needed.
6. **#7 close-out**: OMS ordering premise stale (tries-first shipped
   Sep 1); enrolled backlog was draining 148→130 Fri night — confirm
   ~0 via LOCAL psql and close. Also: `oms_pend` gauge echoes its own
   limit(200) — cosmetic fix; 5 `failed:*` desired rows (mine_q 2,
   ? 2, cap_q 1) worth one look.

## Watches
- Monday: payout credits ≈ $570 accrual (the mother-lode verify).
- Tonight: MIL@CIN settles (Rob's challenge bet).
- Fri sheets: NCAAF Friday update published (91 games, 80 diffs);
  NFL update presence unverified. Mon Sep 7 full build is the second
  unattended Routine test.
- fbprop turn-rate read (exec_probe_runs kind=fbprop_funnel, local).

## Box crib sheet
venv `/Users/robkahla/dev/kahla-house/.venv/bin/python`; restart
`sudo -v && cd ~/dev/kahla-house && git pull && sudo launchctl
kickstart -k system/com.kahlahouse.cellard`; log
`/usr/local/var/log/cellard.log`; stack dump `sudo kill -USR1
$(pgrep -f "m cellar")`; local truth `psql kahla`; ⚠ bootout UNLOADS
(re-bootstrap per §1). One command at a time at Rob's terminal. Rob
must NOT log picks / 💰 Bet on the site until C2.

---

## ⚡ Sat Sep 5 — SESSION PROGRESS (fresh window, ~8-9am AZ)
Verified from LOCAL psql (`/Applications/Postgres.app/Contents/Versions/
latest/bin/psql kahla` — not on PATH): all 11 lanes ticking, opener/scalp
laps 27-28 min, grader every minute, ledger mirror current (32,406
activities, newest 08:06).

**Queue #1 — RENT CULL: BUILT, DRY, awaiting pull+restart** (see
CLAUDE.md OMS §(g)). The ranking (156 pending football bets, $1,441 tied,
$474 rent): NFL totals = 32 bets / $371 (78% of all football rent); NCAAF
spreads 47 / $34; NCAAF totals 46 / $48; NFL spreads 30 / $17. Dry judge
convicted 16 (all NCAAF junk rungs, ~$180, $0.43 rent). To go live after
the restart: `update machine_flags set value = value || '{"dry": false}'
where key='rent_cull'` — first kill pass within the hour (max_kills 10,
then 6 more the next hour). Not judged: bets placed Sep 4-5 (< 2 ledger
days) and FILLED positions (they earn nothing because they filled).

**Rob's two flags, answered:** (a) "Friday bets 0.00" — local AND cloud
mirror both read Sep 4 bets **−$39.29** as of the 08:11 compute; the
day map is local-only (`poly_gameday_pnl` RPC + local poly_activities),
so an earlier 0.00 = the ledger mirror hadn't caught up on Sep 4
resolutions during Friday's 1015 ban window. Not a bug in the math.
(b) Thu/Fri rent ($414.61 + $2.21 paid Sep 3; $155.81 Sep 4) is the
VENUE's own `/v1/incentives/earnings` PENDING rows, re-synced 07:53 —
not our arithmetic. $308 of Sep 3 is 29 Week-3 NFL totals (NO-BAL 45.5
$31.74 …) whose books are empty near the touch (0.4 shares within 3¢ on
the bid side at 8am Sat). The one Sep 3 market that already went
PENDING→PAID (idaho-utah +31.5) paid $0.39 against $0.08 pending — the
venue revised UP, not down. Monday's payout credits are the proof.

**Finding — OMS backlog NOT draining:** 135 never-tried desired rows
(120 of them Sep 12 CFB: 48 ML / 34 spread / 34 total), executor did 179
touches / 37 placed in 12h ≈ 3 touches per 27-min lap. Budget-bound, not
broken. Interim lever at the restart: `OMS_BUDGET_S=120` in the box .env
(docket §3) — 3× throughput inside the same lap.

**Fix riding along:** `_probe_log` required a Flask request context, so
every box-side stamp through it (the fbprop funnel) silently never landed
— now stamps `{"ctx":"cellar","kind":…}`.

**C2 — READY TO EXECUTE WITH ROB (needs his Cloudflare login + Vercel
env paste; nothing started):**
1. `brew install cloudflared` on the box; `cloudflared tunnel login`
   (browser, Rob's Cloudflare account — the zone moved there Fri).
2. `cloudflared tunnel create kahla-db` → `cloudflared tunnel route dns
   kahla-db db.thekahlahouse.com` (tunnel CNAMEs are proxied by design;
   the grey-cloud rule is for the Vercel records only).
3. `~/.cloudflared/config.yml`: ingress `db.thekahlahouse.com →
   http://localhost:3011` (the Caddy shim — strips `/rest/v1`, proxies
   PostgREST 3010, same JWT as the box: `~/.kahla/rehearsal.jwt`),
   catch-all 404. Run as a user LaunchAgent (KeepAlive) like postgrest/
   caddy; verify `curl https://db.thekahlahouse.com/rest/v1/machine_flags
   -H "apikey: $(cat ~/.kahla/rehearsal.jwt)"`.
4. Slate lull: Vercel env `SUPABASE_URL=https://db.thekahlahouse.com`,
   `SUPABASE_SERVICE_KEY=<rehearsal.jwt>` → redeploy. Rollback = paste
   the old two values back.
5. Silence the cloud writers: cron-job.org's 1-min `scanner-poll`
   dispatch (its resolver/ESPN steps are the grader lane now) and the
   `football-sheets-data` Mon schedule (reads DB — fine through the
   tunnel). Every other workflow schedule is already commented out.
6. Unpin nothing: after C2 the "cloud" IS the box DB; the pinned rows
   live in frozen Supabase and stop mattering. Supabase Pro dies per
   §12c once the farewell dump is verified.
Trade-off Rob accepted Fri: site up only while box + home internet up.

**✅ C2 EXECUTED Sat Sep 5 ~9:00-9:15am AZ (Rob at the terminal).** Steps
1-4 done: cloudflared 2026.8.3 (prebuilt binary — brew wanted to compile
Go from source for 30 min mid-slate, killed), tunnel `kahla-db`
(c4cfd1f5…), CNAME routed, LaunchAgent up (4 edge connections PHX/LAX),
Vercel env swapped + redeployed (`dpl_HKoW3K…`). PROOF: Caddy access log
shows Vercel's per-minute `cellar_claim` calls arriving with `Cf-Ray`.
⚠ THE HOLE WE CLOSED FIRST: unauthed GET through the tunnel returned 200
— PostgREST's `db-anon-role` was `robkahla` (superuser). Fixed with a
grant-less `web_anon` role + SIGUSR2 config reload (no restart); unauthed
now 401 locally and publicly. Step 5 (cron-job.org `scanner-poll` off)
still Rob's. Also: box → GitHub over SSH now (keychain had no credential).

**✅ SPEED ROOT CAUSE FOUND (queue #3) — not the GIL per se, the quote
table's 90s AGE rule.** Journal proof: repeg `ms: 33569` (its chase work)
inside a 1,898s lap; opener `fs_hit 867 / fs_rest 2005` per lap. Every
QUIET market missed the table at 90s and paid a REST book read; 3 lanes ×
~700 REST bodies parsing under one GIL = 30-min laps — 20-min laps at 3am
with no slate were the tell. Single REST read measured 0.08-0.22s
standalone; ~1s under the thrash. Fix (73/73 selftests): PRESENCE
freshness — socket epoch + liveness + still-subscribed ⇒ a quiet row is
current. Expect fs_rest → ~0 and laps → minutes at the next restart;
the OMS executor (2 touches in 120s) should stop starving for the same
reason — measure `oms_touch` per lap after.

**✅ THE REAL WHALE (found 9:35am, after the presence fix alone left laps
at 24 min): `_pmm_autolog`.** Second SIGUSR1 dump: repeg, alerts AND the
kalshi_autolog lane all inside `_pmm_autolog` ← `_compute_fill_status`.
Every call rebuilt the slug index with a `pmm_markets.lookup` per active
market in EVERY sport (−12h..+48h ≈ 200 rows on a CFB Saturday; venue
misses re-pay every call), three lanes concurrently, though 225 of 232
intended slugs already had picks. Measured live: one call of the old
shape ran >10 min. Restructured (73/73): known-slug fast lane (stamp +
entry sync straight from the pick row), index ONLY unknown slugs over the
sports they name, one builder at a time (lock), 30-min backoff per
unmatched slug, and an OUT-OF-WINDOW short-circuit (slug date outside
−12h..+48h never earns a lookup). Result: **0.2 s per call** (233
intended / 220 exists / 7 unknown, all out-of-window). Also: the
fill-status book warm-up now skips slugs the quote table vouches for.

**⚠ THE 7 STRAYS ARE A MONEY BUG (open):** 5 AUTOMATIC BUY orders
(20 contracts each, one partially filled) + 1 position (ore-okst total
80.5) are alive on the venue with NO pick — reconcile deleted the picks
as `venue_killed_order` (Sep 2-4; `orders.list` has no pagination —
221 returned, no cursor — so these were transient venue reads surviving
the 12-min two-strike). Consequences: no chase, no scalp cover on fill,
and the OMS re-bet 4 of the 5 games on another rung (reentry:pick_gone →
dedup saw no pick). The autolog can never adopt them: its window is
MLB-shaped (−12h..+48h) and these games are 5-15 days out — docket #6
exactly. NEXT: adopt the 6 via the pick-4879 recipe (source pmm_autolog,
manual_adopt, pmm_slug, order_id, contracts, gridiron_autobet so repeg/
scalp/OMS see them), then build football ghost adoption by slug→market
(the slug names the game + date; no venue lookup needed) into
`_reconcile_tick` or the autolog.

**✅ 09:57 restart receipts:** opener 278→366s, repeg 309→376s, scalp
405s, alerts 21-84s, kalshi_autolog 2s (from 30-40 min). Zero errors.
Remaining cost = fill-status REST (`fs_rest 1031` vs `presence 443` per
opener window) — ROOT: the log said `max subscriptions per connection
reached` for g13..g16 and **g16 was CORE**: the venue caps subscription
REQUESTS (~12/conn), not slugs; football ladder groups took the seats
and our order slugs never subscribed. Fix (81/81 selftests): request
budget 10, core-first eviction, core one-request rebuild, rejection
frames handled. Expect fs_rest → ~0 and laps ~1-2 min at the next
restart. Ghost adoption of the 6 strays awaits Rob's go.

**✅ 10:24 boot (cb83388) receipts — core subscribed FIRST (232 slugs in
1 group, 16s after boot).** repeg lap 1 **44s** (8am: 31-36 min), alerts
8-58s, kalshi_autolog 0-3s, opener 264s, scalp 359s; fill-status now
316 hit + 175 presence vs 140 REST. Boot hole found + fixed on the way:
the private ORDER snapshot has NEVER completed (no "snapshot complete"
in the whole log) and the repeg lap's boot-time orders read can fail in
the 11-lane burst, so core had no pusher on the first lap — now any lane
holding a fresh orders read pushes it (alerts every minute). Rob
cancelled the 5 ghost orders and hand-asked the Oregon position himself.
OPEN after this: repeg lap 2 = 334s in `targeted:209` mode with 9s of
chase — fill-status over 209 dirty slugs; `fs_rest_miss`/`fs_rest_outbid`
counters + `t_setup`/`t_fs` journal timers added (observability only,
next restart) to say which. Scalp 359s = REST book per position by
design (needs the ask ladder to skip our own level — table-first there
is a careful change, not a swap). OMS: never-tried 135 → 39, pending
mix is honest verdicts (151 rent / 93 no_book / 30 no_model).


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

SAME-NIGHT FOLLOW-UPS (Sep 3 evening, Rob: "pointless to be frozen" /
"I got hours"):
- **SITE MIRROR live** (1567861): the box pushes poly_dash_cache to the
  cloud after every local write (get_supabase_mirror, env-gated via
  SUPABASE_MIRROR_URL/_KEY in the box .env — the old cloud creds). One
  table, one writer, display-only. Dashboard confirmed live again at
  17:56 (fresh balance/orders on the cloud row). Also restores session
  observability of the box (derived.cellar rides the mirror).
- **GRADER LANE built** (this commit): the two per-minute Actions jobs
  that never moved — `python -m scripts.bot_picks_resolver` every tick
  + `ingest_espn_markets --commit` every 5 min — as a cellar lane using
  batch.py's subprocess pattern, inheriting the daemon's LOCAL env.
  Enable: add `,grader` to CELLAR_LANES in the box .env + pull +
  kickstart. Without it, local picks never grade (cap slots never free,
  dayof_wait rebets stall) and the ESPN spine freezes.
- Batch lane already owned every DAILY ingest/compute (diamond 3:50am,
  whiff, football props, power ratings, spines) and its subprocesses
  inherit the daemon env — those went local automatically at cutover.
  The Actions twins now run harmlessly against the frozen cloud; disable
  their schedules at C1/C2 proper (spec §8: disable, never delete).
- Still open after grader: site PICK-BOT pages (games list/pending/
  dossiers read frozen cloud bot_picks/markets), resolver_runs header
  on /handicapper reads cloud (stale "graded Nm ago" — cosmetic), Rob
  must NOT log picks / use the 💰 Bet button on the site until C2 (they
  write the frozen cloud; the box would never see them). Friday sheets
  read frozen cloud data (power ratings frozen at Sep 3 — acceptable
  for one week or C2 lands first).

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

## 2. ✅ WS cap probe + quote table — PHASES 1-3a EXECUTED Sep 4 2026
## (the GIL-cliff Friday, watched with Rob 5pm-9pm+)
The whole arc ran in one night, forced by the box wedging at peak-Friday
inventory (132 pos/157 orders — every venue-REST lane starved under one
GIL; sample profiler: one hot JSON parser, 26k cvwait). Landed, in order:
- **scalp = its own lane** + dead-scalp tripwire (uncovered-keyed) +
  budget-clock restart (chase-night bug 3rd sighting) + naked-first walk
  + 90s budget + ctx.detail journal stamp.
- **cap probe** (`cellar/ws_cap_probe.py`): 4,000 slugs OK on one
  connection; >~4k venue fails SILENTLY; **overlapping subscribes
  REJECTED per-connection** — convicted the old rotation of dead-airing
  the feed on every watch-list change. MKTS_MAX_SLUGS=4000 measured.
- **quote table + group subscriptions** (`c151e34`): WS_QUOTES,
  _ws_quote 90s staleness, table-first `_gridiron_price_game` +
  `_LADDER_STRUCT`; opener lap **962s → 253s** measured night one.
- **SIGUSR1 stack dumper** (py-spy can't attach under SIP) — dump named
  three whales, all cut in `28f152f`: get_client() fresh-TLS-per-call
  (now module-cached), fill-status outbid walk (now WS-quote-first —
  repeg AND alerts both ride it; fs_hit/fs_rest in _WS_PRICE_STATS),
  dash day-card pydantic scan every 60s (memoized 240s).
- Triage that saved the evening: tape lanes (pm_snapshot/vsin/
  kalshi_autolog) BENCHED from CELLAR_LANES during game hours — restore
  when the slate quiets (task #9); the day's CLV closes are lost.
Remaining phase 3: repeg PEG targets + scalp walk from the table
(task #8, watched); paperlog/pm_snapshot adoption; parallel writes.

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
