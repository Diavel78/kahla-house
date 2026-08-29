# Thursday Aug 27 box-unfreeze punch list

> ## ✅ STATUS (Aug 28): EVERYTHING BELOW IS BUILT AND ON `main`.
> **THE PULL + DAEMON RESTART IS THE ONLY REMAINING STEP** (Rob runs it).
> Items 1 (scalp arm, shadow), 1b (the double + topup fix), 2 (sweep in
> lane_opener) and 3 (reconcile pass) all shipped Aug 28;
> `fbprop_config.contracts` is already 4 in the DB. Items 4-8 stay open
> as normal work; item 9 stays parked. After the pull:
> - the box starts placing at DOUBLED stakes under the $13 Master Rule;
> - the football chase (`_REPEG_MARKET_TYPES` spread/total, shipped
>   Aug 26) goes live for the first time — the box owns the repeg lease;
> - the week-of sweep finally runs somewhere while the box is healthy;
> - the reconcile pass runs every 15 min inside the repeg lane
>   (ghost-order self-heal; `reconcile_bak` DDL already applied);
> - the scalp arm shadows every rent-lane fill
>   (`signal_blob.scalp_shadow`) — 2-3 days of tape, then Rob's call to
>   flip `SCALP_ENABLED=True`;
> - then run `/api/polymarket/topup` (dry first) to resize the resting
>   unfilled book to the new stakes.
> NOTE: trading was still PAUSED venue-wide when this staged (Aug 27
> night incident) — if it's still paused at pull time everything simply
> idles until the venue returns; nothing here needs babysitting.

The box has been frozen on `21316dd` since Aug 22 19:49 UTC. Step zero is
the pull + daemon restart (Rob runs it); everything below assumes the box
is then on current `main`, which already carries: the ha (hits) kill, the
K kill, the gridiron join-peg fix, executor `fail_tag`s, sweep gate
tallies, `/api/gridiron/sweep-now` (+`force=1`), `/api/polymarket/orders?all=1`,
and the manual-order endpoint. Ordered by money-at-risk:

1. ✅ **BUILT Aug 28 (`app.py:_scalp_tick`, rides the repeg lease,
   `SCALP_ENABLED=False` = shadow).** **BUILD THE SCALP SELL ARM** — `docs/scalp-sell-arm-spec.md`. The
   session's centerpiece. Shadow 2-3 days (`signal_blob.scalp_shadow`),
   then Rob's call to arm. **CONFIRMED THE DAY'S HEADLINE (user, Aug 25
   night, watching a wave of fills land): "Thursday is going to be a
   phase 2 day… this is no longer about betting, we are gonna be the
   bookie!" Every fill on a rent lane is the scalp arm's inventory —
   build this first, everything else waits behind it.**
1b. ✅ **SHIPPED Aug 28 (constants on main + topup taught gridiron
   targets + fbprop_config.contracts=4 in the DB).** **DOUBLE THE CONTRACTS — everything ×2, Master Rule → $13 (user,
   Aug 25 night: "we are doubling the contracts, it's time to make some
   fucking money"; scope + rule confirmed by explicit choice, then held
   for Thursday: "No… Thursday…"). Ship WITH the box pull so both sides
   change together.** The numbers, already arithmetic-checked:
   - `_AUTOBET_CONTRACTS` 10 → 20 (ML + outs/walks)
   - `_AUTOBET_CONTRACTS_NRFI` 5 → 10 (10 × 64¢ = $6.40)
   - `_GRIDIRON_CONTRACTS` 5 → 10, `_GRIDIRON_TOTAL_CONTRACTS` 2 → 4
   - `fbprop_config.contracts` 2 → 4 (DB edit, live next tick, no deploy)
   - `_REPEG_MAX_COST_USD` 6.50 → **13.00** — REQUIRED: at 20 contracts
     the $6.50 rule silently blocks every ML/outs/walks entry above
     32.5¢ (the exact Aug 16 failure class). User-approved with the
     arithmetic shown, per that precedent. `_WHIFF_CONTRACTS_K` stays 4
     (K is killed anyway).
   - ⚠ **FIX TOPUP FIRST**: `/api/polymarket/topup`'s target is
     `nrfi → NRFI const, else _AUTOBET_CONTRACTS` — a football
     spread/total pick would be topped to 20, not 10. Teach it
     `_GRIDIRON_CONTRACTS`/`_GRIDIRON_TOTAL_CONTRACTS` for
     gridiron_autobet picks BEFORE running it; then topup resizes the
     resting unfilled book to the new stakes (cancel → verify → create
     at the SAME price; filled positions stay untouched by design).
2. ✅ **WIRED Aug 28.** **Wire `_gridiron_bet_sweep` into `cellar/lanes.py:lane_opener`** — the
   sweep currently runs NOWHERE while the box is healthy (its only call
   site is the Vercel paperlog route behind `_own_opener`). One call after
   `_gridiron_opener_pass`, same lease, small budget. Then the sweep-now
   bridge endpoint demotes to a manual tool.
3. ✅ **BUILT Aug 28 (`app.py:_reconcile_tick`, 15-min cadence inside the
   repeg lane; both Aug-26 additions included; `reconcile_bak` DDL
   applied).** **THE RECONCILE PASS — user doctrine, Aug 25, final form:** monitor
   every resting machine order; when it leaves the book, exactly two
   cases (the user cancels nothing, so there is no third):
   - **Filled** (TRADE row / position on the slug) → never re-bet; the
     position rides (or scalps per the sell-arm spec).
   - **Gone without a fill = SHADOW-CANCELED by the venue** → clear the
     pick row and put the market **back through the full gauntlet: rule
     1 rent check at the current market, model re-verdict at the current
     book, re-bet if both still pass.** Fresh decision, never a blind
     re-place — Aug 25's re-bets correctly came back on different rungs
     than the dead orders.
   Context that forced this: Polymarket killed the resting football book
   three times in four days (ladder re-provisioning, program-unrelated —
   rent-check verified per-market programs unchanged), their API exposes
   NO cancel history (orders.list ages terminal states out entirely, so
   `all=1` shows nothing), and every dead order's pick row silently
   blocked the executor's dedup from re-betting. Runs per-tick on the
   box; MLB GTD expiries at first pitch are NORMAL, not kills. Until
   built, the 3am AZ routine + 9am daily check run this logic remotely.
   If the user ever does cancel a machine order by hand, deleting the
   pick on /handicapper is the "don't re-bet" signal (the venue can't
   tell us who canceled).
   **TWO ADDITIONS from the first 3am reconcile run (Aug 26, zero
   zombies found — the 67-pick vs 37-order gap was ALL fills):**
   (a) the pass must ALSO check **position size vs pick contracts** —
   lad-det-2026-08-28 filled 20 contracts on a 10-contract pick because
   an ORPHAN order (C3CWVXD88FSS, created with no pick row — likely a
   bet-then-persist-failed pass) rested invisible to the executor's
   dedup until a second order joined it. Both filled at 59¢, $8.13 on
   one event. Detect: our net position on a slug > the pick's stamped
   contracts. (b) **TAPE-QUERY GOTCHA: the venue labels a BUY_SHORT
   (NO-side buy) as `side=ORDER_SIDE_SELL`** — any poly_activities
   trade query must classify our side by `intent`
   (BUY_LONG/BUY_SHORT = we bought), never by `side`, or every NO/dog
   fill silently drops out (this bug briefly overstated a week's ROI
   by missing 44% of the bet denominator).
4. **fbprop graders** — the NFL props lane is armed and bets on listing;
   nothing grades `fbprop_autobet` picks yet (venue truth via poly_pnl
   covers money; the model scoreboard needs a grader off
   `football_player_games`).
5. **Stop the daily ha re-strip** — once the box runs
   `_WHIFF_FAM_KILLED={"k","ha"}`, remove step 0 from the freeze-week
   daily-check routine (or retire that routine for a normal-week check;
   it also carries the football venue-reconcile step, which should keep
   living somewhere).
6. **UFC schedule spine dark since ~Aug 11** — no new UFC `markets` rows;
   also the weekly `ufc_stats` scrape flaked Aug 24 (browser challenge).
   Diagnose the spine (ESPN mma path? Kalshi enrichment?) before the next
   Saturday card.
7. **Telegram queue dead on Vercel?** — zero AUTO-BET pings queued since
   Aug 22 despite ~40 real placements (including the whole Week-1 football
   wave). `_send_fill_telegram` appears to no-op on the Vercel side, which
   also masked create-exception evidence during the ghost-order hunt.
   Find out why; a silent alert channel is the outage-that-looks-healthy
   class.
8. **Open analyses queued:** K execution post-mortem on paper (backtest
   edge real, both live configs lost); the fill-time-vs-rent split (rent
   per resting hour vs P&L per fill, by side strength) that would settle
   strong-vs-weak-side quoting; ML watch — a second ~31% week reopens the
   model question (weekly baseline −$1.28 / −$2.73 / −$98.15 vs ~$197/wk
   rent).
9. **HOCKEY — "NHL can wait" (user, Aug 25 night, minutes after the
   line above): NOT a Thursday item. Parked here so the readiness read
   isn't lost. The rent rails are ready, the betting brain is not.**
   Ready with zero work: the rent gate is sport-agnostic/per-slug (NHL
   programs clear the day the venue lists them), pm-snapshot + Kalshi
   cross-confirm already watch NHL (96h window), repeg covers
   ML/spread/total. Missing, in build order: (a) the **confirmed-starter
   goalie feed** (Crease IQ P1b — the actual planned edge is the
   news-vs-line timing play; pages populate ~late Sept preseason);
   (b) an **NHL bet pass** wired rent→model→bet through
   `_autobet_execute` (nothing exists — MLB has _opener_pass, football
   the gridiron pass); (c) an NHL ML **chase fair** in
   `_fresh_fair_for_repeg` (the moneyline branch is Diamond IQ /
   MLB-only). HONESTY GATE: no Crease IQ number has cleared gate 1
   (team core dead 53.3%; xG core 54.4%/0.2467 still loses to the
   market) — if nothing clears by puck drop, the opening posture is
   rent-lane quoting only, NOT model picks. Don't re-run the
   goalie-identity shape (struck out three times); the levers are the
   starter-news feed and training depth (2022-24 shot backfill).
