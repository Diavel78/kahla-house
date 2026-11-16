# The weekly zone review — runbook

Rob, Nov 15 2026: *"Every Sunday at 11 pm, you will analyze the last week, add it to the
database for all sports, and make any zone changes needed."* A scheduled task wakes the
Claude session that holds this repo every Sunday ~10:40 PM Arizona and it follows this file
(a fresh session with no repo attached stopped at step 1 on Nov 15 2026 — the task is bound
to a session with the repo now). `site_curl.sh` is REST-only: `gh workflow run` / `gh run`
use GraphQL, which Claude sandboxes block. Read CLAUDE.md's
🔥 ZONES paragraph (app.py, `_ZONES`) first.

## What the zones are
The 🔥 on the sheets fires at the END of a measured window where Pinnacle's move means
something: **chase** = the 🔥 on the side Pinnacle moved to; **fade** = the 🔥 on the other
side, 💨 on the side the money went. The live list is `machine_flags zones` (else `_ZONES`
in app.py); `machine_flags zone_rule` turns the rule on/off.

## Steps (run serially — site_curl.sh reads back the newest workflow run)
S=kahla-scanner/scripts/site_curl.sh

1. **Save the week.** For each sport in NBA NHL NFL NCAAF:
   `off=0; loop: $S api/move-thresh-scan "sport=$SP&days=9&off=$off&n=25&pts=1&save=1&quiet=1" out.json`
   — read `games_total`, `saved`, `skip`; `off += 25` until `off >= games_total`.
   This writes each finished game-market's checkpoints to `lookup_cache` rows `zcp|…`
   (kept forever — the line tape itself is pruned at 60 days). A `skip.save_err` = stop and report.
2. **Dump the whole history.** `$S api/zone-data "off=$off&n=300" dump_$off.json` until `next` is null.
   The first dump also carries `zones_live` (what is live now) and `zone_rule`.
3. **Grade.** `python3 kahla-scanner/scripts/zone_review.py /tmp/zr dump_*.json` → `/tmp/zr.md`
   (every window × sport × market, the qualifying zones) and `/tmp/zr.zones.json` (the proposal).
   The selection rule is in the script's docstring; do not hand-pick around it.
4. **Decide.** Compare the proposal with `zones_live`. Apply the proposal when it differs, unless:
   fewer than 400 game-markets in the history, or a zone flips kind (chase↔fade) on the same
   window with < 2 weeks of new data behind the flip — then keep the live zone and say why.
5. **Apply.** URL-encode the JSON and set the flag:
   `$S api/cellar/flag "flag=zones&value=<urlencoded json>" flag.json` — check `ok`.
   If `zone_rule` is false and Rob hasn't said to keep it off, leave it — only Rob turns the rule on/off.
6. **Report.** Commit `/tmp/zr.md` as `docs/zone-reviews/YYYY-MM-DD.md` (Arizona date) to main
   with a short header: games added this week, zones added / dropped / changed and why,
   and last week's 🔥 record per zone. Push main.
7. **Tell Rob** in the session's final message, plain and short: what changed, what didn't, why.

Never place, cancel or change a bet. Never touch any flag but `zones`.

The `_STANDING_ZONES` in app.py (college follow-the-money spots, the NFL spread fade) are NOT
in `zones` and the weekly flag never replaces them — leave them alone; report in the review
how each fired and graded this week.
The college night-follow / day-fade zones are THIS season's regime: dispatch
`cfb-dump.yml`, parse the log (`DUMP-BEGIN`…`DUMP-END`, gzip+base64), and report the last
4 weeks' night-follow and day-fade records at the close. If either has turned (losing 4
weeks running), tell Rob — don't change it yourself.

SIGNALS THAT CAME AND WENT: `lookup_cache` rows `zsig|SPORT|mt|start|event` hold every distinct
🔥 a market showed (zone, side, first seen) and the one Rob's bet locked (`bet_lock`). Grade both:
the locked 🔥 vs the later signal when they differ — that is the review Rob asked for.
BLENDS: `zsig` signals with `kind: blend` are markets where no zone fired but 2+ near-misses
(each ≥40% of its threshold) pointed the same way, summing to a full threshold. Not on the card.
Grade them weekly; first read (Pinnacle tape Oct 29 – Nov 14): NHL 17-15, NBA 4-0, college 1-2.
Recommend promoting a sport's blend to the card only when it's beating the close over 50+ games.
