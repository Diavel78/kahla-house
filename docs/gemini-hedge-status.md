# Gemini hedge machine — live status (handoff, Sep 15 2026 ~20:30 AZ)

**The gate (Rob):** ONE test pair runs an ENTIRE DAY with every fill/sell/repeg/re-rung
tracked, both venues in sync, "without me fixing anything." Then a second test the same way.
Only THEN auto-enroll every Poly football seat with a Gemini twin ("the Lambo leaves the
garage"). Do not propose scaling before a clean day. Rob funds Gemini (~$2K) only after that.

## The one pair — GB @ NYJ, Sun Sep 20 10:00 AZ, spread
| Leg | State at handoff |
|---|---|
| Polymarket | NYJ +5.5 (`asc-nfl-gb-nyj-2026-09-20-neg-5pt5`, BUY_SHORT), 20 resting at **56** = the touch (amended up from 55.5 when the cap went to 101) |
| Gemini | GB −5.5 (`GEMI-NFL-2609201700-GB-NYJ-S-GB5` YES), 20 **held** at 0.45, sell at 0.45 resting (770 ahead in queue) |
| Pair | not yet paired (Poly unfilled); if Poly fills → 101 locked (−$0.20/20), both asks stay until T-60 Sun 9:00 AZ |
| hedge_pairs row | game_prefix `asc-nfl-gb-nyj-2026-09-20`, gem_side away, gem_rv 5.5, gem_cost 0.45, pair_cap 1.01, paired 0, middle false |

Earlier today: Poly's first NYJ +5.5 leg (bought 55.5 at 05:25) SOLD at 56 at 12:38 (+$0.22),
the Ferrari re-seated NYJ +4.5 (the backwards shape vs held GB −5.5), pulled twice by hand
under Rob's order, then re-seated legally under the new rules at 20:10.

## Rules shipped today (all on main, both daemons rebooted on them)
- **Re-rung law:** both legs pending → any re-rung is fine. One leg HELD → the other venue's
  seat may sit only at the MIRROR rung or a MIDDLE (dog up / favorite down), never a 'side'.
  `app._hedge_rung_ok` inside `_gridiron_seat_legal`; Lambo `_rerung_ok`.
- **Pair cap 101 across the board** (`max_pair_cost` 1.01 in `~/.kahla/gemini_hedges.json` →
  `hedge_pairs.pair_cap` → `_hedge_cap_c`/`_gridiron_cap_for` at executor, chase, buy sniper,
  top-up). The executor CLAMPS the bid to the cap (rests behind the touch), never refuses.
- **Held split-rung pair (middle) rides:** `middle=true` → `_hedged_ask_off` True at any hour;
  Lambo rests no ask. "The middle pays 3x bet, rent isn't even close."
- **Lambo follows the Ferrari:** held Gemini leg → follows a LEGAL Poly re-rung on the same
  game (refuses a backwards one aloud); Gemini flat → follows the Poly seat to ANY rung's
  Gemini twin via `venue_contract_map` (`map_lookup`), pulls its orders on the contract it
  leaves, persists the working slug/contract in `~/.kahla/gemini_hedge_state.json`.
- Also merged: the other session's recenter rule (moves only in the seat's favor; value-side
  flip stays). Their successor/rekey code was replaced (no legality check).

## Known gaps (accepted for the test day)
- If Gemini sells first, Poly's +5.5 seat STAYS (legal, at touch); no "from scratch" re-seat
  (would be dog-down, refused by the rung-jump rule). Rob: leave it.
- If a held leg clears and the Poly seat is ILLEGAL, the recenter moves it within 15 min to
  the nearest legal paying rung (always in the seat's favor); tail-pull / side-flip / filled
  seats stay.
- Gemini rent at 20 contracts ≈ $0 (per-event size share vs 250-lot bots on all 22 rungs).
  "ANY rent is a bonus, the REAL prize is zero risk on the bets that fill."

## Open items
- Gemini DET–TOR ML, 10 YES @0.53, filled 3:46pm Sep 15 from an app-style order id — Rob has
  not confirmed it is his. MLB pays no rent on Gemini.
- Poly rent for Sep 14 showed only $0.99 pending (vs $116 Sep 13) at 17:23 — likely posting
  lag; re-check Sep 16.
- Leftover: Gemini MIN–CHI total UNDER 19 @0.43 with exit ask @0.44 resting.

## Where to look
- Lambo log `~/.kahla/logs/gemini-hedge.log`; ledger `~/.kahla/gemini_hedge_ledger.jsonl`;
  restart `launchctl kickstart -k gui/$(id -u)/com.kahlahouse.gemini-hedge`.
- Money daemon restart: `kill -TERM $(pgrep -f "MacOS/Python -m cellar")` (drains ~3 min).
- Gemini payouts: `.venv/bin/python kahla-scanner/scripts/gemini_probe.py status` (pool day
  ends 5:30pm ET; $1 min payout).
