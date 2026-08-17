# The Boiler Room — moving the machine home

**Status:** PLAN ONLY (Aug 17 2026). Nothing in this doc is built. No code
has changed. This is the map for when we start.

---

## 0. The name

**The Boiler Room.** The room in a house where the machine lives, and the
oldest slang there is for a room full of people working the phones for money.
It sits under Kahla House the way a boiler sits under a house.

Naming that falls out of it, so the whole thing is consistent from day one:

| Thing | Name |
|---|---|
| The box in the house | `boiler` (hostname) |
| The long-running process | `boilerd` (systemd/launchd service) |
| The repo package | `boiler/` |
| Heartbeat table | `boiler_ticks` |
| The single-writer lock | `boiler_lease` |
| Telegram prefix on its pings | `🔥` |

Runners-up if this one doesn't land: **The Cellar** (quieter, same idea),
**The Pit** (trading floor, loses the house metaphor), **The Furnace** (too
close to Boiler without the double meaning).

---

## 1. What "the machine" actually is

Grounded by reading the repo today, not from memory. The machine is not one
thing — it's **four** systems that happen to share a database.

### 1a. The hot path (the part that actually bets)

A single HTTP endpoint, `GET /api/handicapper/paperlog`, pinged once a minute
by cron-job.org → GitHub Actions → curl → Vercel. Everything that touches
money rides that one request:

| Engine | Function | Cadence today |
|---|---|---|
| Live suggestion paperlog | inline in the route | every tick |
| Opener lane + auto-bet | `_opener_pass` | every tick |
| Gridiron opener shadows | `_gridiron_opener_pass` | every tick |
| O/U trader | `_ou_trader_eval` | inside opener |
| Whiff/Outs/Hits/Walks auto-bet | `_whiff_autobet` | inside opener |
| Re-peg bot | `_repeg_tick` | `minute % _OUTBID_TICK_MOD` |
| Harvest (take-profit sells) | `_harvest_tick` | `minute % _HARVEST_MOD` |
| Money ledger | `_poly_ledger_tick` | `minute % _POLY_LEDGER_MOD` |
| Outbid pings | `_outbid_alerts` | `minute % _OUTBID_TICK_MOD` |
| Unlogged-bet alerts | `_bet_alerts` | every tick |
| Watchdog | `_opener_watchdog` | every tick |
| Telegram batch flush | `_tg_flush` | every tick |
| Incentives sync | `_incentives_sync` | `minute % _INCENTIVE_SYNC_MOD` |

Three sibling pings on the same job: `/api/pm-snapshot` (1 min),
`/api/handicapper/kalshi-autolog` (2 min), `/api/vsin-snapshot` (15 min).

**This is the thing that moves.** Everything below is context.

### 1b. The batch jobs (27 GitHub Actions workflows)

- **1-min hot path:** `scanner-poll.yml` — ESPN spine ingest + `bot_picks_resolver`, plus the `fill_ping` curl job above.
- **Daily computes:** diamond-iq, whiff-iq, power-ratings, ufc-model.
- **Daily/weekly ingests:** mlb-pitchers, mlb-batters, savant-xwoba, nhl-goalies, nhl-shots, ufc-stats, espn-markets.
- **On-demand backtests:** 9 workflows, `workflow_dispatch` only.
- **Housekeeping:** snapshot-cleanup (nightly), tune-prime-window (weekly), site-curl (the sandbox→site bridge).

### 1c. The website

`thekahlahouse.com` — Flask on Vercel serving Pick Bot, Dashboard, Book Club,
Grocery, Games to Rob **and to family/friends**. Firebase Auth in front,
Firestore behind for users/book-club/grocery.

### 1d. The data

- **Supabase Postgres** — every table the machine reads and writes.
- **Firestore** — auth/users/book-club/grocery only.

### The constraints we are paying for today

| Constraint | Where it bites |
|---|---|
| ~10s serverless request budget | `paperlog` deadline `8.0s` (25s when the live window is empty), opener gets `+14.0s`, pm-snapshot PMM loop `7.5s`, re-peg `20.0s`, top-up `5.0s` |
| No process memory between ticks | Every cache cold-starts; `_EVENT_CACHE`, `_PMM_BOOK` etc. only survive on a warm container by luck |
| Minute-modulo scheduling | Real cadence logic expressed as `now.minute % N` against the **UTC** clock, inside a request handler |
| No concurrency | Games processed serially inside one request, budget-truncated, random-shuffle rotation to be fair about starvation |
| Runner IP reputation | ESPN started 403'ing GitHub runners ~Aug 4 and the spine went dark for six days behind a green checkmark |
| No local state | Every intent must round-trip Supabase; a kill between cancel and create is the ORDER LOST state |

---

## 2. The decision: split the machine from the site. Do not lift-and-shift.

**Move:** the hot path + the batch jobs.
**Leave on Vercel:** the website, Firebase Auth, Firestore.
**Leave in the cloud:** Supabase.

Why not move everything:

1. **The site has users who aren't you.** Book Club and Grocery are family-facing. A house box means home internet, home power, and an inbound tunnel between your wife and her grocery list. That's a new failure mode with no upside.
2. **Supabase stays put or nothing else works.** It's the seam that lets both halves run at once — which is what makes a phased cutover and an instant rollback possible. Move Postgres home and you've coupled the migration to a data migration and burned the safety net. It also kills `run_sql.sh` from Claude sessions.
3. **The machine is the only part that's actually constrained.** Every limit in the table above is a limit on the machine, not the website. The site is a low-traffic Flask app; serverless is genuinely the right shape for it.

What we get from moving just the machine:

- **No time budget.** `8.0` becomes "process every game, properly." The random-shuffle rotation and the starvation problem it works around both go away.
- **Real scheduling.** A 15-second re-peg loop and a 6-hour compute in the same process, both expressed as schedules instead of `minute % N`.
- **Process memory.** Order-book caches, PMM event cache, model snapshots — loaded once, held.
- **Concurrency.** Fan out across games instead of iterating until the clock runs out.
- **A residential IP.** The ESPN 403 class of failure disappears.
- **Crash-safe intent.** A local journal written before a venue write, replayed on boot — the real fix for ORDER LOST, which serverless can't have.
- **Claude sessions get direct access.** No `site-curl.yml` bridge, no shared-secret endpoints just to see a number. `run_sql.sh` and the venue readers run in the same shell as the code.

---

## 3. Target architecture

```
   HOUSE                                      CLOUD
   ┌────────────────────────────┐             ┌──────────────────┐
   │ boiler (Mac mini / Ubuntu) │             │ Vercel (Flask)   │
   │                            │             │  thekahlahouse   │
   │  boilerd  ──────────────┐  │             │  • website       │
   │   scheduler             │  │             │  • Firebase auth │
   │   ├ 15s  repeg          │  │             │  • cold standby  │
   │   ├ 60s  opener/autobet │  │             └────────┬─────────┘
   │   ├ 60s  pm-snapshot    │  │                      │
   │   ├ 60s  paperlog       │  │                      │
   │   ├ 2m   harvest/ledger │  │                      │
   │   ├ 15m  vsin           │  │                      │
   │   └ daily computes+ingests                        │
   │                         │  │                      │
   │  import app  ───────────┘  │                      │
   │  (calls _opener_pass etc.  │                      │
   │   directly, no HTTP)       │                      │
   └───────────┬────────────────┘                      │
               │                                       │
               └──────────► Supabase Postgres ◄─────────┘
                            (shared truth + the lease)
```

**The key structural fact, verified today:** `_opener_pass`, `_repeg_tick`,
`_harvest_tick`, `_poly_ledger_tick`, `_bet_alerts`, `_tg_flush` are pure
functions of `(sb, now)` with **zero** Flask `request`/`g` coupling. `boilerd`
does `import app` and calls them directly. **No engine rewrite is required to
move.** The migration is a scheduling and ownership change, not a port.

---

## 4. Hardware

**Recommended: Mac mini M4, 16GB / 256GB — ~$600.**

- ~7W idle, silent, sits on a shelf and is forgotten about.
- macOS means the SwiftBar widget in `widget/` runs on the same box that
  generates its data.
- Everything we run is pure Python; Apple Silicon is a non-issue.
- `launchd` KeepAlive is a perfectly good supervisor.

**Alternative: refurb SFF PC (ThinkCentre/Beelink), Ubuntu 24.04 LTS — ~$250.**
Cheaper, `systemd` is nicer than `launchd`, louder, and you're managing a
Linux box. Either is fine; the plan below works on both.

**Non-negotiable accessories:**

- **UPS** (~$80). A brownout mid-`cancel→create` is the ORDER LOST state with
  real money resting on the venue.
- **Wired ethernet.** No Wi-Fi for a process that places orders.
- **Router DHCP reservation** so the box has a stable LAN IP.
- **Tailscale** (free tier) so you can reach it from your phone without opening
  a single port on the house.

---

## 5. The safety invariant: one writer, ever

This is the part that must be right before anything else ships.

The failure mode is already documented in this repo: overlapping write batches
produced **9 duplicate resting orders across 8 markets inside two minutes**,
and Polymarket's own guide warns about it. During cutover, Vercel's tick and
`boilerd` would both be live. If both run `_opener_pass`, you get double
orders on real money.

**The mechanism: a database lease.**

```sql
create table boiler_lease (
  lane        text primary key,        -- 'opener','repeg','harvest',...
  owner       text not null,           -- 'boiler' | 'vercel'
  heartbeat_at timestamptz not null,
  ttl_seconds int not null default 180
);
```

Rules:

1. Before any engine runs, its caller claims the lane: `owner=me` if the row is
   unclaimed **or** `heartbeat_at < now() - ttl`. Atomic, single UPDATE with a
   WHERE clause. Loser is a no-op, not an error.
2. `boilerd` renews its lanes every tick.
3. **Vercel's paperlog route keeps running unchanged**, but each engine call is
   wrapped in the same claim. While `boiler` is healthy it always loses the
   claim and does nothing.
4. If the house loses power or internet, `boiler` stops renewing. **Three
   minutes later Vercel automatically reclaims and resumes.** Failover is
   free and requires no human.
5. Lane-granular, so cutover is per-engine: move `pm-snapshot` first, leave
   `opener` on Vercel, and both are correct simultaneously.

Add to that:

- **A global venue-write mutex inside `boilerd`.** One in-process lock around
  every Polymarket write. Concurrency is for reads and model math; venue writes
  stay strictly serial. This is the existing "RUN WRITE ENDPOINTS SERIALLY" rule,
  finally enforced by a lock instead of by discipline.
- **An intent journal.** Append to a local SQLite/JSONL file *before* a cancel,
  clear it after the paired create confirms. On boot, replay unfinished intents
  and reconcile against `orders.list`. This is strictly better than what
  serverless can do.

**Acceptance gate for the whole migration:** the lease is proven by killing
`boilerd` mid-slate and watching Vercel pick the lanes back up within one TTL,
with zero duplicate orders on the venue.

---

## 6. Phases

Each phase has an acceptance gate. Do not start the next one until the gate passes.

### Phase 0 — Prep (no hardware needed)

- Buy the box + UPS.
- Inventory every secret: `POLYMARKET_KEY_ID`, `POLYMARKET_SECRET_KEY`,
  `FIREBASE_SERVICE_ACCOUNT`, `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`,
  `KALSHI_API_KEY_ID`, `KALSHI_PRIVATE_KEY`, `FILLED_BOT_TOKEN`,
  `FILLED_BOT_CHAT_ID`, `FILLS_CRON_SECRET`, `PARLAY_API_KEY`, `RESEND_API_KEY`,
  `WALMART_*`. Decide where they live on the box (`~/.boiler/env`, `chmod 600`,
  **never** in the repo).
- Create `boiler_lease` and `boiler_ticks` in Supabase (`run_sql.sh -f`).

**Gate:** tables exist, secrets list is complete and verified against Vercel.

### Phase 1 — Batch jobs move first (zero risk)

The ~20 ingest/compute/backtest workflows only read public APIs and write
Supabase. Nothing bets. Move them and you immediately fix the ESPN-403 class
of failure.

- Clone repo to `/opt/boiler` (or `~/boiler`), Python 3.12 venv, install both
  `requirements.txt` files.
- Port each workflow to a scheduled `boilerd` job (or plain systemd timers
  first — it's fine to start dumb).
- **Leave the GitHub workflows in place, disabled-but-present**, as the standby.
- Heartbeat every job into `boiler_ticks`.

**Gate:** one full week where every daily compute/ingest lands from the house,
`boiler_ticks` shows no gaps, and the ESPN spine creates markets every day.

### Phase 2 — `boilerd` skeleton, shadow mode

- `boiler/` package: config, scheduler, lease client, journal, structured logs,
  Telegram on crash.
- `import app`, call the read-only engines: `pm-snapshot`, `paperlog`,
  `vsin-snapshot`. **Claim no lanes yet** — run them with writes disabled and
  diff the output against what Vercel is producing.
- Wire the health surface: `boiler_ticks` heartbeat + a chip on `/handicapper`
  next to the existing resolver heartbeat, so a dead boiler is visible on the
  phone in under a minute.

**Gate:** 48h of shadow output that matches Vercel's rows, and a deliberate
`kill -9` that `launchd`/`systemd` restarts cleanly.

### Phase 3 — Cut the lanes over, one at a time

Order matters. Each step is one lane, one week, with the lease doing the
handoff. Roll back by revoking the lease — no deploy needed.

1. `pm-snapshot` — pure logger, highest volume, zero money.
2. `paperlog` (suggestions + shadows) — still no orders.
3. `vsin` + `kalshi-autolog` + `poly_ledger` — reads and bookkeeping.
4. `harvest` — writes, but only *sells* on positions we already hold.
5. `repeg` — writes, cancel→create, the ORDER LOST surface. **The journal must
   be proven before this one.**
6. `opener` + `autobet` + `whiff_autobet` + `ou_trader` — new money. Last.

**Gate per lane:** 7 days on the boiler with venue state matching expectations,
then move to the next.

### Phase 4 — Retire the scaffolding

- Turn off the cron-job.org ping and the `fill_ping` job (keep the YAML).
- `site-curl.yml` becomes obsolete for local sessions; keep it for cloud ones.
- Vercel's paperlog route stays deployed forever as the standby claimer.
- Consider dropping Vercel Pro → Hobby once the per-minute compute is gone.

**Gate:** 30 days, no manual intervention, no missed slate.

### Phase 5 — Optional, later: unlock the things serverless couldn't do

Only after Phase 4 is boring. This is the actual payoff and it should be
separate work with its own plan:

- **Sub-minute re-peg.** 15s chases instead of 2-minute ones — directly attacks
  the adverse-selection finding, which is the biggest open problem in the machine.
- **Persistent order-book state** instead of re-reading the touch every tick.
- **Fill-time instrumentation** — the measurement the adverse-selection note
  says is missing. A long-lived process can watch fills as they happen instead
  of inferring them from polls.
- **Full-slate processing** — no budget truncation, no rotation, every game
  every tick.
- **Local model serving** — the daily computes become continuous.

---

## 7. What actually changes in code

Deliberately small. This is the argument for doing it.

| File | Change |
|---|---|
| `boiler/` (new) | Scheduler, lease client, journal, config, entrypoint |
| `app.py` | Wrap each tick engine's call site in `lease.claim(lane)`. No engine logic touched. |
| `app.py` budget constants | `8.0`/`14.0`/`7.5`/`20.0`/`5.0` become config, defaulting to today's values. **Do not raise them in the same change that moves them** — some are also rate-limit protection against Cloudflare on the venue. |
| Minute-modulo gates | `_HARVEST_MOD`, `_OUTBID_TICK_MOD`, `_POLY_LEDGER_MOD`, `_INCENTIVE_SYNC_MOD` stay for the Vercel path; `boilerd` expresses them as real schedules |
| `.github/workflows/*` | Add `if: vars.BOILER_STANDBY == 'true'` guards; delete nothing |
| Supabase | `boiler_lease`, `boiler_ticks` |
| `templates/handicapper.html` | One health chip, same pattern as the resolver heartbeat |

Timezone: set the box to **America/Phoenix**. Every "today" in this codebase is
an AZ calendar day; a local box whose clock agrees with the domain removes a
whole category of off-by-one.

---

## 8. Landmines

1. **Double execution during cutover.** The lease is the answer, and it must be
   built and tested before a single write lane moves. Nine duplicate orders in
   two minutes is the documented precedent.
2. **Silent green checkmarks.** The ESPN 403 painted six days of green while
   creating nothing. `boiler_ticks` must record *work done*, not *job ran*, and
   the watchdog must alert on a lane producing zero output — not on a lane
   erroring.
3. **Home power/internet.** UPS, wired, and the lease's automatic Vercel
   failover. A dead boiler with orders resting and nothing re-pegging them is
   the real cost of an outage.
4. **Secrets sprawl.** Two copies now (Vercel + boiler). Write down which is
   canonical and rotate both together.
5. **`import app` has import-time side effects** — Firebase initializes at line
   60. `boilerd` needs `FIREBASE_SERVICE_ACCOUNT` present at boot even though it
   never serves a request.
6. **Don't tune while you migrate.** Move the machine unchanged. Every budget,
   cadence, and threshold keeps its current value through Phase 4. If results
   change during the move you need to know it was the move.
7. **The GitHub workflows are the rollback.** Disable, never delete.

---

## 9. Cost

| | Today | After |
|---|---|---|
| Vercel | $20/mo Pro | $0–20 (Hobby likely enough once the per-minute compute leaves) |
| GitHub Actions | $0 (public repo) | $0 |
| cron-job.org | $0 | $0 |
| Supabase | unchanged | unchanged |
| Electricity | — | ~$2/mo |
| Hardware | — | ~$680 one-time (mini + UPS) |

Payback is roughly three years on the Vercel line alone — so **this is not a
cost decision.** It's a capability decision: sub-minute execution, real
concurrency, persistent state, fill instrumentation, and a residential IP.
Those are the things you're buying.

---

## 10. Rollback

At every phase, in order of escalation:

1. **Per-lane:** stop renewing that lane's lease. Vercel reclaims in ≤3 min.
2. **Whole machine:** `systemctl stop boilerd`. Vercel reclaims every lane.
3. **Total:** re-enable the GitHub workflow schedules. Back to today exactly.

No deploy, no revert, no data migration at any level. That property is the
reason Supabase stays in the cloud.

---

## 11. Explicitly not moving

- The website and its users.
- Firebase Auth / Firestore.
- Supabase Postgres.
- The resolver's ESPN grading (it's fine where it is, and it's the standby's
  only real job).
- Anything about the models, the gates, the thresholds, or the money rules.
  **This migration changes where the machine runs. It changes nothing about
  what the machine decides.**
