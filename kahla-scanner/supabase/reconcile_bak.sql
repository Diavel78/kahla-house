-- reconcile_bak — backup shelf for the venue-truth reconcile pass
-- (app.py:_reconcile_tick, Aug 28 2026). Every bot_picks row the pass
-- deletes as a confirmed ZOMBIE (venue killed the resting order, no fill,
-- no trade evidence) is inserted here FIRST; a failed insert blocks the
-- delete (default-deny). Replaces the hand-made dated bak tables the
-- Aug 23/27 fire drills used (gridiron_ghosts_bak_0823 etc).
-- Idempotent. Apply via kahla-scanner/scripts/run_sql.sh -f <this file>.

create table if not exists reconcile_bak (
  id       bigserial primary key,
  at       timestamptz not null default now(),
  pick_id  bigint,
  reason   text,
  row      jsonb
);

create index if not exists reconcile_bak_at_idx on reconcile_bak (at desc);
