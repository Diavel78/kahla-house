-- Diamond IQ LIVE snapshot: the walk-forward engine state serialized
-- daily (compute_diamond_iq.py) so Flask — which cannot import the
-- kahla-scanner subproject — can price MLB moneylines from the model
-- (the power_ratings pattern). Latest row wins.
-- Run via kahla-scanner/scripts/run_sql.sh -f <this file>. Idempotent.
create table if not exists diamond_iq_snapshot (
  id          bigserial primary key,
  computed_at timestamptz default now(),
  snapshot    jsonb not null
);
