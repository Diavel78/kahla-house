-- Fix the earnings key: STATUS belongs in it.
--
-- The API reference (docs.polymarket.us/institutional/incentives/overview)
-- states plainly:
--
--   "A single market on a single date may return multiple rows — one per
--    payout status (PAID, PENDING, SKIPPED). Sum across statuses (or
--    filter to one) when aggregating per market and date."
--
-- The first cut of this table keyed on (market_slug, earn_date,
-- program_type) and summed collisions under a single winning status. That
-- collapses a market-day that is part PAID and part PENDING into one row
-- and mislabels it — destroying the accrual-vs-paid split this mirror
-- exists to provide. Caught by reading the reference instead of inferring
-- the shape from a sample.
--
-- Small table, freshly built, no history worth preserving: rebuild it.
-- The sync repopulates from the venue on the next tick.
--
-- Apply: run_sql.sh -f kahla-scanner/supabase/poly_incentives_002_status_key.sql

drop table if exists poly_incentive_earnings;

create table poly_incentive_earnings (
    market_slug   text        not null,
    earn_date     date        not null,   -- EASTERN TIME per the venue
    program_type  text        not null default 'liquidityProgram',
    status        text        not null,   -- PAID | PENDING | SKIPPED
    reward        numeric(12,4) not null default 0,
    first_seen_at timestamptz not null default now(),
    updated_at    timestamptz not null default now(),
    primary key (market_slug, earn_date, program_type, status)
);

create index if not exists poly_inc_earn_date_idx
    on poly_incentive_earnings (earn_date desc);
create index if not exists poly_inc_earn_status_idx
    on poly_incentive_earnings (status);
create index if not exists poly_inc_earn_slug_idx
    on poly_incentive_earnings (market_slug);
