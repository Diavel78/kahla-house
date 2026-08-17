-- THE CELLAR — single-writer lease + heartbeat.
--
-- Spec: docs/cellar-migration-spec.md §5 ("one writer, ever").
--
-- WHY THIS EXISTS: during cutover the house box and Vercel are both live.
-- If both run _opener_pass you get duplicate resting orders on real money --
-- the documented 9-duplicates-in-2-minutes failure. Every engine call on
-- BOTH sides claims its lane first; the loser is a silent no-op.
--
-- OWNERSHIP MODEL (asymmetric on purpose):
--   'cellar' is PRIMARY   -- may preempt a live 'vercel' lane at any time.
--   'vercel' is STANDBY   -- may only take a lane whose heartbeat has gone
--                            stale (i.e. the house went dark).
-- So cutover of a lane = the cellar simply starts running it. Rollback =
-- the cellar stops; Vercel reclaims one TTL later, with no deploy.
--
-- Idempotent. Apply with: kahla-scanner/scripts/run_sql.sh -f <this file>

create table if not exists cellar_lease (
  lane          text        primary key,
  owner         text        not null default 'vercel',
  heartbeat_at  timestamptz not null default now(),
  ttl_seconds   integer     not null default 180,
  note          text,
  updated_at    timestamptz not null default now()
);

-- One row per lane. Seeded owner='vercel' with a STALE heartbeat: today
-- Vercel runs these unconditionally, so seeding it "live" would be a lie,
-- and a stale row lets either side take the lane on first claim.
insert into cellar_lease (lane, owner, heartbeat_at, ttl_seconds, note)
values
  ('pm_snapshot',    'vercel', 'epoch', 180, 'exchange cent logger'),
  ('paperlog',       'vercel', 'epoch', 180, 'suggestion + shadow logger'),
  ('opener',         'vercel', 'epoch', 180, 'opener lane + autobet (NEW MONEY)'),
  ('repeg',          'vercel', 'epoch', 180, 'maker order chase (WRITES)'),
  ('harvest',        'vercel', 'epoch', 180, 'take-profit sells (WRITES)'),
  ('ledger',         'vercel', 'epoch', 300, 'poly money ledger'),
  ('vsin',           'vercel', 'epoch', 900, 'circa/dk splits logger'),
  ('kalshi_autolog', 'vercel', 'epoch', 300, 'kalshi fill -> bot_picks'),
  ('alerts',         'vercel', 'epoch', 180, 'telegram flush + bet/outbid pings'),
  ('batch',          'vercel', 'epoch', 3600, 'daily ingests + model computes')
on conflict (lane) do nothing;


-- Atomic claim. Single UPDATE => Postgres row locking serializes racers and
-- the loser re-evaluates the WHERE after the winner commits, so exactly one
-- caller can hold a lane. Server clock only -- never trust the caller's.
create or replace function cellar_claim(
  p_lane  text,
  p_owner text,
  p_ttl   integer default null
) returns boolean
language plpgsql
as $$
declare v_ok boolean;
begin
  update cellar_lease
     set owner        = p_owner,
         heartbeat_at = now(),
         ttl_seconds  = coalesce(p_ttl, ttl_seconds),
         updated_at   = now()
   where lane = p_lane
     and (
          owner = p_owner                                              -- renew my own
       or heartbeat_at < now() - make_interval(secs => ttl_seconds)     -- holder is dead
       or (p_owner = 'cellar' and owner = 'vercel')                     -- primary preempts standby
     )
  returning true into v_ok;
  return coalesce(v_ok, false);
end $$;


-- Voluntary handback: expire my own heartbeat so the other side can take the
-- lane immediately instead of waiting out the TTL. Called on clean shutdown.
create or replace function cellar_release(
  p_lane  text,
  p_owner text
) returns boolean
language plpgsql
as $$
declare v_ok boolean;
begin
  update cellar_lease
     set heartbeat_at = now() - make_interval(secs => ttl_seconds + 1),
         updated_at   = now()
   where lane = p_lane and owner = p_owner
  returning true into v_ok;
  return coalesce(v_ok, false);
end $$;


-- Heartbeat / observability.
--
-- LANDMINE THIS ANSWERS (spec §8 item 2): the ESPN spine painted six days of
-- green checkmarks while creating nothing. `work` records UNITS OF WORK DONE,
-- not "the job ran" -- so the watchdog can alert on a lane that is healthy,
-- on time, and producing zero.
create table if not exists cellar_ticks (
  id          bigserial   primary key,
  lane        text        not null,
  owner       text        not null,
  started_at  timestamptz not null default now(),
  duration_ms integer,
  ok          boolean     not null default true,
  claimed     boolean     not null default true,   -- false = lost the lease, no-op
  work        integer     not null default 0,      -- rows/orders/games actually touched
  detail      jsonb,
  error       text
);

create index if not exists cellar_ticks_lane_time
  on cellar_ticks (lane, started_at desc);

-- Retention: this table is high-frequency. Nightly cleanup keeps 14 days,
-- matching the book_snapshots convention.
create index if not exists cellar_ticks_time
  on cellar_ticks (started_at);
