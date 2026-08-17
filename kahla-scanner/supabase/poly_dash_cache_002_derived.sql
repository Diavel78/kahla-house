-- Park the dashboard's DERIVED blocks next to the summary.
--
-- Aug 17 2026. The slim dashboard was designed to answer from ONE cached
-- row, and it did — until the day/rent cards were added on top of it.
-- Each one brought its own Supabase scan back onto the page load:
-- _maker_rewards_split (500 activity PAYLOADS), _rent_window (5000
-- earnings rows), _window_pnl (3000 bot_picks), _incentive_day_rewards
-- (2000), _gameday_pnl (1000). Six queries, three of them large scans
-- aggregated in Python — exactly the shape poly_dash_cache exists to
-- avoid, and the page got measurably slower again.
--
-- Same fix as before, applied to the rest of the page: the tick computes
-- these blocks once and parks them here; the dashboard reads one row.
--
-- Apply: kahla-scanner/scripts/run_sql.sh -f <this file>

alter table poly_dash_cache
    add column if not exists derived jsonb;
