-- parlay-api.com integration state (bet-time Pinnacle stamp).
-- One generic k/v row store: DB-backed odds cache (survives Vercel cold
-- starts — an in-memory cache would refetch per cold start and burn the
-- free 1,000 credits/mo) + the monthly usage counter the hard budget
-- guard reads. Apply via kahla-scanner/scripts/run_sql.sh -f.
create table if not exists parlay_state (
    k          text primary key,
    v          jsonb not null,
    updated_at timestamptz not null default now()
);
