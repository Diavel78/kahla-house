-- Diamond IQ ITER5 (platoon spine): per-batter per-day REALIZED wOBA
-- split by opposing pitcher hand, + the static pitcher-throws map.
-- Source: the same Savant statcast_search CSV the xwOBA ingest fetches
-- (columns `p_throws`, `pitcher`, `woba_value` on PA-ending rows) —
-- one fetch feeds both spines via ingest_savant_xwoba --platoon.
-- Realized (not expected) wOBA on purpose: ITER4 graded xwOBA out.
-- APPLIED to the live DB Aug 3 2026 via run_sql.sh.
create table if not exists mlb_platoon_batter_days (
    game_date date not null,
    batter_id integer not null,
    vs_hand text not null check (vs_hand in ('L', 'R')),
    pa integer not null,
    woba_sum numeric not null,
    primary key (game_date, batter_id, vs_hand)
);

create table if not exists mlb_pitcher_throws (
    pitcher_id integer primary key,
    throws text not null check (throws in ('L', 'R', 'S')),
    updated_at timestamptz default now()
);
