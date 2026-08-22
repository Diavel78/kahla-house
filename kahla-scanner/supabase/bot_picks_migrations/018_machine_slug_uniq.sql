-- ONE MACHINE PICK PER POLYMARKET SLUG — the retry-safety net for
-- _autobet_execute's book write (Aug 21 2026, the wa-gercol-gte2 orphan).
--
-- The failure it exists for: the venue order is created FIRST, then the
-- bot_picks insert -- and a dropped connection between them left a live,
-- filled, PAID position with no book entry (Aug 20 14:01, found only when
-- the venue-truth day RPC disagreed with the pick ledger by exactly that
-- win). The fix retries the insert on disconnect; a retry after an
-- ambiguous drop can double-commit, and THIS index is what makes that
-- impossible -- the second insert fails into it, which the code treats as
-- success.
--
-- Scoped to machine-created rows (source autobet / whiff_autobet /
-- ou_trader) with a slug, AND id > 2682: the pre-dedup era (Aug 9) holds
-- real 2-3x duplicate rows that a bare unique index would refuse to build
-- over. Cleaning those is a history-correction decision for the weekend
-- review, not a side effect of a midnight migration.
--
-- Idempotent. Apply: kahla-scanner/scripts/run_sql.sh -f <this file>

create unique index if not exists bot_picks_machine_slug_uniq
  on bot_picks ((signal_blob->>'pmm_slug'), (signal_blob->>'source'))
  where signal_blob->>'source' in ('autobet', 'whiff_autobet', 'ou_trader')
    and signal_blob->>'pmm_slug' is not null
    and id > 2682;
