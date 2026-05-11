-- Bot recommendations get auto-logged on dossier view so PMM sync
-- always has a row to link the user's real bet to. These rows use a
-- new status='recommended' which:
--   • doesn't get graded by the ESPN/PMM resolver (they're not bets,
--     just records of what the bot would have suggested);
--   • doesn't show on the /handicapper pending list (those are bets
--     the user actually committed to);
--   • flips to 'pending' the moment the PMM sync (or a manual click
--     on "Log this pick") links a real bet to the row.
--
-- Run in Supabase SQL editor. Idempotent.

alter table bot_picks drop constraint if exists bot_picks_status_check;
alter table bot_picks add constraint bot_picks_status_check
  check (status in ('pending','recommended','won','lost','push','void'));

-- Default stays 'pending' — manual "Log this pick" clicks shouldn't
-- need to pass status. Auto-log path sets status='recommended'
-- explicitly when inserting.
