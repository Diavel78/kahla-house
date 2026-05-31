-- bot_picks migration 009 — allow 0.5-unit sizing.
--
-- NRFI/YRFI picks are logged at a flat 0.5u while we evaluate the
-- first-inning model against live results (user's call — half a unit
-- until the setup is reviewed). The units column was `integer` with a
-- check in (1,3,5,10); widen it to numeric and add 0.5 to the allowed
-- set. Existing integer rows cast cleanly to numeric.
--
-- Run via kahla-scanner/scripts/run_sql.sh -f (or the Supabase SQL
-- editor). Idempotent — safe to re-run.

alter table bot_picks alter column units type numeric using units::numeric;

alter table bot_picks drop constraint if exists bot_picks_units_check;
alter table bot_picks add  constraint bot_picks_units_check
  check (units in (0.5, 1, 3, 5, 10));
