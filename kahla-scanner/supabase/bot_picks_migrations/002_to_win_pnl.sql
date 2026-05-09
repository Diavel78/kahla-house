-- bot_picks migration 002 — to-WIN PnL recompute.
--
-- Rationale: the resolver was using to-RISK sizing (a 3u bet at +123
-- counted as +3.69u on a win, -3u on a loss). User bets TO WIN units,
-- not to risk them. Recompute pnl_units for already-settled rows so
-- the lifetime totals match the new model.
--
-- Per-row math:
--   push / void                → 0
--   won                        → +units
--   lost @ +N (positive entry) → -units · 100 / N
--   lost @ -N (negative entry) → -units · |N| / 100
--
-- Idempotent: re-running yields the same values.

update bot_picks
set pnl_units = case
    when status in ('push', 'void') then 0
    when status = 'won'  then units::numeric
    when status = 'lost' and entry_price > 0
        then -units::numeric * 100.0 / entry_price
    when status = 'lost' and entry_price < 0
        then -units::numeric * abs(entry_price) / 100.0
    else pnl_units
end
where status in ('won', 'lost', 'push', 'void');
