# THE TAPE PROCESS SPLIT — step 3 of "get off REST" (spec, Sep 12 2026)

## Why

Every venue read the money lanes make costs 0.5s idle and 13-27s on the
box, because one interpreter is shared with three parse-heavy lanes that
need none of the money process's live state:

| lane | work per lap | what it is |
|---|---|---|
| pm_snapshot | 55-80 `pmm_markets.lookup` megapayloads every ~90s | the cent tape (pm_snapshots, prop_snapshots) |
| paperlog | one `build_dossier` (70-137s contended) + syncs | the suggestion logger + dashboard/incentive syncs |
| vsin | 6 VSiN pages + Circa, 400-1500s contended | the splits tape |

Measured Sep 12 (CFB Saturday): opener REST pricing 27s each under
contention, 0.5s per page idle. Steps 1-2 (lookup cache, rung window,
120s pack audit) cut the money lanes' REST *count*; this step cuts the
*cost* of every remaining read by taking the parse CPU off their GIL.

## Shape

Two daemons from the same checkout, same `.env`, same local Postgres:

- **money process** (today's daemon minus the tape lanes):
  `opener, repeg, scalp, alerts, ledger` + the three websocket feeds
  (private, markets ×N, depth) + the venue mirror + the quote/depth
  tables + the snipers. `CELLAR_LANES` as today minus the tape lanes.
- **tape process**: `pm_snapshot, paperlog, vsin, kalshi_autolog, batch,
  grader`. No sockets, no mirror, no venue WRITES. `CELLAR_SIDE=cellar`
  still (the lease table arbitrates per lane, and the two processes never
  claim the same lane — enforce in `Runner.validate`: a lane may be in
  exactly one process's roster; a second launchd plist,
  `com.kahlahouse.cellard-tape`).

## What breaks if you just move the lanes (found reading the code)

1. **`_fbprop_pass` PLACES BETS from inside the pm-snapshot route** (the
   NFL props lane, `_autobet_execute` at app.py ~15332) and
   `_whiff_shadow_pass` prices pitcher props from the same in-memory
   `prop_rows`. Both must move to the money process: make them read the
   newest `prop_snapshots` rows per game (already written by the tape,
   deduped on change) instead of the lap's in-memory list, and run them
   from the opener lane's slice. Until they move, pm_snapshot stays in the
   money process.
2. **Props → socket push** (`_WS_PROPS_CB` / `props_add`) happens in the
   pm-snapshot route. Move the push to the money process: the opener
   lane reads `prop_snapshots` for its games and pushes the slugs.
3. **Lookup result cache** (`pmm_markets._LOOKUP_CACHE`, step 1) is
   in-process. Cross-process handoff: the tape writes each lookup result
   (ml/spread/total/nrfi lists only — no props, ~5-20 KB) to a
   `lookup_cache` table keyed by the same UTC key with `fetched_at`; the
   money pricer reads it when younger than `_LOOKUP_REUSE_S` (a local
   Postgres read + a small JSON parse — not a megapayload). Persisted
   `ladder_cache` already follows this pattern.
4. **`_pmm_autolog` / entry sync** runs from the paperlog route's
   fill-status path only on Vercel; on the box the repeg lane owns it.
   Nothing to move.
5. **`_tg_flush` / `_bet_alerts`** ride the paperlog route body; the
   alerts lane also flushes. The lease already makes that safe.
6. **cellar_boot stamp + `code <sha>` on the dashboard**: stamp per
   process (`side=cellar`, `proc=money|tape`); the health card shows both.

## Order of work

1. Move `_fbprop_pass` + `_whiff_shadow_pass` + the props push to the
   opener lane, reading `prop_snapshots` (money process only). Verify one
   NFL-props placement and one whiff shadow row after the move.
2. `lookup_cache` table + the pricer's DB-read path (falls back to the
   in-process cache, then REST).
3. Second plist + roster validation; cut the tape lanes over in a slate
   lull; watch `ws_price.rest_s`, opener/repeg lap times, and that
   pm_snapshot rows keep landing.

Expected: opener laps well under 60s, repeg 5-15s, the slate walked every
minute. Do it after a Sunday slate, not during one.

## Built (Sep 12 2026, dormant until `CELLAR_TAPE_SPLIT=1`)

- `app._prop_passes_tick` runs the whiff + NFL-props passes from the opener
  lane (socket prop rows + newest `prop_snapshots`); the pm-snapshot route
  skips them under the flag (`fbp_gate: tape_split`).
- `lookup_cache` table (DDL `kahla-scanner/supabase/lookup_cache.sql`,
  applied on the box) + `pmm_markets._LOOKUP_DB_PUT/_GET` hooks planted by
  app: every fetch is persisted (props dropped); a `max_age_s` read that
  misses in-process reads the table before REST.
- `scripts/com.kahlahouse.cellard-tape.plist`: the tape roster, no sockets,
  own journal dir (`~/.cellar-tape`), `CELLAR_PROC=tape` (boot stamps carry
  `proc`). `Runner.validate` accepts a tape roster under the flag.

## Cutover (do it in a lull; every step reversible)

1. `.env`: add `CELLAR_TAPE_SPLIT=1`; set
   `CELLAR_LANES=opener,repeg,scalp,alerts,ledger` (the money roster).
2. `sudo launchctl kickstart -k system/com.kahlahouse.cellard` — money
   process up on the new roster; confirm `cellar_boot` stamp shows
   `proc=money`, lanes = the five.
3. Install + load the tape plist (INSTALL block in the plist). Confirm a
   second `cellar_boot` stamp with `proc=tape` and `pm_snapshot` /
   `paperlog` ticks landing in `cellar_ticks`.
4. Watch 20 min: `ws_price.rest_s` per opener lap, opener/repeg lap
   times, `pm_snapshots` rows still inserting, `lookup_cache` rows
   accruing, `props` stats on the opener tick (`fbprop`, `whiff_shadows`).
5. Rollback: `sudo launchctl bootout system/com.kahlahouse.cellard-tape`,
   restore the two `.env` lines, kickstart the money daemon.
