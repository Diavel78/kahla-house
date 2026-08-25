CREATE OR REPLACE FUNCTION public.cellar_health()
 RETURNS TABLE(lane text, owner text, note text, hb_age_s integer, ttl_seconds integer, last_tick_s integer, last_work_s integer, ticks_1h integer, work_1h bigint, fails_1h integer, last_error text, last_fail_s integer, last_ok_s integer)
 LANGUAGE sql
 STABLE
AS $function$
  select
    l.lane, l.owner, l.note,
    extract(epoch from (now() - l.heartbeat_at))::integer,
    l.ttl_seconds,
    (select extract(epoch from (now() - t.started_at))::integer
       from cellar_ticks t where t.lane = l.lane
      order by t.started_at desc limit 1),
    -- NULL here means "has never done a unit of work in the retained
    -- window", which is a louder statement than any large number.
    -- PAPERLOG EXCEPTION (Aug 25 2026): its core logger deliberately runs
    -- on BOTH sides (only the engines are lease-gated), and Vercel's cron
    -- ping often wins the per-minute insert race -- the box's ticks then
    -- honestly report work=0 while pickbot_paperlog fills. The lane's real
    -- product is the TABLE, so its work-clock reads the table directly;
    -- the false "idle 15h" card this fixes was work happening off-meter.
    case when l.lane = 'paperlog' then
      least(
        coalesce((select extract(epoch from (now() - t.started_at))::integer
                    from cellar_ticks t
                   where t.lane = l.lane and t.work > 0
                   order by t.started_at desc limit 1), 2147483647),
        coalesce((select extract(epoch from (now() - max(p.logged_at)))::integer
                    from pickbot_paperlog p), 2147483647))
    else
      (select extract(epoch from (now() - t.started_at))::integer
         from cellar_ticks t where t.lane = l.lane and t.work > 0
        order by t.started_at desc limit 1)
    end,
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
$function$
