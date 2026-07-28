-- prop_snapshots + prop_suggestions — the PROP BETS pipeline (July 2026).
--
-- Thesis (user): "Markets move the same, we can log the same." Props on
-- Polymarket/Kalshi are priced order books exactly like the main lines, so
-- the SAME movement engine applies: log the cent history, score the move,
-- suggest the side money is arriving on.
--
-- prop_snapshots: price history per prop, one row per (game, venue, prop)
-- each time the YES-mid cent CHANGES (deduped, the pm_snapshots pattern).
-- Written by the existing /api/pm-snapshot cron tick — the props ride the
-- SAME pmm_markets.lookup() call that already fetches the main lines, so
-- ingestion costs zero extra network.
--
-- prop_suggestions: the current movement verdict per prop, upserted each
-- tick for the games processed. `cleared` = the recency-weighted move
-- cleared the suggestion gate; side = the direction money is arriving on
-- (YES price rising -> yes, falling -> no; "harder = sharp", same rule as
-- the main markets). The games-list "Prop Bets X" badge counts cleared
-- rows; /api/handicapper/props merges these scores onto live quotes.
--
-- Idempotent. Run via kahla-scanner/scripts/run_sql.sh -f or the SQL editor.

create table if not exists prop_snapshots (
    id          bigserial primary key,
    market_id   uuid        not null,   -- our markets.id (the game)
    venue       text        not null,   -- 'polymarket' | 'kalshi'
    prop_key    text        not null,   -- PMM slug (stable per market) / Kalshi ticker
    question    text,                   -- human-readable market question
    prop_type   text,                   -- PMM v1 slug (e.g. baseball_player_home_runs)
    line        numeric,                -- prop line when the market has one
    cents       integer     not null,   -- YES mid implied prob, 1-99
    captured_at timestamptz not null default now()
);

create index if not exists prop_snapshots_latest_idx
    on prop_snapshots (market_id, venue, prop_key, captured_at desc);
create index if not exists prop_snapshots_captured_idx
    on prop_snapshots (captured_at);

create table if not exists prop_suggestions (
    market_id   uuid        not null,
    prop_key    text        not null,
    venue       text        not null default 'polymarket',
    question    text,
    prop_type   text,
    line        numeric,
    side        text,                   -- 'yes' | 'no' (direction of the move)
    score       numeric,                -- recency-weighted cent movement (abs)
    cleared     boolean     not null default false,
    cents       integer,                -- current YES mid at last update
    updated_at  timestamptz not null default now(),
    primary key (market_id, prop_key)
);

create index if not exists prop_suggestions_cleared_idx
    on prop_suggestions (market_id, cleared, updated_at desc);
