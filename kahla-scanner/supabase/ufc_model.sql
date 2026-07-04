-- Fight IQ model snapshots (Phase 2, July 2026). One row per weekly
-- compute_ufc_model.py run: fitted params + every fighter's current
-- FightElo/state. Flask reads the LATEST row (Phase 3), mirroring the
-- power_ratings snapshot pattern. Idempotent.

create table if not exists ufc_model (
  id          bigserial primary key,
  computed_at timestamptz not null default now(),
  params      jsonb not null,   -- {scale, beta, n_fit_records, n_fights}
  ratings     jsonb not null    -- {fighter_id: {elo,n,last,ko2,fin_rate,name}}
);

create index if not exists ufc_model_computed_idx on ufc_model (computed_at desc);
