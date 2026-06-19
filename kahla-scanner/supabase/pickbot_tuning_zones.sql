-- pickbot_tuning: add `zones` (jsonb list of [lo,hi] minute segments) so the
-- prime window can be MULTI-ZONE. Live paperlog showed the edge is bimodal —
-- 30-90 and 150-210 win, 90-120 is a losing hole between them — which a single
-- contiguous (prime_lo,prime_hi) span can't express. _load_prime_zones reads
-- `zones` and falls back to [[prime_lo,prime_hi]] when null. Idempotent.

alter table pickbot_tuning add column if not exists zones jsonb;

-- Seed the data-driven two-zone window (computed June 2026 from 222 settled
-- gate-cleared ML/SPR/TOT paperlog rows). The weekly tuner overwrites this.
update pickbot_tuning
   set zones = '[[30,90],[150,210]]'::jsonb,
       prime_lo = 30, prime_hi = 210,   -- envelope (back-compat readers)
       updated_at = now(),
       basis = jsonb_build_object(
                 'metric','two_zone_unit_roi',
                 'seeded_from','222 settled gate-cleared timed paperlog rows',
                 'zones','[[30,90],[150,210]]',
                 'note','hole at 90-120 (-0.32/u) + cold tails excluded')
 where id = 1;
