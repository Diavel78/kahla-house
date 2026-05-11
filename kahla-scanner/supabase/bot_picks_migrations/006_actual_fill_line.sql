-- Track the ACTUAL line the user bet on Polymarket separately from
-- the bot's recommended line. PMM offers `.5` lines exclusively on
-- SPR/TOT (no pushes possible), so when the bot recommends e.g.
-- TOT 9.0 from PIN and the user bets TOT 9.5 on PMM, both numbers
-- should be recorded — entry_line stays the bot's view,
-- actual_fill_line is what the user actually played.
--
-- Run in Supabase SQL editor. Idempotent.

alter table bot_picks
  add column if not exists actual_fill_line numeric;
