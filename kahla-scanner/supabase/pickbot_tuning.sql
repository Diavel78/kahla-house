-- pickbot_tuning — singleton config row the Pick Bot reads at runtime for
-- its data-driven prime betting window. Written weekly by
-- scripts/tune_prime_window.py (grid-search over pickbot_paperlog,
-- optimize unit-ROI with a CLV guardrail), read by
-- handicapper_web._load_prime_window. The (60,180) seed matches the old
-- hand-set constant so behaviour is unchanged until the first tune.
--
-- Idempotent. Run via scripts/run_sql.sh -f or the Supabase SQL editor.

create table if not exists pickbot_tuning (
    id          int primary key default 1,
    prime_lo    int not null,
    prime_hi    int not null,
    updated_at  timestamptz not null default now(),
    basis       jsonb,
    constraint pickbot_tuning_singleton check (id = 1)
);

insert into pickbot_tuning (id, prime_lo, prime_hi, basis)
values (1, 60, 180, '{"seed": true}'::jsonb)
on conflict (id) do nothing;
