-- VSiN betting-splits time series (Circa + DraftKings handle% / bets%).
--
-- The free, low-latency sharp-money log: every ~15 min we snapshot Circa
-- (sharp — no bet limits) and DraftKings (public — limited book) handle% and
-- bets% for every upcoming game's ML / spread / total, deduped so a row only
-- lands when a number actually moves. That movement curve is how we see WHEN
-- sharp money hits Circa (early vs late steam), per market, across the whole
-- slate — not just games we bet. Mirrors pm_snapshots (the exchange-cent
-- series); the resolver stamps each pick's CLOSING Circa read off this table
-- the same way CLV pulls the closing line.
--
-- Apply: kahla-scanner/scripts/run_sql.sh -f kahla-scanner/supabase/vsin_snapshots.sql
create table if not exists vsin_snapshots (
    id           bigserial primary key,
    market_id    uuid,                 -- our games.markets row (matched by team name)
    book         text not null,        -- 'circa' | 'draftkings'
    market_type  text not null,        -- 'ml' | 'spread' | 'total'
    side         text not null,        -- 'home' | 'away' | 'over' | 'under'
    line         numeric,              -- spread/total line (NULL for ml)
    handle_pct   integer,              -- money % (Circa handle = sharp money)
    bets_pct     integer,              -- tickets % (public)
    captured_at  timestamptz not null default now()
);

-- Hot read paths: (a) latest-per-key for the dedup-on-change insert,
-- (b) per-game pre-event_start lookup for the resolver's closing stamp.
create index if not exists vsin_snapshots_key_idx
    on vsin_snapshots (market_id, book, market_type, side, captured_at desc);
create index if not exists vsin_snapshots_captured_idx
    on vsin_snapshots (captured_at desc);
