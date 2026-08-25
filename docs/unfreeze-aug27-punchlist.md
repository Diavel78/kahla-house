# Thursday Aug 27 box-unfreeze punch list

The box has been frozen on `21316dd` since Aug 22 19:49 UTC. Step zero is
the pull + daemon restart (Rob runs it); everything below assumes the box
is then on current `main`, which already carries: the ha (hits) kill, the
K kill, the gridiron join-peg fix, executor `fail_tag`s, sweep gate
tallies, `/api/gridiron/sweep-now` (+`force=1`), `/api/polymarket/orders?all=1`,
and the manual-order endpoint. Ordered by money-at-risk:

1. **BUILD THE SCALP SELL ARM** — `docs/scalp-sell-arm-spec.md`. The
   session's centerpiece. Shadow 2-3 days (`signal_blob.scalp_shadow`),
   then Rob's call to arm.
2. **Wire `_gridiron_bet_sweep` into `cellar/lanes.py:lane_opener`** — the
   sweep currently runs NOWHERE while the box is healthy (its only call
   site is the Vercel paperlog route behind `_own_opener`). One call after
   `_gridiron_opener_pass`, same lease, small budget. Then the sweep-now
   bridge endpoint demotes to a manual tool.
3. **Football verify-after-create + venue reconcile** — the ghost-order
   class (venue cancels resting orders on ladder re-provision; the pick
   row then blocks re-betting forever). Make football self-healing like
   MLB: a periodic pass that clears order-less unfilled football picks
   (venue-verified: no open order, no position, no TRADE activity) so the
   sweep re-places at current books. The manual procedure it replaces is
   in CLAUDE.md's football bullet and the daily-check routine prompt.
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
