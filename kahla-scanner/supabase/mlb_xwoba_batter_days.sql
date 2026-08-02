-- Diamond IQ ITER4 (the Statcast spine): per-batter per-DAY expected
-- wOBA aggregates from Baseball Savant's statcast CSV. Daily grain =
-- walk-forward safe (rolling xwOBA rebuilt as-of any date, no season
-- aggregate leakage). Thesis: blending batter quality toward xwOBA
-- regresses luck out faster than realized results alone.
-- Run via kahla-scanner/scripts/run_sql.sh -f <this file>. Idempotent.
create table if not exists mlb_xwoba_batter_days (
  game_date   date    not null,
  batter_id   bigint  not null,
  pa          integer,          -- PA-ending events with an xwOBA value
  xwoba_sum   numeric,          -- sum of estimated wOBA over those PAs
  inserted_at timestamptz default now(),
  primary key (game_date, batter_id)
);
create index if not exists mlb_xwoba_batter_days_batter_idx
  on mlb_xwoba_batter_days (batter_id, game_date);
