# Box handoff — Sep 25 2026 engine review

**Read this in a LOCAL Claude Code session on the house box** (the cloud
session that wrote it cannot reach the box, its Postgres, or the daemon).
Everything below is on `main` already. Your job here is: pull, prove,
restart, verify, then clean the pair table. Rob is watching; report each
step's actual output, never a summary of what "should" have happened.

Companion doc (findings, ranked, what is FIXED vs OPEN):
`docs/review-2026-09-25.md`. Read §1, §3 and §4 first if you have not.

Standing rule that shaped every change here (Rob, today): **"early comes
and goes and comes and goes… ALL THE TIME… which is why step ONE is:
does the market pay rent."** Nothing in the code or docs may treat a
rent reading as a program table. `_rent_ok` asks per market at every
placement; `_pair_min_lead_h` re-reads the live schedule every seeder
lap. If you find yourself writing "early is gone", stop.

---

## 0. What is on `main` since the box's boot SHA (6f25bb9, Sep 22)

| commit | what |
|---|---|
| `08a34d3` | `/api/pair/status?game=` read-only pair table + dry re-rung verdict |
| `c587af4` | football-sheets refresh cron fix (not engine) |
| `758a7b1` | **pair floor / re-rung / amend fixes, football wall gates, buy-amend sizing** |
| `fb00f36` | round 2: paged pending reads, scalp gone-path re-read, adopted-ask indicator, recreate keeps GTD, OMS kickoff floor, 60s rent-read-failure cache |
| `54ae540` | review doc: box vitals via `/api/cellar/health` |
| `fa0e7b2` | review doc: rent reading is a morning's answer, not a table |

Engine changes in one breath (all in `app.py` unless noted):

- **Pairs (`_pair_*`)**: amends send `n + cum` (modify's `quantity` is
  the order TOTAL — a partial fill was shrinking the bid every lap);
  held-leg exit after a partner sale floors FLAT (pair cost − sold,
  `sold_cost` remembered), not `cap − sold`; `_pair_rent` is tri-state
  and an unreadable venue KEEPS orders; off-touch cancels PARK the leg
  10 min (`parked_at`), creates get a 20s grace, a vanished bid re-reads
  positions before minting a new lot; lap budget 150s + rotation
  (`deferred` in the stamp); ONE ENABLED ROW PER SLUG — oldest owns it,
  twins reported as `dup_rows`; leg SIDE comes off the label
  (`_pair_leg_side`), never the key; `_pair_rerung` walks every option;
  $13 master rule on pair BIDS only; seeder lead floor
  `_pair_min_lead_h` = 6h while the family pays early, else
  `pair_min_lead_dayof_h` (3h), `pair_min_lead_h` flag overrides.
- **Ferrari / OMS**: under `pairs_own_football`, `_oms_pass`,
  `_gridiron_recenter_tick` and `_gridiron_bet_sweep` stand down BEFORE
  pricing or cancelling (`pairs_own`, 4h retry) — the recenter had been
  cancelling seats the executor could not re-seat.
- **Repeg / snipers**: chase amend sends `qty + order_cum`; buy sniper
  sends `qty + cum`; buy-snap publish honours a real pop
  (`SCALP_POPPED` vs the read's mono time).
- **Scalp / reconcile / recenter / rent sweep**: pending-pick reads go
  through `_sb_paged` (the 1,000-row cap was truncating the far book);
  `_scalp_amend` "gone" path re-reads positions; adopted manual asks
  carry `manualOrderIndicator`; recreate passes the derived GTD; OMS
  row select requires ≥60 min to kickoff; a failed
  `_rent_market_periods` read is cached 60s, not 10 min.
- **`cellar/runner.py`**: a `wake()` landing mid-tick is sticky
  (`_wake_pending`) instead of dropped; watch-list MERGE callback
  (`_WS_WATCHLIST_MERGE_CB`) so the pair lane adds slugs without
  evicting the repeg lap's list.
- **`cellar/wsfeed.py`**: `_apply_ops` re-queues cap-hold / post-eviction
  ops at the END of the queue instead of losing them.
- **`cellar/selftest.py`**: expectations updated + 4 new tests
  (`test_pair_leg_side`, `test_buy_amend_sends_the_total`,
  `test_football_wall_is_checked_before_the_price`,
  `test_pair_tick_guards`).
- New route `/api/cellar/health` (shared-secret): boot SHA, lane vitals,
  every `machine_flags` row, pair-row counts.

---

## 1. Pull

```bash
cd ~/kahla-house
git status            # ⚠ report ANY local modification before pulling
git log --oneline -1  # expect 6f25bb9 or later
git pull --ff-only origin main
git log --oneline -1  # expect fa0e7b2 or later
```

**If `git status` shows `app.py` modified locally:** the scalp lane
logged `UnboundLocalError: seen_qty` on this box, and that name is
initialised in every committed version of `_scalp_amend`. A local
uncommitted edit is the leading theory. Show Rob the diff
(`git diff app.py | head -80`) before doing anything with it; do not
stash it silently.

## 2. Prove it

```bash
python3 -m cellar --selftest 2>&1 | tail -25
```

Expected: all pass. The cloud session's last complete run was 413
passed / 5 failed BEFORE the five expectations were updated; the re-run
after the update was blocked by the sandbox, so **this run is the first
green proof**. If anything fails, stop and show the failure — do not
restart the daemon on a red selftest.

## 3. Restart the money daemon

```bash
kill -TERM $(pgrep -f "MacOS/Python -m cellar")
# launchd KeepAlive restarts it (~90s). Then:
sleep 100; pgrep -fl "MacOS/Python -m cellar"
```

The tape daemon (`com.kahlahouse.cellard-tape`) runs the same code and
should be kicked too:

```bash
launchctl kickstart -k gui/$(id -u)/com.kahlahouse.cellard-tape
```

Do NOT send `kill -USR1` to either (it kills the daemon — CLAUDE.md
Sep 7 note).

## 4. Verify the boot

```bash
psql kahla -c "select stamped_at, detail->>'sha' as sha, detail->>'mode' as mode
  from exec_probe_runs where kind='cellar_boot' order by stamped_at desc limit 2;"
```

`sha` must be the pulled commit (`git rev-parse --short HEAD`), `mode`
must NOT be dry. The dashboard lane card prints the same (`code <sha>`).
Also confirm the boot banner in the log shows `mode=` live:

```bash
tail -50 ~/.kahla/logs/cellard.log | grep -i "boot\|mode="
```

(If the log path differs on this box, `launchctl print
gui/$(id -u)/com.kahlahouse.cellard | grep -i log` finds it.)

## 5. Clean the pair table

Twins found live (younger row on a slug an older enabled row already
owns): **90 (twin of 82), 96 (twin of 70), 98 (twin of 51)**. The lap now
skips them as `dup_rows`, but they still sit in the table and confuse
every read. Disable them:

```bash
psql kahla -c "select id, enabled, created_at, legs->0->>'slug' as a_slug, legs->1->>'slug' as b_slug
  from pair_hedges where id in (51,70,82,90,96,98) order by id;"
# eyeball: 90/82, 96/70, 98/51 share slugs; the LOWER id is the owner
psql kahla -c "update pair_hedges set enabled=false where id in (90,96,98) returning id;"
```

Rows **86 / 93 / 97** were hand-inserted with keys swapped (`a` is the
home/under leg). The code reads the side off the LABEL now, so they
work; they are NOT twins. Leave them enabled unless Rob says otherwise.
(93 and 97 each had a twin — 58 and 71 — and 93/97 are the YOUNGER
rows, so if Rob wants the table clean, disable 93 and 97 too and keep
58/71. Ask before doing that; it changes which row's `state` lot memory
survives.)

## 6. Watch the first pair lap

```bash
psql kahla -c "select stamped_at, detail from exec_probe_runs
  where kind='pair_tick' order by stamped_at desc limit 1;" | head -60
```

(if the stamp kind differs, `select distinct kind from exec_probe_runs
where stamped_at > now() - interval '1 hour'` lists what the lane
writes). Look for:

- `dup_rows` — should be EMPTY after step 5.
- `deferred` — should be 0 on a slate this size; a non-zero number
  means the 150s budget is binding and the lane needs the wake scoping
  fix listed as OPEN in the review doc §4.
- `rerung` > 0 wherever a leg's next rung pays and the partner is
  unheld; `rent_pulled` on unheld legs is the RULE WORKING when the
  venue answers day-of only for that market — re-check that market
  with `/api/rent-check?slug=…` before calling it a bug.
- Write volume: the previous box was doing ~766 pair writes/hour.
  After the parking/grace fixes it should be a small fraction. Count:

```bash
psql kahla -c "select count(*) from scalp_snipes where ts > now() - interval '1 hour';"  # sells
```

and read `writes`/`cancels`/`creates` off the pair stamp for bids.

Also run once, read-only:

```bash
python3 - <<'PY'
import app, datetime as dt
print(app._pair_min_lead_h("NFL", app._sb()), app._pair_min_lead_h("NCAAF", app._sb()), app._pair_min_lead_h("MLB", app._sb()))
PY
```

6.0 means the family's early program is live in the newest schedule
scrape; 3.0 means only day-of is (the flag `pair_min_lead_dayof_h`
sets that number; `pair_min_lead_h` overrides both).

## 7. Decisions Rob owns (each reversible by a `machine_flags` row)

1. **Seeder lead floor follows the program** — `pair_min_lead_dayof_h`
   (3h) when only day-of pays; set `pair_min_lead_h` to force a value.
2. **Flat floor after a partner sale** (pair cost − sold), not
   `cap − sold`. Revert = code change, not a flag; say so if he wants
   the profit floor back.
3. **$13 master rule on pair BIDS only**, asks exempt.
4. **Football sizing while the per-market answer is day-of** — the
   review doc §5 lays out the choices; nothing was changed.

## 8. OPEN items (not started — `docs/review-2026-09-25.md` §4)

Lease owner per process, `_autobet_execute` one-order-per-slug,
virgin-seeder stub, event-cap nominal stakes, mirror write ordering,
lot-ledger clamp, pair-lane wake scoping, seeder budget, MLB totals
path never seating, socket sleep/audit loop, intent journal. Pick by
Rob's priority, not by list order.
