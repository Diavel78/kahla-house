# THE SCALP SELL ARM — Market Maker Phase 2, v1

**Status: SPEC (Aug 24 2026 night, user-designed in conversation). Build at
the Thursday Aug 27 box unfreeze. Nothing here is live.**

The user's conclusion after the Aug 24 lane review, verbatim intent: *"This
entire program is leading one direction and only one direction. It's a rent
machine. The only way that it actually can completely one hundred percent
become a rent machine is if we get to the point where every time an order
fills, we automatically put in a sell order to take profit."*

## Why the dead harvest's math doesn't apply

The Aug 19 harvest kill reasoned that selling early gives up the model's
edge. Three weeks of venue truth (weekly ML bets P&L: −$1.28 / −$2.73 /
−$98.15) says the rent-lane entries carry ~zero edge — so scalping the
spread on that inventory gives up nothing and is the only reliable profit
those positions produce, plus variance reduction. The harvest also rested
FIXED rungs (entry×1.35+) that losers never touched; this design rests AT
THE TOUCH and walks DOWN, so it actually trades.

## Policy (the user's, decided Aug 24)

1. **RENT LANES ONLY.** MLB moneyline (`aec-mlb-`) + football spreads/totals
   (`asc-nfl-`, `tsc-nfl-`, NCAAF if it ever pays). **Edge lanes NEVER
   sell**: NRFI (the proven jump market — 0/46 losers touched, resolves in
   minutes), walks, outs, and any future lane whose model prints on its own.
   Doctrine: **model lanes ride, rent lanes scalp.** Manual bets untouched
   (AUTOMATIC-flag gate, as every sweep).
2. **Trigger:** a rent-lane pick's buy fills (position detected on the tick).
3. **Initial ask = top of book** (join the best ask; never cross).
4. **Walk DOWN when unfilled**, on the repeg cadence, toward the floor.
   Walking UP is allowed when the book rises (stay at the touch); the floor
   only binds the bottom.
5. **Floor = entry + 1¢** ("that way it's always a baby profit if nothing
   else"). Maker fee at worst 1.75·p(1−p) ≈ 0.44¢/share at 50¢, so +1¢
   stays net-positive at every price on the grid. Never rest below floor.
6. **GTD through resolution — the ask stays working in-play** until it
   fills or the contract settles. The pick-six risk (live fair gaps past a
   resting ask before a tick can move it) is ACCEPTED by the user: unwinds
   (flags, replays) cut the other way, ~99% of fills will happen in the
   first minutes of play while price ≈ entry, and the floor makes the worst
   case a small profit, not a loss. Do not add a jump-guard in v1.

## Mechanics (all proven elsewhere in the machine — reuse, don't reinvent)

- Cancel → verify-no-fill → create fresh for every walk. `orders.modify`
  BANNED (REPLACED-husk landmine, twice probe-proven). `orders.list` is the
  only truthful read. Post-only (`participateDontInitiate`) on every ask.
- One sell per slug — the dedup invariant. `dedup-orders` stays BUY-only
  (its sell exclusion note changes: there is no ladder anymore, but the
  scalp still wants exactly one working ask; extend dedup to sells ONLY
  after the harvest-ladder exclusion comment is retired with it).
- Serial writes, bounded walks per tick (start: 1 sell walk/tick beside the
  buy repeg's budget), wall-clock guard before each cancel.
- The first-pitch cancel sweep is BUYS-only and must stay that way — the
  scalp's whole point is asks that outlive kickoff.
- Sells price YES-canonical on synthetic/short positions (1 − c), same as
  the buy side.
- Flag: `SCALP_ENABLED` (new engine, own constant — `_HARVEST_ENABLED`
  stays dead; git history keeps the ladder).

## Rollout

Shadow 2–3 days (log the ask the scalp WOULD rest each tick + whether the
tape crossed it → `signal_blob.scalp_shadow`), then arm. The user may
shorten this on review — his call.

## What it must measure (the scoreboard that decides if it stays)

- **Round-trip completion rate** — the standing open question from the
  Aug 19 memo: fraction of buys exited at floor-or-better vs ridden to
  resolution.
- Realized scalp $/week per lane (venue truth via poly_pnl/activities).
- **Rent effect, re-measured** — the Aug 19 finding said a resting ask
  earned NO rent ($0.054/mkt-day with our ask working vs $0.075 with none).
  The business case is the spread capture; treat sell-side rent as upside
  to verify, never assume.
- Inventory hold time before/after (capital velocity is the hidden win:
  a scalped position frees its stake the same day).
