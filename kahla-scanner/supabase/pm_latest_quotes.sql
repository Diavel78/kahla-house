-- LATEST QUOTE PER RUNG (Sep 20 2026). pm_snapshots is deduped on CHANGE, so
-- "rows in the last 30 minutes" is not the board — it is only the rungs that
-- moved. The pair seeder asked that way and saw 92 of 93 ladders as unpriced.
-- This returns the most recent row per (market, type, side, line), which IS
-- the current price, however long it has been sitting there.
create or replace function pm_latest_quotes(p_market_ids uuid[], p_hours int default 36)
returns table (market_id uuid, market_type text, side text, line numeric,
               bid_c int, ask_c int, captured_at timestamptz)
language sql stable as $$
  select distinct on (s.market_id, s.market_type, s.side, s.line)
         s.market_id, s.market_type, s.side, s.line, s.bid_c, s.ask_c, s.captured_at
  from pm_snapshots s
  where s.market_id = any(p_market_ids) and s.source = 'pmm'
    and s.captured_at > now() - (p_hours || ' hours')::interval
    and s.line is not null and s.bid_c is not null and s.ask_c is not null
  order by s.market_id, s.market_type, s.side, s.line, s.captured_at desc;
$$;
