# FOOTBALL WEEKLY GAME SHEETS — build spec (scoped Aug 22 2026, with Rob)

> STATUS: SCOPED, NOT BUILT. This doc is the handoff for the build session.
> Read CLAUDE.md first — especially RULE 0.001, THE RENT RULE, the clock
> rule (Arizona, always), and "THE ORDER OF WORK" (this project IS item 2,
> FOOTBALL). The sheets are ANALYSIS ONLY — they never place bets; betting
> stays the Cellar's job.

## What Rob asked for (his words, condensed)

Weekly game sheet, **Monday night**, for **full NFL and full NCAAF**, built
on the model, PLUS a deep analysis Claude writes per game. **Friday update:
a quick list of changes only (injuries, line moves) since Monday.** Last
year this lived in Claude Projects chat; this year it integrates the model.
"Doesn't have to be quite this crazy" — referring to the reference example
below — but every game gets a sheet. **Odds from DK AND Polymarket now**
(last year was DK-only). Delivery: **Telegram ping confirmed**; putting the
sheets on the website is a "maybe" — propose it, let him decide.

## The reference format (last year's Alamo Bowl deep dive)

Sections, in order, from the example Rob pasted (full text in the chat
transcript; shape matters more than exact tables):
1. **Header** — matchup, date/time, venue.
2. **Line movement & betting splits** — Opening vs Current spread/total,
   per book; DK bets% vs handle% per market; reverse-line-movement callout.
3. **ESPN FPI / SOS / resume ranks** — small comparison table.
4. **Personnel report** — the heart of it: injuries/opt-outs per side with
   stats and impact, "who actually takes the field" (the USC-missing-10-
   starters / Ken-Seals-is-not-a-normal-backup analysis).
5. **ATS trends** — situational (e.g. "1-6 ATS as 3+ pt favorite").
6. **Common opponents / notable results.**
7. **Game script projection** — advantages per side, bulleted.
8. **MY ANALYSIS** — the narrative synthesis.
9. **RECOMMENDATION** — play / line / confidence table (primary play,
   sprinkles, leans).

## What upgrades this year (the "UP A NOTCH")

- **OUR NUMBER section (new):** Gridiron IQ per game — projected margin,
  **cover probability at the actual posted line** (`_lib/gridiron_spread.py`
  `cover_prob`, gate-1 passed all four NFL/NCAAF spread+total cells on the
  3-season fit), projected total via `total_fit`, win prob. The sheet says
  where the model and the market disagree, in points and in pp.
- **Splits upgrade:** `vsin_snapshots` carries **Circa (sharp) AND DK
  (public)** handle/bets/lines — RLM detection is computed (line moved
  against the heavy-handle side), not eyeballed.
- **Line movement from our own tape:** `pm_snapshots` (PMM + Kalshi, NFL
  watch window 168h, NCAAF 72h) — opening = first snapshot at listing.

## Data source map (all existing unless marked NEW)

| Sheet section | Source |
|---|---|
| Schedule spine | `markets` table (ESPN ingest is the football spine) |
| PMM/Kalshi lines, open→now | `pm_snapshots` per market_id/market_type/line |
| DK + Circa lines & splits | `vsin_snapshots` (+ `handicapper_web._vsin_movement` for the arriving-money read) — ⚠ VERIFY VSiN's NCAAF coverage breadth early; it may only carry the majors |
| Model | `power_ratings` latest snapshot (off/def + fitted hfa/scale + `spread_fit`/`total_fit`) + `_lib/gridiron_spread.py` — note Flask can't import kahla-scanner; mirror or compute cellar-side |
| Injuries | ESPN injuries API (already fetched in `handicapper_web`) |
| FPI / SOS / resume ranks | NEW — small ESPN FPI fetch (site.web.api.espn.com) |
| Common opponents / results | `game_results` (football 2023→present backfilled) |
| ATS trends | ⚠ GAP: needs historical CLOSING spreads, which we do not have for football. Accrues forward from Week 1 via our own pm_snapshots close. Year one: section is thin/omitted or researched live by the writing session — never fabricated |
| Deep analysis + recommendation | Claude, at generation time (that's the Routine's session) |

## Architecture (proposed, confirm in build session)

1. **Monday Routine** (`create_trigger`, fresh session per fire, ~6pm AZ
   Monday): session pulls every NFL + NCAAF game in the coming week from
   the spine, builds the data tables mechanically, writes the narrative
   per game, publishes the week's sheets, pings Telegram (existing Filled
   Bot sender — batched digest is fine, no urgent flag).
2. **Friday Routine**: re-pull injuries + lines, diff against Monday's
   stored sheet, publish ONLY the changes list ("line moved USC -5.5 →
   -3.5", "WR X ruled out"), ping Telegram.
3. **Storage**: sheets must persist (Friday diffs against Monday, and the
   season becomes a record). New Supabase table (e.g. `football_sheets`:
   week, market_id, sheet_md, model_blob, lines_blob, published_at) — DDL
   via `run_sql.sh`.
4. **Delivery**: Telegram ping = confirmed. Website page (`/football`,
   admin-gated, phone-first, week nav) = RECOMMENDED but Rob said "maybe"
   — build the data/writing pipeline first, propose the page with a mock
   before building it. Claude artifacts are the fallback surface.

## Scale reality (flag to Rob in the build session before writing code)

Full NCAAF is **60-100+ games/week**. Every game CAN get the mechanical
sheet (tables are free — the model prices everything). The DEEP narrative
at Alamo-Bowl depth for 100 games is not one session's context. Proposed
tiering, needs Rob's sign-off: full deep dive for ALL NFL + NCAAF games
that are (ranked matchup) OR (model-vs-line disagreement above a threshold)
OR (Rob-flagged); everything else gets the data sheet + a 2-3 sentence
model read. If Rob insists on full depth for everything, the Monday run
fans out subagents/multiple sessions — budget accordingly.

## Rules that bind this build

- **Analysis ≠ execution.** The sheet recommends; only the Cellar bets, and
  only where rent pays (NCAAF has NO rent programs — those sheets are for
  Rob's own reading/hand bets, and NFL model bets stay gated by the
  shadow-record earn-in, per the gridiron opener section of CLAUDE.md).
- **Arizona clock** for every "Monday"/"Friday"/week boundary. Cron
  expressions are UTC — convert.
- **Never guess column names** — schema-check every table before writing
  queries (this burned the last session repeatedly).
- **Venue truth / no invented numbers:** a sheet section whose source is
  unreachable renders "unavailable", never a guessed figure. Same law as
  the dashboard.
- **NFL preseason no-fly** date floors (`_GRIDIRON_MIN_START`) — the season
  starts NFL 2026-09-08, NCAAF Week 0 2026-08-29. First real sheet is the
  Monday before NCAAF Week 1 / NFL Week 1; there IS a Week-0 slate
  2026-08-29 — ask Rob if he wants a Week-0 sheet as the shakedown run.

## Open decisions for the build session

1. Website page: propose with a mock, Rob decides ("maybe" as of scoping).
2. Depth tiering for the NCAAF long tail (see Scale reality).
3. Week-0 shakedown sheet (Aug 29 slate) as the dry run?
4. Where generation runs: fresh CCR session per fire (proposed) vs cellar
   batch job. The narrative requires a Claude session — Routine wins.
