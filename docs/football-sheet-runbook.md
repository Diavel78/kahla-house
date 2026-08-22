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

**Depth by tier** (the assembly already decided `tier` + `tier_reasons`):

- **`deep`** — the full read, modeled on the Alamo Bowl reference format:
  1. **What the model sees** — Gridiron IQ margin (calibrated), total, win
     prob, cover prob at the posted line; where model and market disagree,
     in points and pp. The blob's `model` node has every number.
  2. **Line movement & splits read** — open→now from the exchange tape +
     DK/Circa; call out RLM flags (`splits.rlm`), handle-vs-tickets
     divergence, where the sharp money looks to be.
  3. **Personnel** — who actually takes the field. Injuries from the blob;
     weigh the impact (a starting QB out is worth ~a TD — the model does
     NOT know injuries, say so explicitly when one moves the number).
  4. **Form / common opponents / H2H** — from `history` (labeled 2025
     season until current-season results accrue).
  5. **MY ANALYSIS** — the synthesis. Where you'd disagree with the model
     and why.
  6. **RECOMMENDATION** — a small table: play / line / confidence
     (primary play, sprinkles, leans — or "pass"). Analysis for humans
     betting by hand; be honest when there's nothing.
- **`data`** — 2-4 sentences: the model read, the one thing that stands
  out, lean or pass.

**Guardrails for the narrative:**
- Every number you cite must exist in the blob. The model does not know
  injuries/news — when you adjust off the model, label it as your read.
- The model's raw margins are too extreme by construction — cite
  `margin_cal`/`total_cal`, never `margin_raw` as the headline.
- NCAAF has NO Polymarket rent programs and the NFL model lane is still in
  shadow earn-in — do not describe any sheet play as "the machine is on
  it". These sheets are for humans.
- ~45+ deep dives on a September Monday is more than one context holds:
  fan out subagents (Agent tool), 5-8 games each, each writing its
  `sheet_md` rows directly via `sb_patch`, then verify every deep row has
  `sheet_md` before rendering.

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

That renders the sheet-pack HTML → PDF (Chromium), uploads to the public
`football-sheets` storage bucket, stamps `football_sheet_weeks`, and
queues the Telegram digest (the Vercel `_tg_flush` sends it within ~10
min; links are public — Rob forwards the PDF to friends). Then:

- Stamp the game rows published:
  `./scripts/run_sql.sh "update football_sheets set published_at=now() where week_key='<WEEK>' and published_at is null;"`
  (Friday: `friday_published_at`.)
- Sanity-fetch each public URL (curl the printed `url`, expect 200).

## 4. Failure posture

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
it returns only the featured slate; ESPN 403s multi-day `dates=A-B` from
Actions (per-day loop); team names need accent-folding (`_fold`) to join
ratings/game_results ("San José State", "Hawai'i"); `SUPABASE_URL` env in
the CCR sandbox is wrapped in literal `<>`.
