# FOOTBALL WEEKLY GAME SHEETS — BUILT Aug 22 2026

> STATUS: LIVE. The living doc is **`docs/football-sheet-runbook.md`** —
> procedure, architecture map, and landmines all live there. The original
> scoping spec (reference format, data source map, open decisions) is in
> git history pre-Aug-22-2026.
>
> The four open decisions were resolved in the build session (Rob can
> override any of them):
> 1. **Website page** — deferred; PDFs publish to the public
>    `football-sheets` storage bucket and the Telegram digest carries the
>    links. A `/football` page mock was proposed to Rob; build it only on
>    his yes.
> 2. **NCAAF tiering** — NFL all deep; NCAAF deep on ranked involvement /
>    model-vs-market gap ≥1.5 pts (aligned with the PLAY bar — Rob:
>    "anything over 1.5 on total and spread is a play"), capped 18/week;
>    the rest get the data sheet + short read. `decide_tiers` in
>    `kahla-scanner/scripts/football_sheet_data.py`.
> 3. **Week-0 shakedown** — yes; the first Monday Routine fire
>    (Aug 24 2026, 6:30pm AZ) runs the Aug 29 slate as the dry run.
> 4. **Where it runs** — data assembly on GitHub Actions
>    (`football-sheets-data.yml`, Mon 5pm + Fri 2pm AZ — the only compute
>    reaching both ESPN and Supabase); narrative + PDF + publish in
>    fresh-session Routines (Mon 6:30pm / Fri 3:30pm AZ). The Cellar is
>    not involved — sheets are analysis only.
