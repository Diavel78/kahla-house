-- Football player game logs — the training spine for football PROP models
-- (the mlb_pitcher_games pattern: who actually played and their line,
-- leakage-free because it is parsed from final boxscores).
--
-- One row per (sport, game, player), stat columns merged across the ESPN
-- boxscore categories (passing/rushing/receiving/fumbles/kicking). Nullable
-- by design — a WR has no passing line. Game universe = game_results (which
-- already excludes preseason), so this table can never contain a game the
-- ratings pipeline does not know.
--
-- Apply: kahla-scanner/scripts/run_sql.sh -f kahla-scanner/supabase/football_player_games.sql

create table if not exists football_player_games (
  id             bigserial primary key,
  sport          text not null default 'NFL',
  espn_event_id  text not null,
  event_start    timestamptz,
  game_date      date,
  season         int,
  team           text,
  opponent       text,
  home           boolean,
  player_id      text not null,
  player_name    text,
  position       text,
  -- passing
  pass_cmp int, pass_att int, pass_yds int, pass_td int, pass_int int,
  sacks int, sack_yds int,
  -- rushing
  rush_att int, rush_yds int, rush_td int, rush_long int,
  -- receiving
  rec int, rec_tgts int, rec_yds int, rec_td int, rec_long int,
  -- fumbles
  fum int, fum_lost int,
  -- kicking
  fg_made int, fg_att int, xp_made int, xp_att int, kick_pts int,
  ingested_at    timestamptz not null default now(),
  unique (sport, espn_event_id, player_id)
);

create index if not exists fpg_player_date
  on football_player_games (player_name, game_date);
create index if not exists fpg_event
  on football_player_games (espn_event_id);
