# FOOTBALL WEEKLY GAME SHEETS — generation runbook

> This is the procedure the **Monday** and **Friday** Routine sessions
> execute. You are a fresh session in the kahla-house repo, fired by a
> Routine. The mechanical data was assembled ~90 minutes before you fired
> by `.github/workflows/football-sheets-data.yml` (GitHub Actions — the
> only compute that reaches both ESPN and Supabase; YOUR sandbox is
> ESPN-blocked). Your job: verify the data landed, write the analysis,
> render + publish the PDFs, ping Telegram. The sheets are **analysis
> only** — friends read them and bet by hand; the Cellar does its own
> betting and nothing here touches it.
>
> Rules that bind you: **Arizona clock** for every date word. **Never
> invent a number** — a section with no data says "unavailable". Never
> guess column names — schemas are in `kahla-scanner/supabase/
> football_sheets.sql`. Reads/writes go through raw Supabase REST (the
> sandbox can't import supabase-py — cffi bug) — the helpers in
> `kahla-scanner/scripts/football_sheet_data.py` (`sb_select`, `sb_patch`)
> already handle this, including stripping the angle-bracket wrapper the
> env's `SUPABASE_URL` carries and paging past PostgREST's 1,000-row cap.
>
> **THIS IS THE ONE FOOTBALL-CARD SYSTEM (Sep 18 2026 — Rob: "I want 1
> damn thing that runs sheets... I don't need 3").** A separate `/slate`
> skill + three Routines (Thursday CFB+NFL, Friday NCAAF full-board,
> Saturday NFL full-board) had grown up alongside this pipeline, producing
> phone-first text cards on overlapping schedules — redundant with the
> PDFs, and unreliable (Thursday's run showed `ROUTINE_RUN_STATUS_ABANDONED`,
> the Friday one had no recorded successful run at all). All three deleted.
> **The Friday schedule moved to the MORNING the same day**: the data
> assembly is now Fri 5am AZ (was 2pm) and the Friday narrative Routine
> fires 6am AZ (was 3:30pm) — so both the full pack and the picks-only
> sheet (§3) are published and pinged before Rob is up, for that day's and
> the next day's games. If you're asked to add a third schedule variant or
> a separate lightweight card, push back — that's the thing that just got
> deleted for being one system too many.
>
> ⚠ **KNOWN GAP, not yet fixed: the sheet's model does NOT carry the QB
> adjustment or the CFBD-ratings blend that `app.py`'s live betting engine
> (`_gridiron_proj`) now has.** `football_sheet_data.py` builds its own
> standalone ratings from `_lib/power_ratings.py` + `_lib/gridiron_spread.py`
> off `game_results` only — it has no player table and doesn't touch
> `_lib/football_qb.py` or `app._cfbd_consensus`. That means a team that
> lost its starting QB, or a team whose results-only rating is still noisy
> this early in the season (CFBD's SP+/FPI blend exists specifically to
> fix that), is mis-rated on these sheets the same way the live engine was
> before those fixes shipped. The "Who's new/who's out" narrative section
> is the only thing currently catching this — it is NOT optional, and a
> `bet_spread`/`bet_total` verdict must never be quoted as a pick without
> it (see `.claude/skills/slate.md` Rule 0 for the disaster case: Week 1
> picks on Kyler Murray's released Arizona, injured Tua's Miami, and
> Cooper-Rush-started Atlanta — all model output presented as analysis
> with no lineup check). Porting the QB/CFBD layers into this pipeline is
> the real fix; until then, treat the model number as a rent-parking
> direction, not a finished handicap, exactly as slate.md said.

## 0. Setup (both days)

```bash
cd kahla-scanner
pip install -q playwright markdown   # PDF + narrative rendering; Chromium
                                     # is preinstalled at /opt/pw-browsers
```

Week key = this week's Monday, AZ (`scripts/football_sheet_data.py:
week_key_default()` computes it — don't hand-derive).

## 1. Verify the data run landed

```bash
./scripts/run_sql.sh "select sport, count(*) games, count(*) filter (where tier='deep') deep,
    max(data_built_at) built
  from football_sheets where week_key='<WEEK>' group by sport;"
```

- `data_built_at` should be within ~3 hours. If the rows are missing or
  stale, trigger the workflow yourself via the GitHub MCP
  (`actions_run_trigger`, workflow `football-sheets-data.yml`, ref `main`,
  inputs `{mode: monday|friday}`), wait for the run to finish
  (`actions_get`), and re-check. **Routine-fired sessions may not carry
  the GitHub MCP tools** — in that case run the assembly locally:
  `python -m scripts.football_sheet_data --mode <monday|friday> --commit`.
  Everything except the ESPN sections (injuries/FPI/ranks, full-FBS
  slate) builds fine from the sandbox; the script falls back to the
  markets spine for the game list and marks ESPN sections unavailable.
  If ESPN was dark on the runner the blobs carry `unavailable` markers —
  proceed; the sheets print "unavailable" for those sections, never a
  guess.
- Off-season / empty slate: report "no games" to Telegram is NOT needed —
  just end quietly.

## 2A. MONDAY — write the narratives

For every row, read `data_blob` and write `sheet_md` (markdown, no top-level
H1 — the renderer owns the page header). Fetch rows with:

```python
from scripts.football_sheet_data import sb_select, sb_patch
rows = sb_select("football_sheets", {"select": "id,event_name,tier,data_blob",
    "week_key": "eq.<WEEK>", "sport": "eq.NFL"})  # then NCAAF
sb_patch("football_sheets", {"id": "eq.<ID>"}, {"sheet_md": md})
```

**VOICE — read this before writing a word (Rob, Aug 22 2026).** You are a
**sports handicapper**, not a quant. Football betting is **spreads and
totals** — never lead with a win percentage ("91% to win" on a −7.5
favorite is saying nothing). The model runs in the engine room; the reader
never sees its machinery. BANNED from narrative text: "calibrated",
"shrinkage", "raw projection", "logistic", "opponent-adjusted", "fit",
"pp", any model internals. The model is "our number", and a play is
stated the way a capper states it: **"TCU −7.5 is the bet at −135 or
better; −6.5 works to −152"** — the blob's `model.bet_spread` /
`model.bet_total` blocks carry exactly these numbers (side, line, fair
price, ±1pt ladder, play/lean/pass verdict). Use them verbatim; don't
re-derive.

**Depth by tier** (the assembly already decided `tier` + `tier_reasons`):

- **`deep`** — the full read, modeled on the Alamo Bowl reference format:
  1. **The bet** — spread first, then the total, in price-or-better
     terms from `bet_spread`/`bet_total`. Where our number and the
     market disagree, say it in points ("we make it TCU −12, the market
     says −7.5 — that gap is the whole card"). Pass honestly when
     there's no edge.
  2. **Who's new / who's out (THE HEART OF THE SHEET)** — college is a
     new team every year: **new starting QB, transfers in and out, new
     head coach / coordinators, key returners** — and, in-season,
     injuries and suspensions (blob `injuries`). The blob does NOT carry
     roster news: this comes from your own knowledge and **WebSearch
     when the tool is available** (search "<team> 2026 starting QB
     transfers coaching changes"). Roster FACTS are the one place you
     write beyond the blob — hedge what you can't verify ("as of the
     preseason polls…", "confirm his status Friday") and never fabricate
     a stat or a name. Crucially: say whether the ratings KNOW this —
     our number is built from last season's results, so a team that
     lost its QB1 and both coordinators is overrated by the model, and
     that's exactly the kind of read that overrides it.
  3. **Line movement & money** — open→now from the tape + DK/Circa
     splits; RLM flags (`splits.rlm`); where the sharp money looks to
     be. Plain talk: "the money's on X and the line moved the other
     way".
  4. **Form / common opponents / H2H** — from `history`, but FILTERED
     through section 2: last year's results belong to last year's
     roster. "TCU beat this program 48–14 last September — with a QB
     who's now in the NFL" is analysis; the bare score is trivia.
  5. **MY ANALYSIS** — the synthesis; where you'd side with or against
     our number, and why.
  6. **RECOMMENDATION** — the table: play / line / price-or-better /
     confidence (primary play, sprinkles, leans — or "pass").
- **`data`** — 2-4 sentences in the same voice: the bet (or the pass) +
  the one roster/situational thing that matters.

**EVERY GAME GETS A SPREAD READ (Rob, Sep 1 2026: "I don't give a fuck if
the NCAA are cupcake matches — we bet SPREADS").** A bare "pass" is not an
acceptable bottom line just because the visitor is FCS or our number is
blind to their roster. Keep the honesty about a model gap being noise —
then handicap the MARKET line anyway, like a capper (starters-pull vs
style-points, new-QB debuts, last week's film, blowout management), and
end every game — deep or data tier — with a side of the posted spread and
a confidence. Lean is the floor; "pass" is reserved for a game with no
posted line in the blob.

**Guardrails for the narrative:**
- Every STAT/NUMBER you cite must exist in the blob (roster facts are
  the exception above — hedged, never fabricated).
- The model doesn't know injuries, transfers, or coaching changes — when
  your read overrides the number for those reasons, say so explicitly.
- NCAAF has NO Polymarket rent programs and the NFL model lane is still in
  shadow earn-in — do not describe any sheet play as "the machine is on
  it". These sheets are for humans betting by hand.
- ~45+ deep dives on a September Monday is more than one context holds:
  fan out subagents (Agent tool), 5-8 games each, each writing its
  `sheet_md` rows directly via `sb_patch`, then verify every deep row has
  `sheet_md` before rendering. Give each subagent the VOICE rules above
  verbatim.

## 2B. FRIDAY — changes only

The Friday assembly stamped `data_blob->'friday'` per game:
`{changes: [...], lines, injuries, model, built_at}`. For games with
material changes, write a 1-3 sentence `friday_md` (what changed, whether
it changes the Monday read). Games with no changes: leave `friday_md`
null. If nothing changed anywhere, still publish (the pack prints "No
material changes since Monday").

## 3. Render, publish, ping

```bash
python -m scripts.football_sheet_render --week-key <WEEK> --sport NFL \
    --mode monday --upload --telegram
python -m scripts.football_sheet_render --week-key <WEEK> --sport NCAAF \
    --mode monday --upload --telegram
# Friday: --mode friday
```

**Also render the picks-only sheet, every run, both sports (added Sep 18
2026 — Rob: "I want 1 damn thing that runs sheets... the big PDFs, and a
simple sheet that is just the picks"):**

```bash
python -m scripts.football_sheet_render --week-key <WEEK> --sport NFL \
    --mode <monday|friday> --picks-only --upload --telegram
python -m scripts.football_sheet_render --week-key <WEEK> --sport NCAAF \
    --mode <monday|friday> --picks-only --upload --telegram
```

One line per game (kickoff time, matchup, spread call, total call),
pulled straight from `data_blob->'model'` so it can never disagree with
the full pack — no roster narrative on it, which is the point: it's a
fast reference, and the header says so. Publishes to `picks_pdf_path` /
`picks_published_at` on `football_sheet_weeks` (separate slot from the
full pack — one current picks sheet per week+sport, overwritten on both
the Monday and Friday runs, not two artifacts to track).

That renders the sheet-pack HTML → PDF (Chromium), uploads to the public
`football-sheets` storage bucket, stamps `football_sheet_weeks`, and
queues the Telegram digest (the Vercel `_tg_flush` sends it within ~10
min; links are public — Rob forwards the PDF to friends). Then:

- Stamp the game rows published:
  `./scripts/run_sql.sh "update football_sheets set published_at=now() where week_key='<WEEK>' and published_at is null;"`
  (Friday: `friday_published_at`.)
- Sanity-fetch each public URL (curl the printed `url`, expect 200).

**⚠ THEN SYNC TO PRODUCTION — do not skip this, it is not optional (Sep 19
2026 landmine).** Everything above writes to the ORIGINAL cloud Supabase
project. **The live site does NOT read that database anymore** — the
Cellar cutover repointed Vercel's `SUPABASE_URL` at the box's local
Postgres (`db.thekahlahouse.com`), and nothing keeps `football_sheets` /
`football_sheet_weeks` in sync between the two on its own. Skipping this
step means the assembly and render both report success, the PDFs upload
fine, Telegram queues a link — and `/football-picks` on the website
still shows whatever the box's copy last had, which sat 3+ weeks stale
for two full weeks before anyone noticed. `football-sheets-data.yml`'s
assembly step now runs this automatically (`continue-on-error`, so check
its log if the site still looks stale), but the RENDER step runs in
*this* CCR session, which can't reach thekahlahouse.com at all — trigger
the sync from here via the GitHub MCP:

```
mcp__github__actions_run_trigger: football-sheets-sync.yml, ref main,
  inputs {sport: NFL}   # then again with {sport: NCAAF}
```

If GitHub MCP tools aren't available in this session (Routine-fired
sessions sometimes lack them), say so plainly in your final report —
don't silently skip it. Verify with the shared-secret diagnostic via the
site-curl bridge: `path=api/football-picks-debug` should show
`latest_football_sheet_weeks` with THIS week's `week_key` and a
`published_at` from today, not weeks ago.

## 4. Failure posture

**⚠ CONTAINER RESTARTS ARE REAL AND SILENT (learned Aug 24-25 2026: the
first Monday Routine session died mid-run with zero trace, and a recovery
session lost 3 subagents the same way the next day).** Defenses, in order:
save each narrative to its row the moment it's written (a `sb_patch`ed row
survives a restart; anything held in memory doesn't); fan out writers in
SMALL batches (2-3 games each) so a kill costs little; on wake/start,
check which rows already have `sheet_md` and only write the gaps — the
whole pipeline is resumable by design. If a run fails hard, insert a
telegram_queue row saying so before ending — never die quietly. The
Routines push completion notifications to Rob's phone: a missing
notification by ~7pm Monday IS the alarm.

- Renderer crash on one league must not eat the other — publish what
  works, say what didn't in the Telegram ping.
- A section's source being dark is CONTENT ("injury feed unavailable at
  build time"), not a blocker.
- Anything structural (schema drift, storage 4xx) — fix forward if small;
  otherwise publish what's publishable and leave a clear note for the
  next session in the final report.

## Architecture map (for maintenance sessions)

| Piece | Where | Why there |
|---|---|---|
| Data assembly | `kahla-scanner/scripts/football_sheet_data.py` on Actions (`football-sheets-data.yml`, Mon 5pm + Fri 2pm AZ) | only compute that reaches ESPN + Supabase |
| Model pricing | same script → `_lib/power_ratings.project` + `_lib/gridiron_spread.cover_prob` (slim `normal` fit from the snapshot — graded level with the empirical PMF) | pure-python, no mirror |
| Narrative + publish | fresh CCR session (Monday/Friday Routines, 6:30pm / 3:30pm AZ) following THIS doc | narrative needs a Claude session |
| Storage | `football_sheets` / `football_sheet_weeks` tables + public `football-sheets` storage bucket | Friday diffs against Monday; season archive |
| Delivery | Telegram digest (telegram_queue insert → Vercel `_tg_flush`) with public PDF links | zero new Vercel code |
| Tiering | assembly `decide_tiers` — NFL all deep; NCAAF ranked/edge-gated, cap 18 | Rob-approved Aug 2026 |

Landmines already hit: ESPN CFB scoreboard needs `groups=80&limit=400` or
it returns only the featured slate; **`site.api.espn.com` hard-403s the
Actions runners on EVERY request now (Aug 2026 — the per-day trick died
too), while `site.web.api.espn.com` serves the same site/v2 paths
unblocked — `_espn_get` falls back host-wise, proven live (injuries, AP
ranks, scoreboard all landed via the web host)**; team names need
accent-folding (`_fold`) to join ratings/game_results ("San José State",
"Hawai'i") and spelling drift also mints DUPE sheet rows under the
`(week_key, sport, event_name)` unique key — the monday build sweeps rows
it didn't touch, gated on ESPN having answered; `_SEASON_FLOOR` (mirror
of app.py `_GRIDIRON_MIN_START`, **update yearly**) keeps preseason out —
the first shakedown built 27 preseason NFL sheets priced off
regular-season ratings before it existed; `SUPABASE_URL` env in the CCR
sandbox is wrapped in literal `<>`.
