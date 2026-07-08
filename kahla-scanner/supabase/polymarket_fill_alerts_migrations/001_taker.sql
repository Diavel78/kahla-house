-- 001: maker/taker classification per tracked order (July 2026).
-- `taker = true` means the order was first seen ALREADY terminal-filled
-- (created + fully filled between two check-fills ticks, never observed
-- resting) — i.e. it executed as a taker and its effective entry is
-- fill + taker fee. Anything we ever tracked open rested = maker
-- (fill − maker rebate). Read by the Pick Bot entry auto-sync
-- (_fs_auto_sync_entry) to pick the right fee side when restamping a
-- pick's entry_price to the real Polymarket fill.
-- NULL (pre-migration rows) is treated as maker — the common case.
-- Idempotent.
alter table polymarket_fill_state
    add column if not exists taker boolean;
