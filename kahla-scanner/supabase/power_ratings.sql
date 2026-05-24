-- Power-ratings pipeline tables (the bot's independent model).
--
-- Two tables, both written by the kahla-scanner cron, read by Flask:
--   game_results  — every completed game's final score (from ESPN).
--                   The raw material the ratings are solved from.
--   power_ratings — one snapshot row per sport per compute run, holding
--                   the opponent-adjusted off/def/net ratings as jsonb.
--                   Flask reads the latest row per sport.
--
-- Run in the Supabase SQL editor. Idempotent.

create table if not exists game_results (
  id           bigserial primary key,
  sport        text not null,                 -- MLB / NBA / NHL / NFL / CBB / NCAAF
  espn_id      text not null,                 -- ESPN event id (dedup key)
  event_start  timestamptz,
  game_date    date,                          -- US/Eastern calendar date
  home         text not null,
  away         text not null,
  home_score   numeric not null,
  away_score   numeric not null,
  ingested_at  timestamptz not null default now(),
  unique (sport, espn_id)
);

create index if not exists game_results_sport_start_idx
  on game_results (sport, event_start desc);

-- One snapshot per (sport) per compute run. ratings is the full blob:
--   { "<team>": {"off":.., "def":.., "net":.., "gp":.., "w":..}, ... }
-- Flask reads the most recent row per sport (order by computed_at desc).
create table if not exists power_ratings (
  id             bigserial primary key,
  sport          text not null,
  computed_at    timestamptz not null default now(),
  as_of          timestamptz,                 -- ratings computed "as of" this time
  league_avg     numeric,
  n_games        int,
  half_life_days numeric,
  ratings        jsonb not null,
  params         jsonb                         -- {hfa, scale, ...} used
);

create index if not exists power_ratings_latest_idx
  on power_ratings (sport, computed_at desc);

-- Lock anon out — same posture as the other scanner tables.
alter table game_results  enable row level security;
alter table power_ratings enable row level security;
