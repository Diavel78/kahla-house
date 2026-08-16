-- Precomputed dashboard summary — one row, refreshed on the tick.
--
-- Aug 16 2026. The slim dashboard needed `total_pnl`, which needs the
-- summary, which needed a 15-page sequential walk of the Polymarket
-- activities feed on EVERY page load. The obvious fix — read the feed
-- from the poly_activities mirror instead — is explicitly rejected in
-- CLAUDE.md and for good reason: the whole feed is ~10k payloads ≈ 10 MB
-- per read, the module cache dies with each serverless container, and a
-- dashboard polling every 60s already put Supabase egress at 138% of
-- quota once and took the site down.
--
-- So neither source is read at page time. The tick already walks the feed
-- (_acts_sync, every 5th minute); it now also computes the summary once
-- and parks the RESULT here. The dashboard reads one row of a few hundred
-- bytes. Egress cost is trivial and constant, independent of history
-- length, and the page stops waiting on the venue entirely.
--
-- The P&L parsing stays in Python on purpose — parse_activities carries
-- the complement-price landmines (SDK `price` is the opposite side, use
-- cost/qty; realizedPnl is a sell INDICATOR not a value) and
-- reimplementing that in SQL would fork the money math into two places.
--
-- Apply: kahla-scanner/scripts/run_sql.sh -f kahla-scanner/supabase/poly_dash_cache.sql

create table if not exists poly_dash_cache (
    id           int primary key default 1,
    summary      jsonb not null,      -- compute_summary output
    balance      numeric(14,4),
    open_count   int,
    computed_at  timestamptz not null default now(),
    note         text,
    constraint poly_dash_cache_singleton check (id = 1)
);
