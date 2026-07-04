-- Diamond IQ Phase 1: per-game MLB pitching logs from the official
-- MLB Stats API boxscores. Who ACTUALLY started every game (not the
-- probable) + every pitcher's line — the walk-forward raw material for
-- the pitcher-quality layer, leakage-free (the nhl_goalie_games
-- pattern). Spring training / exhibition / all-star excluded at ingest.
--
-- Run via kahla-scanner/scripts/run_sql.sh -f <this file>. Idempotent.

create table if not exists mlb_pitcher_games (
  game_pk       bigint  not null,
  pitcher_id    bigint  not null,
  game_date     date,
  game_type     text,            -- R / F / D / L / W
  team          text,            -- abbreviation, e.g. NYY
  opponent      text,
  home          boolean,
  pitcher_name  text,
  started       boolean,         -- first pitcher used = the actual starter
  outs          integer,         -- IP in outs (5.2 IP = 17)
  batters_faced integer,
  hits          integer,
  runs          integer,
  earned_runs   integer,
  strikeouts    integer,
  walks         integer,
  home_runs     integer,
  inserted_at   timestamptz default now(),
  primary key (game_pk, pitcher_id)
);

create index if not exists mlb_pitcher_games_date_idx
  on mlb_pitcher_games (game_date);
create index if not exists mlb_pitcher_games_pitcher_idx
  on mlb_pitcher_games (pitcher_id, game_date);
