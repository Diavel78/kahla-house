-- bot_picks migration 010 — allow 0.25-unit sizing.
--
-- NRFI/YRFI drops from 0.5u to 0.25u (user's call — quarter-unit on the
-- experimental first-inning market). Add 0.25 to the units CHECK. KEEP
-- 0.5 in the set: existing 0.5u NRFI rows must stay valid so the resolver
-- can still UPDATE them on settle (Postgres re-checks the row on UPDATE).
--
-- Run via kahla-scanner/scripts/run_sql.sh -f (or the Supabase SQL
-- editor). Idempotent — safe to re-run.

alter table bot_picks drop constraint if exists bot_picks_units_check;
alter table bot_picks add  constraint bot_picks_units_check
  check (units in (0.25, 0.5, 1, 3, 5, 10));
