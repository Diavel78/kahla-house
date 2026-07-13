-- Kalshi authed-reader credentials, DB-resident option (the
-- parlay_state.api_key precedent — user's call to keep creds out of the
-- Vercel dashboard). Read by app.py:_kalshi_creds as the fallback behind
-- the KALSHI_API_KEY_ID / KALSHI_PRIVATE_KEY env vars; 5-min cached.
-- The key VALUES are inserted manually via run_sql.sh — never commit them.
create table if not exists kalshi_state (
  id smallint primary key default 1 check (id = 1),
  key_id text,
  private_key text,          -- RSA private key PEM (read-only API key)
  updated_at timestamptz not null default now()
);
