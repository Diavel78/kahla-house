-- Snapshot table for the NFL props model state (the whiff_iq_snapshot
-- pattern): compute_football_props.py serializes per-player game history
-- + league priors here daily; the app-side tail mirror prices captured
-- props from it at bet time. One row, id=1.
--
-- Apply: kahla-scanner/scripts/run_sql.sh -f kahla-scanner/supabase/football_props_snapshot.sql

create table if not exists football_props_snapshot (
  id        int primary key,
  state     jsonb not null,
  engine    text,
  built_at  timestamptz not null default now()
);
