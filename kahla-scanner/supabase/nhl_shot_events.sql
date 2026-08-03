-- Crease IQ P3 (shot-quality spine): every UNBLOCKED shot attempt
-- (goal / shot-on-goal / missed-shot = Fenwick) with rink coordinates,
-- shooter, goalie-in-net and strength state, from the free NHL API
-- play-by-play (api-web.nhle.com/v1/gamecenter/{id}/play-by-play).
-- The P2 verdict named this lever: raw shot-count sv% can't separate
-- goalies — an xG model (distance/angle/type) over these events yields
-- GSAx + team xG for/against. Blocked shots excluded on purpose (the
-- block location isn't the shot origin — standard xG practice).
-- Game universe = nhl_goalie_games (already regular+playoffs only).
-- APPLIED to the live DB Aug 3 2026 via run_sql.sh.
create table if not exists nhl_shot_events (
    game_id bigint not null,
    event_id integer not null,
    game_date date not null,
    event_type text not null,          -- goal | shot-on-goal | missed-shot
    is_goal boolean not null default false,
    period integer,
    time_in_period text,
    situation text,                    -- raw situationCode (strength state)
    team_id integer,                   -- eventOwnerTeamId (shooting team)
    shooter_id integer,
    goalie_id integer,                 -- goalieInNetId (null on empty net)
    x numeric,
    y numeric,
    shot_type text,
    zone text,
    primary key (game_id, event_id)
);
create index if not exists nhl_shot_events_date_idx
    on nhl_shot_events (game_date);
