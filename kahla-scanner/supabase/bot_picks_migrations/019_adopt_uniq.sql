-- Widen the machine-slug unique net to pmm_autolog rows: the prop-orphan
-- ADOPTION path (the wa-gercol fix's second half) inserts as
-- source='pmm_autolog', and its retry/race safety comes from this index
-- exactly like the autobet insert's does — a duplicate-key IS success.
-- Same id floor: the pre-dedup era's real duplicates stay out of scope.
--
-- Idempotent. Apply: kahla-scanner/scripts/run_sql.sh -f <this file>

drop index if exists bot_picks_machine_slug_uniq;
create unique index if not exists bot_picks_machine_slug_uniq
  on bot_picks ((signal_blob->>'pmm_slug'), (signal_blob->>'source'))
  where signal_blob->>'source' in ('autobet', 'whiff_autobet', 'ou_trader',
                                   'pmm_autolog')
    and signal_blob->>'pmm_slug' is not null
    and id > 2682;
