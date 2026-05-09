-- bot_picks migration 001 — add 10u/whale tier, drop max tier.
--
-- Rationale: sizing rubric changed to:
--   low → 1u
--   medium → 3u (was 1u)
--   high → 5u (was 3u)
--   whale → 10u (was 'max', 5u)
--
-- Run in Supabase SQL editor on the existing bot_picks table. Idempotent
-- via `if exists` / `if not exists` patterns; safe to re-run.

-- Rename any existing 'max' confidence rows to 'whale' BEFORE swapping
-- the constraint, otherwise the constraint add will reject them.
update bot_picks set confidence = 'whale' where confidence = 'max';

-- Replace check constraints. Postgres auto-names them <table>_<col>_check
-- when no explicit name is given, which the original DDL didn't.
alter table bot_picks drop constraint if exists bot_picks_confidence_check;
alter table bot_picks add  constraint bot_picks_confidence_check
  check (confidence in ('low','medium','high','whale'));

alter table bot_picks drop constraint if exists bot_picks_units_check;
alter table bot_picks add  constraint bot_picks_units_check
  check (units in (1,3,5,10));
