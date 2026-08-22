-- THE CELLAR'S VITAL SIGNS — and the whole point is that a HEARTBEAT IS
-- NOT HEALTH.
--
-- On Aug 18 2026 the `ledger` lane sat at owner=cellar with a five-second
-- heartbeat for twenty-two hours while stamping absolutely nothing: its
-- engine was gated off by a modulo that belonged to Vercel's tick, so it
-- claimed, ran, returned zero, and renewed — forever. Every liveness
-- signal we had was green. It was found by a human noticing a P&L number
-- looked wrong two days later.
--
-- So this function reports two independent clocks per lane:
--
--   last_tick_s  — when the lane last RAN      (liveness)
--   last_work_s  — when it last DID SOMETHING  (usefulness)
--
-- A large gap between them is the signature of that failure, and it is
-- the only thing that would have caught it. Ticks and work over the last
-- hour come along so a glance distinguishes "quiet slate" from "wedged".
--
-- Cheap by construction: one seq scan of cellar_lease (ten rows) and
-- index-only lookups on cellar_ticks_lane_time per lane.
--
-- last_fail_s / last_ok_s (Aug 21 2026): red must mean "failing NOW",
-- not "hiccuped once in the trailing hour". A lane ticking 60x/hour over
-- a network takes the occasional dropped connection; its work is
-- idempotent and the next tick catches up. Painting that identical to a
-- lane that is down is the false-alarm disease (repeg `work never`, the
-- opener stuck-line — third sighting in one night). These two columns
-- let Flask ask the only question that matters: did the MOST RECENT
-- completed tick fail?
--
-- Return type changed => drop first (create or replace cannot).
drop function if exists cellar_health();
create function cellar_health()
returns table (
  lane text, owner text, note text,
  hb_age_s integer, ttl_seconds integer,
  last_tick_s integer, last_work_s integer,
  ticks_1h integer, work_1h bigint, fails_1h integer, last_error text,
  last_fail_s integer, last_ok_s integer
) language sql stable as $$
  select
    l.lane, l.owner, l.note,
    extract(epoch from (now() - l.heartbeat_at))::integer,
    l.ttl_seconds,
    (select extract(epoch from (now() - t.started_at))::integer
       from cellar_ticks t where t.lane = l.lane
      order by t.started_at desc limit 1),
    -- NULL here means "has never done a unit of work in the retained
    -- window", which is a louder statement than any large number.
    (select extract(epoch from (now() - t.started_at))::integer
       from cellar_ticks t where t.lane = l.lane and t.work > 0
      order by t.started_at desc limit 1),
    (select count(*)::integer from cellar_ticks t
      where t.lane = l.lane and t.started_at > now() - interval '1 hour'),
    coalesce((select sum(t.work) from cellar_ticks t
               where t.lane = l.lane
                 and t.started_at > now() - interval '1 hour'), 0),
    (select count(*)::integer from cellar_ticks t
      where t.lane = l.lane and t.started_at > now() - interval '1 hour'
        and not t.ok),
    (select left(t.error, 140) from cellar_ticks t
      where t.lane = l.lane and t.error is not null
      order by t.started_at desc limit 1),
    (select extract(epoch from (now() - t.started_at))::integer
       from cellar_ticks t where t.lane = l.lane and not t.ok
      order by t.started_at desc limit 1),
    (select extract(epoch from (now() - t.started_at))::integer
       from cellar_ticks t where t.lane = l.lane and t.ok
      order by t.started_at desc limit 1)
  from cellar_lease l
  order by l.lane;
$$;
