-- pickbot_tuning: add `kelly_fraction` (the runtime aggression dial the
-- weekly sizing auto-tuner writes). handicapper_web._load_kelly_fraction
-- reads it; NULL → fall back to the KELLY_FRACTION constant (0.25 quarter-
-- Kelly). Intentionally NOT seeded — it stays dormant (constant behaviour)
-- until tune_sizing.py has enough settled NEW-SCALE picks (~2 weeks after
-- the June 2026 exchange cutover) to prove higher-edge picks earn more.
-- Idempotent.

alter table pickbot_tuning add column if not exists kelly_fraction numeric;
