-- Link bot_picks rows to actual Polymarket positions so the user's
-- real bets become the source of truth for "did I take this pick"
-- + actual fill price + resolution. Rather than rebuilding the
-- Polymarket → game mapping, we reuse the enriched position data
-- the dashboard already computes (team name, outcome string).
--
-- One pick row = one bet. Recommendation columns (entry_price, units,
-- confidence) stay as the bot's view; new columns track what actually
-- happened on Polymarket.
--
-- Run in Supabase SQL editor. Idempotent.

alter table bot_picks
  add column if not exists actual_fill_price   integer,       -- American odds at PMM fill (converted from PMM 0-1 share price)
  add column if not exists actual_fill_qty     numeric,       -- contracts taken (1 contract = 1 unit per user convention)
  add column if not exists actual_fill_at      timestamptz,   -- when the PMM position was opened
  add column if not exists actual_fill_pnl     numeric,       -- dollar P&L per Polymarket (settled positions only)
  add column if not exists polymarket_slug     text,          -- PMM market slug, e.g. yankees-vs-mets-2026-05-10
  add column if not exists polymarket_outcome  text,          -- raw enriched outcome string, e.g. "Yankees", "Over 8.5"
  add column if not exists pmm_side            text           -- 'YES' / 'NO' on the PMM contract (separate from our home/away)
                          check (pmm_side is null or pmm_side in ('YES','NO')),
  add column if not exists auto_linked         boolean not null default false;

-- True if the row was auto-created from a PMM position with no matching
-- bot recommendation (user bet something the bot didn't suggest).

-- Index for the sync job's "find existing pick to link" lookup.
create index if not exists bot_picks_pmm_link_idx
  on bot_picks (market_id, market_type, side, picked_at desc);

-- Index for "show me everything I actually bet" queries.
create index if not exists bot_picks_pmm_filled_idx
  on bot_picks (actual_fill_at desc)
  where actual_fill_price is not null;
