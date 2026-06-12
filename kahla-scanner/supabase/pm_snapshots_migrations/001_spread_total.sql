-- 001_spread_total.sql — pm_snapshots grows market_type + line so the
-- exchange feed can carry PMM spreads/totals alongside moneylines.
--
-- WHY (June 2026): user decision to retire The Odds API ($59/mo) and run
-- the whole odds stack on Polymarket + Kalshi. The exchange history in
-- this table becomes the source for the sharp score, the odds-board
-- sparklines/movement, and CLV — but the Pick Bot suggests ML/SPR/TOT,
-- so ML-only history isn't enough. PMM's lookup() already returns
-- spread/total quotes in the same call the ML logger makes; this just
-- gives the rows somewhere to land.
--
-- market_type: 'ml' | 'spread' | 'total'. Existing rows backfill to 'ml'
-- via the default. line: spread/total point (e.g. -1.5, 8.5); NULL for ml.
-- Dedup key in /api/pm-snapshot becomes
-- (market_id, source, market_type, side, line).
--
-- Idempotent. Run via kahla-scanner/scripts/run_sql.sh -f.
alter table pm_snapshots
    add column if not exists market_type text not null default 'ml';
alter table pm_snapshots
    add column if not exists line numeric;

-- Replace the latest-per-key index with one that includes market_type
-- (line is low-cardinality per key and filtered client-side).
drop index if exists pm_snapshots_latest_idx;
create index if not exists pm_snapshots_latest_mt_idx
    on pm_snapshots (market_id, source, market_type, side, captured_at desc);
