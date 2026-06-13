-- pm_snapshots — prediction-market price history for the cross-confirm
-- detector. One row per (our game, source, side) each time the cent price
-- CHANGES (deduped, like book_snapshots), from the free Polymarket + Kalshi
-- feeds. Written every ~1 min by /api/pm-snapshot (Flask, cron-pinged).
--
-- Purpose: (1) the detector's memory — compare current cents to the last
-- stored value to catch a >=1c move; (2) the validation dataset — prove the
-- PMM+Kalshi 1c-AND fires cleanly AND measure how many minutes these free
-- feeds lead our paid Pinnacle (Odds API) capture in book_snapshots.
--
-- `cents` = mid of the side's bid/ask in integer cents (1-99) = implied
-- win probability. Anchored to OUR markets.id so it joins straight to
-- book_snapshots (PINN) by market_id for the lead/lag analysis.
--
-- Idempotent. Run via kahla-scanner/scripts/run_sql.sh -f or the SQL editor.
create table if not exists pm_snapshots (
    id          bigserial primary key,
    market_id   uuid        not null,   -- our markets.id (the game)
    source      text        not null,   -- 'pmm' | 'kalshi'
    side        text        not null,   -- 'home' | 'away'
    cents       integer     not null,   -- mid implied prob, 1-99
    captured_at timestamptz not null default now()
);

-- Latest-per-(game,source,side) lookups for the dedup + move check.
create index if not exists pm_snapshots_latest_idx
    on pm_snapshots (market_id, source, side, captured_at desc);
-- Retention sweeps + time-range scans.
create index if not exists pm_snapshots_captured_idx
    on pm_snapshots (captured_at);
