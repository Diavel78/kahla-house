# THE MIDDLE PAIR — spec

**Status (Sep 20 2026): ONE test pair live, the seeder BUILT AND OFF.**
Rob's rule while the first pair is on trial: build it, don't bet it.

## Why it exists

Every day looks the same: the bets lose, the rent wins. Sep 1-18: **bets
−$169.21, rent +$2,315.85**. The bets bleed for a structural reason, not a
model reason — the sell arm exits whatever rises, so what we're left holding
into the whistle is always the side that fell. 438 positions held to the end,
only 106 won, −$489.52.

A pair removes the bet from the equation and keeps the rent. Two legs on
opposite sides of two neighbouring rungs cover every outcome, so one always
wins. The pair costs a little over 100¢ (or under, when we're early), pays back
100¢, and pays 200¢ when the game lands in the gap between the rungs. Both legs
rest at the touch on their own market, so **both earn rent** — the product.

The predecessor was the Lambo (Polymarket ↔ Gemini). It died Sep 18: Gemini
paid no rent, and keeping two venues in sync in two processes never worked.
This lives on one venue, in one engine, in one process.

## The shape

`line` is always the AWAY line (spread) or the total.

| | Leg A | Leg B | Wins both when |
|---|---|---|---|
| spread | away at `la` (BUY_LONG on that rung) | home at `lb` (BUY_SHORT — the NO of the mirrored rung) | `−la < margin < lb` |
| total | over `la` (BUY_LONG) | under `lb` (BUY_SHORT) | `la < total < lb` |

Example: GB −4.5 with NYJ +5.5 wins both when GB wins by exactly 5.

**A window whose only covered number is 0 is not a middle** — away +0.5 with
home +0.5 is simply both sides of one line, and only a tie pays twice. Excluded
by rule; it was 7 of the finder's first 28 picks.

## The engine (`app._pair_tick`, lane `pair`, every 20s)

One engine owns both legs. Legs are NOT `bot_picks` rows, so every pick-driven
engine (repeg chase, scalp lap, snipers, fast ask, reconcile, recenter, rent
cull, seat top-up) never sees them. The two engines that act on pick-LESS
orders are excluded explicitly by slug (`_pair_slugs`): the autolog's adoption
set and the scalp's orphan sweep. The football executor refuses a (game,
market) a pair owns and reports `pair_owned`.

⚠ **Any new engine that acts on orders or positions without a pick must skip
`_pair_slugs` too.**

⚠ **ONE OWNER PER MARKET, AND IT IS CHECKED (Sep 20 2026 — the first armed
seeder run).** The seeder used to only *say* it skipped ladders the Ferrari
owns. It didn't check, and 5 of its first 6 pairs landed on slugs already
carrying picks, positions or resting orders; the wrong-side guard froze them
(it worked) and two pair bids rested 6 minutes on a Ferrari ladder before being
cancelled. No money moved. Now a slug is TAKEN when a pending pick names it,
the venue holds a position on it, or an AUTOMATIC order rests on it — taken on
either leg disqualifies the pair, a pending pick on the same (game, market)
disqualifies it at any rung, and an unreadable venue fails CLOSED. The engine
independently refuses to manage a leg a pending pick owns.

### State machine

| State | Bids | Asks |
|---|---|---|
| both empty | both chase the touch, together ≤ cap | — |
| one filled | the empty leg chases up to `cap − filled cost` | the filled leg lists at its own cost |
| one sold, one held | the sold leg re-bids, capped by the held leg's cost | the held leg floors at **`cap − sold price`**, live through the game, rides if unsold |
| both filled ≤ 100 | — | **none. The lock rides.** |
| both filled > 100 | — | at cost until T−30, then all asks cancel and it holds for the middle |
| either leg loses rent, nothing held | **both bids cancel** | — |
| either leg loses rent, one leg held | the empty leg keeps bidding anyway | the held leg keeps its exit |
| T−30, nothing held | **both bids cancel** | — |
| T−30, one leg held | the other keeps bidding to kickoff | held leg's ask stays |
| kickoff | stop | a lone held leg's ask stays working the whole game, until sold or settled |

**RULE 4 — rent is a reason to START, never a reason to go naked (Rob, Sep 20
2026).** Rent is re-asked every tick and a program can be pulled mid-life. If
EITHER leg stops paying while NOTHING is held, the whole pair comes down — two
orders resting for a middle we were only renting have no reason to be there.
Once a leg is HELD, we keep working to complete the pair (and to sell the held
leg) even with no rent on either side: **a hedge half-built is a naked bet, and
we don't accept a naked bet as the price of losing a rent program.**

**The T−30 split (Rob, Sep 20 2026).** With NOTHING held, both bids come down:
a leg filling at T−20 with no partner is a fresh naked bet 20 minutes before
kickoff, which is the thing this machine exists to avoid. With ONE leg held the
bids stay up to kickoff, because then a fill COMPLETES the pair — it cannot
create a naked leg, and a completed pair under the cap is the outcome we want.

Other invariants: one order per (leg, side); prices snap to the market's own
tick grid; a bid never crosses the ask (post-only); each leg's rent is checked
every tick and an unpaid leg doesn't bid; the $13 Master Rule and the book-wide
exposure cap both apply; and if the venue ever reports us holding the **wrong
side** of a leg, the pair freezes and pages Rob (the Lambo's first-night bug).

⚠ **An unreadable book leaves resting orders ALONE.** A transient empty read
cancelled a bid at 07:41 Sep 19 and re-placed it a minute later at the back of
the queue. Fix queued.

## Who gets the ladder (Rob, Sep 20 2026)

**The pair machine looks first; the Ferrari bets what it refuses.** Not a
stand-down — "the Ferrari doesn't stop seating football, it just doesn't get
first pick." With `machine_flags pair_priority` on, `_gridiron_try_bet` defers a
football spread/total ladder (verdict `pair_first_look`, 10-min retry) until the
seeder has written a `pair_declined` row for it — owned, no_middle, leg_taken or
rent. A ladder the seeder WANTS is never declined, so the Ferrari can't inherit
it out from under a pending seat. An unreadable `pair_declined` table answers
"declined" on purpose: a missed pair costs one pair, a frozen football lane
costs the board.

**The end state:** once pairs prove out, the pair machine *becomes* the Ferrari
for football — hedges only, and a game that can't be paired isn't bet.

## The seeder (`app._pair_seed_tick`, built, default-dry, flag `pair_seed_enabled`)

Walks the venue's rent list — the enrolled rungs ARE the universe — prices them
**off the quote table, never REST** (a standalone run of the same logic tripped
the venue's rate limiter on its first pass), and seats the best pair per (game,
market).

**It picks by EDGE: what the middle is worth minus what we pay.** Never by
price. The first draft ranked by price and chose a middle on 9 (worth 1.3%)
over one at the line.

### What a middle is worth (measured, `scripts/middle_stats.py`)

3,045 NFL finals since 2015 with real closing lines; 3,071 college finals.

| Middle on | NFL worth | Venue charges | Edge |
|---|---|---|---|
| 3 | 17.2% | 5.6¢ | +12 |
| 6 | 7.3% | **1.9¢** | **+6** |
| 7 | 8.1% | 5.3¢ | +3 |
| 2 | 5.3% | 2.2¢ | +3 |
| 9 | 1.3% | 1.0¢ | +1 |

**Six is the steal** (Rob called it before the query ran): the venue prices it
like an 8 while it hits like a 7. A three-number window sitting on the line
hits **26%** when the line is near 3.

**College is the same game with a wider spread.** Same key numbers (3 = 9.4%,
7 = 7.6%) and the multiples of 7 keep paying out to 28, so a 20.5/21.5 middle
(21 = 3.5%) beats an NFL middle on 9. Half of a college Saturday prices over
17.5, so big lines are normal there and the NFL's "skip 9 and up" does not
apply. **The one genuine difference: college 6 is 2.7%, not the NFL's 6.9%** —
overtime rules and how games end, not the scoring grid.

### The cap

```
cap = min(sport ceiling, 100 + the middle's measured worth + 1.5¢)
      and, only when the two mids are coherent (≥100), mid_sum + 1.5¢
```

Ceilings: football and basketball 110, MLB 116, NHL 119. Baseball and hockey
need more room because the gap is a whole run or goal (an NFL-style 1-point
middle doesn't exist there): a fair MLB moneyline + runline pair prices at
113-114, and NHL worse.

⚠ **A pair under 100 is a LOCK, not a stale quote** (Rob, Sep 20: "we are
EARLY, absolutely might have pairs under 100"). Early books are wide and barely
quoted — that is the seat we want. My first cut refused them AND let their
incoherent mids set the cap, which would have stranded the second leg with
nowhere to chase.

### Fences

Both legs must pay rent to SEAT a pair (RULE #1, per market; rule 4 governs what happens when rent is pulled later), each leg 20-80¢,
window 1-3 numbers, worth ≥ 2%, edge ≥ 1¢, at most 3 new pairs a tick, and
never past `pair_max_active` (25) live pairs.

## Sizes, and what a pair risks

15 contracts per leg, spread **and** total on a game. About $15-16 tied up per
pair. If the middle misses, a pair loses `cost − 100` — **$0.15 to $1.20 on
this week's board** — and the rent on two resting legs runs the whole time.

## Where things live

- engine + seeder: `app.py` (`_pair_tick`, `_pair_plan`, `_pair_candidates`,
  `_pair_seed_tick`), lane in `cellar/lanes.py`, cadence in `cellar/config.py`
- table: `pair_hedges` (`kahla-scanner/supabase/pair_hedges.sql`) — `legs` is
  the definition, `state` the engine's memory (its own fill prices, never the
  venue's blended average)
- shopping list, read-only: `kahla-scanner/scripts/pair_finder.py`
- the worth numbers: `kahla-scanner/scripts/middle_stats.py`
- kill switches: `machine_flags` `pair_enabled`, `pair_seed_enabled`,
  `pair_max_active`; or a row's `enabled`

## Going live

1. The test pair runs a full cycle through kickoff with no hand fixes.
2. Ship the unreadable-book fix; restart the money daemon.
3. Wire `_pair_seed_tick` into the `pair` lane, still dry, and read a day of
   its shopping list.
4. Flip `pair_seed_enabled`, 25 pairs, and watch the first fills.
