-- bot_picks migration 008 — NRFI / YRFI first-inning market.
--
-- Adds a new market_type 'nrfi' (No Run First Inning) with sides
-- 'no' (NRFI — neither team scores in the 1st) and 'yes' (YRFI — a run
-- scores in the top or bottom of the 1st). MLB only.
--
-- This is the Pick Bot's first DERIVATIVE/prop market. It does NOT use
-- PIN line movement (NRFI isn't in our odds feed) — it's an OUR-NUMBER
-- play priced from a first-inning model in handicapper_web.py
-- (_nrfi_model): P(NRFI) = q_top · q_bot, two independent scoreless
-- half-innings driven by the opposing starter + the top of the order +
-- park + weather. entry_line stays NULL (no line, just yes/no).
--
-- Grading is fully automatic and clean (unlike UFC method bets): the
-- resolver reads ESPN's first-inning linescore — any run in inning 1 →
-- YRFI. It can even grade early, the moment the 1st inning completes,
-- without waiting for the final.
--
-- Run via kahla-scanner/scripts/run_sql.sh -f (or the Supabase SQL
-- editor). Idempotent — safe to re-run.

-- market_type: add 'nrfi' to the allowed set.
alter table bot_picks drop constraint if exists bot_picks_market_type_check;
alter table bot_picks add  constraint bot_picks_market_type_check
  check (market_type in ('moneyline','spread','total','nrfi'));

-- side: add 'yes' / 'no' for the first-inning market alongside the
-- existing home/away/over/under.
alter table bot_picks drop constraint if exists bot_picks_side_check;
alter table bot_picks add  constraint bot_picks_side_check
  check (side in ('home','away','over','under','yes','no'));
