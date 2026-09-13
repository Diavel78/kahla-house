---
name: slate
description: |
  THE SLATE CARD — a day's football picks, vetted against real lineups.
  TRIGGER on any request for a day's or a weekend's picks: "give me the
  picks for Saturday", "NFL tonight what's the bets", "run the games",
  "what are we betting today", or explicit "/slate".
  NOT for a single game — that's /handicap.
  Output is a phone-readable card where every pick has already been
  checked against who is actually playing.
---

# The Slate Card

## ⚠ RULE 0 — THE MODEL IS A RENT COLLECTOR, NOT A HANDICAPPER

**Rob, Sept 13 2026, after a Week-1 disaster: "The model isn't really for
betting. It is for collecting rent."**

Gridiron IQ is opponent-adjusted points-for/points-against from past final
scores, recency-decayed, with a fitted HFA, run through a shrinkage fit.
**There is no player table in it. Anywhere.** It does not know quarterbacks
exist. It knows "Arizona" as a bundle of points.

That is fine for its actual job — picking which side of a market to REST an
order on so the maker rebate accrues. The measured history says so plainly:
the ML lane ran −$24 on bets and +$30 on rent. *The bets lose; the rent
carries it.*

**So `bet_spread.verdict` is NOT a pick. It is a rent-parking direction.**
Never put it in front of Rob as a pick without doing §2 first. Doing that
is what produced, in one Week 1:

- **Arizona +10.5** — betting on Kyler Murray's Arizona. Released in March.
- **Miami +3** — betting on Tua's Miami. He was in Atlanta, injured.
- **Atlanta +6.5** — betting on Penix's Atlanta. Cooper Rush started.

All three were live, real-money, placed weeks early. The model was working
exactly as designed. The failure was presenting its output as analysis.

## ⏰ RULE 1 — THE CLOCK

Arizona (`America/Phoenix`, no DST) is the only time zone. **Query it
first, every single time:**

```bash
./scripts/run_sql.sh "select to_char(now() at time zone 'America/Phoenix','Dy YYYY-MM-DD HH24:MI') as az_now;"
```

The harness date is UTC and rolls at 5pm AZ. Reusing a clock reading from
earlier in the session is how you tell someone a game kicks tomorrow when
it kicked twenty minutes ago. Re-query on every slate request.

## The workflow

### 1. Refresh the lines

Lines older than a few hours are a different card. From the CCR sandbox
ESPN is blocked at the egress proxy, so the refresh runs on Actions:

```
mcp__github__actions_run_trigger: football-sheets-data.yml, ref main,
  inputs {mode: friday}
```
Then wait for fresh `data_blob->'friday'->>'built_at'` on the week's rows.
`week_key` = that week's Monday (`week_key_default()` — don't hand-derive).

### 2. RESEARCH THE LINEUPS — this is the job, not a garnish

For every game carrying a candidate pick, establish:

- **Both starting QBs, confirmed.** Not "the presumptive starter" — who is
  taking the first snap today.
- **Any starter OUT** that moves a number: RB1 / WR1 / TE1, a premium
  defender, two or more OL.
- **Offseason departures the ratings still carry.** A team whose rating was
  built on a QB, a DPOY edge rusher, or a WR1 who is now elsewhere is
  mis-rated by the model in a direction you can name.
- **New HC/OC in game one** — scheme the ratings have never seen.
- **Whether inactives have posted** (~90 min pre-kick). If they haven't,
  say so on the card.

Fan this out with subagents (3-4 games each, WebSearch). Give each one the
model number and the market line so it can tell you *which direction* the
news cuts. Tell them plainly to write "unconfirmed" rather than hedge.

**The sheet's own Monday narrative already contains most of this** — it is
written by a session with WebSearch and it is usually right. Read
`sheet_md` before searching; it caught the Falcons QB mess four days early
and said "confirm the Week 1 starter before you bet this." Read
`friday_md` too — it carries the week's injury deltas.

### 3. Merge — model + lineups → the pick

For each game, the model gives a direction and the research gives a
verdict on whether that direction is still real:

| Research says | What to do |
|---|---|
| Rating rests on a departed/out starter | **Override.** Take the other side or pass. Say why. |
| Big roster hit the model can't see | Keep the side, size it down, name the hit. |
| Nothing material | The model's direction stands. |
| Model disagrees with market by a lot AND nothing explains it | Suspect the model. Vegas rarely misses by double digits. |

**Never print an override silently.** Mark it and give the one-line reason
— Rob needs to see the reasoning to trust or reject it.

### 4. The card format — PHONE FIRST

Rob reads these on a phone, usually minutes before kickoff.

- **NO wide tables.** A 3-column table scrolls sideways on a phone and he
  has told us so. Group by kickoff time as a heading, then one short line
  per game: `Team @ Team — **Side**, Total`.
- **State the leg count up front**, and say whether it's games or bets.
  "43 bets" when it was 43 games containing 65 legs is a real error — he
  writes the tickets.
- **Bold the side.** Prices only if he asks; he mostly wants the side.
- **Overrides and caveats go inline with the pick**, not in a footer he has
  to scroll back to.
- Unrated / no-number games: one line at the end saying how many, not a list.
- When he asks for a copyable block for iMessage: plain text, no pipes, no
  asterisks, short lines, grouped by time.

### 5. Required to pick

When Rob says "you're required to pick" he means every game gets a side
AND a total, no passes. Give the forced lean and label it. He does not
want "no bet" as an answer to a direct request — he wants the best
available read with its weakness stated. (Standing exception: never
manufacture a number for a game where a team is genuinely unrated —
say so and handicap the market line on game shape instead.)

No heavy chalk as a "side" — he has called −400 favorites "pussy bets."
Spread or a reasonable ML; if the ML is chalkier than about −250, give
the spread.

## Grading a past slate

Pull finals with the Actions ingest (`power-ratings.yml`, `days: 2`,
the right `sport`), then grade from `game_results`.

⚠ **DERIVE THE COVER TEST FROM FIRST PRINCIPLES. EVERY TIME.**
With `marg = home_score − away_score` and a side's own line `L`:

```
away covers  ⟺  away + L > home  ⟺  marg < L
home covers  ⟺  home + L > away  ⟺  marg > −L
```

That is the whole rule and it works for both signs of `L`. A shipped
grader once added a special case for road favorites (`marg < −L` when
`L < 0`) and silently turned two losses into wins — Rob caught it by
counting the rows himself. **No branch on the sign of the line.**

Report legs, not games. Split spreads vs totals. Leans are graded
separately and never folded into the play record.

## Standing facts about this system

- **NCAAF ingest is FBS-scoped** (ESPN `groups=80`), so an FCS team only
  enters `game_results` when it plays an FBS opponent. A rating built on
  ≤9 such games is excluded by `_MIN_TEAM_GP`. Say "FCS, N games on file"
  — never "our number doesn't rate them," which reads as ignorance.
- **The Friday model block is identical to Monday's.** The refresh
  re-anchors the ladder to the new line; the projection itself does not
  move. So a Friday "verdict flip" on an injury-driven line move is an
  artifact, not new information. Check the cause of every move.
- **Football totals** (`total_fit`) passed gate 1 but never gate 2 — it has
  never been tested against the market. Treat it as the least-proven
  object in the stack.
- Exchange lines in older blobs go stale and produce impossible numbers
  (a 16.5 or 20.5 NFL total). If a number fails the smell test, it is a
  data artifact — say so and pass rather than "finding" a huge edge.
