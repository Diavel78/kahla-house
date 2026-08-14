-- Local mirror of the Polymarket activities feed.
--
-- WHY (Aug 13 2026): every consumer walked the live feed newest-first
-- under a fixed page cap. That cap was written when the account placed a
-- few bets a day; at machine scale (~500 activities daily) 2,000 rows is
-- about four days, so the dashboard's Maker Rewards card silently halved
-- as older payouts fell off the end, and each page load made 20 fresh
-- sequential round trips. A fixed window can only postpone that.
--
-- The feed is append-only history, so mirror it once and read locally.
-- `payload` keeps the RAW activity json verbatim: parse_activities() and
-- compute_summary() then work unchanged, whether rows come from the
-- venue or from here.

create table if not exists poly_activities (
    key        text primary key,           -- transactionId when present, else payload sha1
    at         timestamptz,                -- activity time (detail.updateTime/timestamp)
    type       text,                       -- raw ACTIVITY_TYPE_*
    slug       text,
    payload    jsonb not null,
    synced_at  timestamptz not null default now()
);

create index if not exists poly_activities_at_idx on poly_activities (at desc);
create index if not exists poly_activities_type_idx on poly_activities (type);

-- Backfill bookmark. The incremental sync only ever needs the newest few
-- pages; walking back to March is a one-time job that must be resumable
-- because a serverless call cannot hold the whole feed in one request.
create table if not exists poly_activities_state (
    id               int primary key default 1,
    backfill_cursor  text,
    backfill_done    boolean not null default false,
    pages_walked     int not null default 0,
    updated_at       timestamptz not null default now()
);

insert into poly_activities_state (id) values (1) on conflict (id) do nothing;
