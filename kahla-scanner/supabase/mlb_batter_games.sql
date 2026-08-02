-- Diamond IQ Phase 3 (the BATTER spine): per-game MLB batting logs from
-- the official MLB Stats API boxscores. The model's offense was a
-- team-aggregate blob while its pitching was player-level — this table
-- closes that gap: who was ACTUALLY in the lineup (batting_order from
-- the boxscore, leakage-free — in reality the lineup posts pre-game)
-- and every batter's line, for a rolling per-batter quality layer.
-- Spring training / exhibition / all-star excluded at ingest (the
-- power-ratings preseason lesson).
--
-- Run via kahla-scanner/scripts/run_sql.sh -f <this file>. Idempotent.

create table if not exists mlb_batter_games (
  game_pk       bigint  not null,
  batter_id     bigint  not null,
  game_date     date,
  game_type     text,            -- R / F / D / L / W
  team          text,            -- abbreviation, e.g. NYY
  opponent      text,
  home          boolean,
  batter_name   text,
  batting_order integer,         -- lineup slot 1-9 (from boxscore battingOrder/100)
  started       boolean,         -- in the starting lineup (battingOrder % 100 == 0)
  ab            integer,
  runs          integer,
  hits          integer,
  doubles       integer,
  triples       integer,
  home_runs     integer,
  rbi           integer,
  walks         integer,
  strikeouts    integer,
  hbp           integer,
  sac_flies     integer,
  stolen_bases  integer,
  inserted_at   timestamptz default now(),
  primary key (game_pk, batter_id)
);

create index if not exists mlb_batter_games_date_idx
  on mlb_batter_games (game_date);
create index if not exists mlb_batter_games_batter_idx
  on mlb_batter_games (batter_id, game_date);
