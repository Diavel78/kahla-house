-- PICK SHEETS FOR EVERY SPORT (Sep 30 2026 — Rob: "hockey (and eventually
-- all sports sheets) added to the pick sheets"). football_sheets /
-- football_sheet_weeks are the generic pick-sheet store now; the sport
-- CHECK only allowed NFL/NCAAF. Idempotent. Apply to BOTH databases:
--   cloud: kahla-scanner/scripts/run_sql.sh -f supabase/pick_sheets_sports.sql
--   box:   psql kahla -f supabase/pick_sheets_sports.sql   (nhl_sheet_data
--          self-applies this when it finds psql on disk)
alter table football_sheets drop constraint if exists football_sheets_sport_check;
alter table football_sheets add constraint football_sheets_sport_check
    check (sport in ('NFL','NCAAF','NHL','MLB','NBA','CBB','UFC'));
alter table football_sheet_weeks drop constraint if exists football_sheet_weeks_sport_check;
alter table football_sheet_weeks add constraint football_sheet_weeks_sport_check
    check (sport in ('NFL','NCAAF','NHL','MLB','NBA','CBB','UFC'));
notify pgrst, 'reload schema';
