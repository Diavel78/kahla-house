-- Crease IQ Phase 1 (July 2026): per-game NHL goalie logs from the
-- official NHL API boxscores (api-web.nhle.com, free/no-auth). The
-- raw material for the goalie-quality model that replaces the dead
-- 50/50 GAA blend (team core backtested 53.3% — no signal without
-- knowing who's in net). Populated by scripts/ingest_nhl_goalies.py.
-- Idempotent.

create table if not exists nhl_goalie_games (
  game_id       bigint  not null,          -- NHL API game id
  goalie_id     bigint  not null,          -- NHL API player id
  game_date     date,
  game_type     int,                        -- 2 regular, 3 playoffs (1 pre excluded)
  team          text,                       -- NHL tricode (BOS, VGK, ...)
  opponent      text,
  home          boolean,
  goalie_name   text,
  starter       boolean,                    -- started the game (the load-bearing bit)
  shots_against int,
  saves         int,
  goals_against int,
  toi           text,                       -- mm:ss on ice
  decision      text,                       -- W / L / O
  created_at    timestamptz not null default now(),
  primary key (game_id, goalie_id)
);

create index if not exists nhl_goalie_games_date_idx
  on nhl_goalie_games (game_date);
create index if not exists nhl_goalie_games_goalie_idx
  on nhl_goalie_games (goalie_id, game_date);
