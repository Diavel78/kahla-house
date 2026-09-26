# THE MIDDLE PAIR — spec

**Status (Sep 21 2026): LIVE at full throttle — the pair machine looks at
every football ladder first and the Ferrari bets what it declines.** Football
pairs are built from the executor's own line rule; MLB totals keep the
worth-table path.

**Sep 25 2026 — read `docs/review-2026-09-25.md` first.** The venue pulled
every EARLY program ~Sep 17; the seeder's lead floor now follows the program
(`_pair_min_lead_h`), sides are read off labels, one enabled row owns a slug,
the exit floor after a partner sale is FLAT, amends send the total, rent is
tri-state, off-touch cancels park the leg.

## THE GOAL IS FLAT, NOT HEDGED (Rob, Sep 21 2026)

**"Flat, money back plus rent collected, is the fucking goal. Hedged is almost
a guaranteed loss unless we middle. So ideally, we would collect rent and never,
ever have anything bet."** Read every rule below in that light:

- A fill is not a win. The seat exists to rest at the touch and earn.
- Half-filled does **both at once** — chase the partner AND list the held leg at
  cost. "Try to sell the one you own and keep trying to buy the one you don't."
- A completed pair over 100 pays 100 and only profits on the middle: a bounded
  LOSS, accepted as the price of renting two sides.
- **A pair under 100 is the day-one exception — "fuck it, just take the
  profit."** It rides to settlement with no asks.

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

## THE DOCTRINE (Rob, Sep 21 2026)

**"This is a fucking RENT MACHINE. At touch or it's POINTLESS. Hedge losses
with no rent is just losing slowly."** Every rule below serves that and is
read in its light:

- **Be AT the touch or come down.** A leg more than a tick under the touch
  earns nothing in the venue's per-second scoring, so it moves to a window it
  can hold the touch on, or its bid is cancelled. There is no third option.
- **NEVER take.** Crossing costs 5c·p(1−p), worst at a coin flip, which is
  exactly where pairs live. Every order is post-only.
- **Raise the loss cap instead.** A pair may cost up to 120 (machine_flags
  `pair_max_loss_c`, cents over 100) if that is what it takes to sit at the
  touch on both legs. The locked loss is the price of the rent, and it is
  cheaper than the same loss taken slowly with no rent at all.
- **On a completion, take the WIDEST window under the cap** — most numbers to
  middle on. The mirror is the last resort, not the target.

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
| one sold, one held | the sold leg re-bids, capped by the held leg's cost | the held leg floors **FLAT** (pair cost − sold price; Sep 25 2026 — `cap − sold` was written when the cap was a value estimate and became a 5-11¢ profit demand under the 120 loss budget), live through the game, rides if unsold |
| both filled ≤ 100 | — | **none. The lock rides.** |
| both filled > 100 | — | at cost until T−30, then all asks cancel and it holds for the middle |
| either leg loses rent, nothing held | **both bids cancel** | — |
| either leg loses rent, one leg held | the empty leg keeps bidding anyway | the held leg keeps its exit |
| T−30, nothing held | **both bids cancel** | — |
| T−30, one leg held | the other keeps bidding to kickoff | held leg's ask stays |
| kickoff | stop | a lone held leg's ask stays working the whole game, until sold or settled |

**RULE 5 — THE RE-RUNG LADDER (Rob, Sep 20 2026).** One leg filled, the other
stranded under the touch because the cap binds = no rent on either side and no
hedge. The partner walks IN, rung by rung, to the best rung it can hold the
TOUCH on inside the cap — **all the way to the mirror if that is what it takes:
"the goal is to get out, not have the bet."** Every rung in is still a hedge
(it only shrinks the window); the mirror has no middle at all and is a legal
stop; below the mirror is a GAP where both legs lose and is never offered. The
old bid is cancelled before the new leg is written — an order left behind is a
second seat on the same game. Live example: holding WAS −1.5 at 37 with SEA
+4.5 run away to 78 (pair 115 vs cap 103), +3.5 pairs at 111 and +2.5 at 107,
both past their own caps, so the ladder walks to the mirror at 99.

**RULE 5b — A BAD RUNG SITS AT ITS OWN TOUCH FOREVER (Sep 21 2026).** The
off-touch test only fires when the market walks away from a leg, so a pair
seated on the wrong rung in the first place is never re-picked — 59 of them
were resting, none filled, windows beside the number. An UNFILLED football pair
is now re-judged against the line rule every 30 min (`_PAIR_RELINE_S`) and
re-picked when the rule wants different rungs. Nothing is held, so this is just
rule 5's "both pending → re-rung is fine" with no hedge to protect.

**AND THE REBUY REMEMBERS.** When a completed pair sells out of both legs, the
rebuy starts from the LAST rung traded, never the seed rung — that rung was a
read on a line that has since moved.

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

Walks the venue's rent list — the enrolled rungs ARE the universe — and seats
the best pair per (game, market).

### FOOTBALL: FERRARI RULES, WITH A PAIR (Rob, Sep 21 2026)

**"Ferrari rules… with a pair… is the ENTIRE GOAL"** and **"bad rungs is the
entire issue… and why we can't find a middle."**

`_pair_from_gridiron_rule` builds a football pair out of the executor's own
`_gridiron_line_rule` — the same center (Pinnacle > DK > FanDuel, else the
venue moneyline converted to a spread, else the model capped ±7) and the same
`_gridiron_seat_legal` the Ferrari has placed by since Sep 5. Each leg must
also pay rent, not be culled, have a live bid, and peg where the executor
pegs (`_gridiron_join_touch`).

**Both sides of one rule ARE the middle.** The bound pushes each side away from
the center in opposite directions — dogs up, favorites down — so a legal away
seat and a legal home seat straddle the line by construction.

**Seeds rank by DISTANCE FROM THE LINE, then cost** (Rob, Sep 21 2026).
Widest-window is the COMPLETION rule — one leg held, its cost already spent, so
more numbers on the loss is strictly better. It is wrong for a fresh seed:
widest-under-cap puts the rungs as far apart as the tail gate allows, a deep
favorite against a longshot (CARK +17.5 at 81.5¢ against FSU −8.5 on a game
lined near −30). **The reason is line movement, not leg balance:** "reality is
we're going to end up fucking owning these things, and we're so fucking early
the line's going to be moving all over the place — so you want the distance to
cover the fact that we really have no idea what the lines are going to end up
being." Today's line is the best estimate of the closing line; sitting on it
protects the seat we are still holding after the number moves.

**Two price fences: 65¢ a leg, 120¢ the pair.** Not the Ferrari's single-seat
`_GRIDIRON_MAX_ENTRY_C` (60) — that threw out real middles a leg at a time.
And not "the combined cap is enough" either: the combined cap bounds the loss
ONLY IF BOTH LEGS FILL, and half-filled is the normal state because the
expensive leg is the one the market is leaving. The 65 is a SEEDING fence only
— once a leg is held, the partner chases to `120 − filled cost` and the hedge
is never blocked by it.

It replaced a worth-table-and-price-band seeder that ranked rungs with no idea
where the real number was. On the same board, same minute:

| | old seeder | the rule |
|---|---|---|
| ATL@GB spread (Pinnacle −6) | away +4.5 / home −3.5, wins on **4** | away +8.5 / home −3.5, wins on **4-8** |
| ATL@GB total (Pinnacle 44) | over 46.5 / under 48.5, wins on **47-48** | over 41.5 / under 46.5, wins on **42-46** |
| CARK@FSU spread (≈ −30) | away +17.5 / home −8.5 | no legal seat — refused |

The old windows sit *beside* the number; the rule's sit *on* it.

**MLB totals keep the standalone path** (`_pair_candidates` + the worth tables)
— the football line rule does not exist for them.

### The old brain (still live for MLB)

Prices rungs **off the quote table, never REST** (a standalone run of the same
logic tripped the venue's rate limiter on its first pass) and **picks by EDGE:
what the middle is worth minus what we pay.** Never by price. The first draft
ranked by price and chose a middle on 9 (worth 1.3%) over one at the line.

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

**THE CAP IS A LOSS BUDGET, NOT A VALUE ESTIMATE (Rob, Sep 21 2026: "raise the
cap to 120, take the most expensive rung under cap at touch… highest odds to
middle on the loss", and "never ever take — I'd rather raise the fucking loss
cap and get back to touch. I want fucking rent").** `_pair_ceiling` is
`100 + machine_flags pair_max_loss_c` (20) against a per-sport floor, so a pair
may lock up to 20¢ of loss per contract — $3 on a 15-lot — to sit at the touch
on both legs and to reach the mirror on a completion. Crossing the spread is
never the alternative: the taker fee peaks at exactly the prices pairs live at,
and every order is post-only.

MLB keeps the measured-worth cap:

```
cap = min(sport ceiling, 100 + the middle's measured worth + 1.5¢)
      and, only when the two mids are coherent (≥100), mid_sum + 1.5¢
```

Ceiling floors: football and basketball 110, MLB 116, NHL 119. Baseball and hockey
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


## Sep 25 2026 addenda (box review — see `docs/review-2026-09-25.md` and commits 5516d5f…68b2c1e)

Rules added or corrected on the box, all live and selftested (430 passed):

1. **One leg per side per game.** `_pair_side_held(positions, game_prefix, mt, side, exclude_slug)` reads what we hold on a side through ANY rung (venue sign: pos-/neg- spread slugs are the away line, net>0 away / net<0 home; totals net>0 over / net<0 under). A pair BID that would add a second lot to a held side is refused (`why: side_held:N`) and our resting bid there cancelled. Two legs on one side is not a pair (`not_pair_rows`).
2. **Shared slug → the row holding more legs owns it** (tie → oldest). A losing twin whose own leg is empty is skipped and its bid cancelled; one whose own leg is held keeps running for its ask.
3. **Adopt or decline, never stack.** The seeder declines a (game, market) with any held seat ≥5 (`held_seat`). The handover — held seat as leg one plus one opposite bid under the cap — is NOT built; clean case only when it is.
4. **Flat exit floor** after a partner sale: pair cost − sold price (`sold_cost` travels), then own cost, never `cap − sold`.
5. **Amends send the order TOTAL** (`leaves + cum`); the venue's `quantity` carries fills.
6. **Tri-state rent** (`_pair_rent`): unreadable keeps orders. **Off-touch cancels park 10 min.** A vanished bid re-reads positions before a new lot.
7. **Socket wakes the pair lane only for its own legs** (`app.PAIR_LIVE_SLUGS`). Laps stamp `t_lap` and `slow_rows`.
8. **Cap stays a flat 120 for now** (Rob): probability-based caps come only after re-rungs and completions are proven on a day-of window. Fair middle value = 100 + P(middle)¢ — the MLB seeder already prices that way; the football seeder and the completion path use the flat ceiling.
9. **Retired-but-held legs are released** to the autolog (`_pair_slugs`), and the autolog's own blocks on adopting them are gone (`_sell_slugs` MANUAL-only; index 018 per rung).

Open: the handover build; picks whose `entry_line` disagrees with their slug (OKL@GA, HOU@IND); the earnings sync riding a stuck paperlog lane.
