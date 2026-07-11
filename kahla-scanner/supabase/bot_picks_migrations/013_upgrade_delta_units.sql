-- bot_picks migration 013 — allow quarter-unit-increment sizing.
--
-- The Pick Bot's upgrade flow (a 0.5u lean that later clears the gate and
-- becomes a real 1u/3u pick) now logs the DELTA as a second row at the
-- current odds, instead of a full-size duplicate. e.g. 0.5u logged at +105,
-- upgrade to 3u → a 2.5u row logged at the live price (total exposure 3u
-- across two maker fills at two prices). Those deltas (0.75, 2, 2.5, 2.75,
-- and any future combo if 5u re-enables) are NOT in the old fixed set
-- {0.25,0.5,1,3,5,10}, so the CHECK rejected them.
--
-- Fix: allow ANY quarter-unit increment in (0, 10] — future-proof against
-- every target/logged combination, not just the ones enumerated today.
-- All prior values (0.25/0.5/1/3/5/10) remain valid, so existing rows and
-- the resolver's UPDATE-on-settle still pass the re-check.
--
-- Run via kahla-scanner/scripts/run_sql.sh -f (or the Supabase SQL
-- editor). Idempotent — safe to re-run.

alter table bot_picks drop constraint if exists bot_picks_units_check;
alter table bot_picks add  constraint bot_picks_units_check
  check (units > 0 and units <= 10 and units * 4 = floor(units * 4));
