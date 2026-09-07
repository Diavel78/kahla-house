-- RESOLUTION LEGS per Arizona game day, DEDUPED, one row per leg — the
-- set-based half of the day math handed back to Python so the COST can be
-- taken from OUR lot ledger instead of the venue's blended
-- beforePosition.cost (the Braves 74¢ lesson, Sep 7 2026: 60 bought / 46
-- sold across four lots read a 39.2¢ lot as $10.34 of cost).
-- Same dedupe + UNDEFINED-twin rules as poly_gameday_pnl; no math here.
-- Apply: psql kahla -f kahla-scanner/supabase/poly_gameday_legs.sql

create or replace function poly_gameday_legs(p_days int default 3)
returns table (az_day date, slug text, side text, net numeric, cost numeric,
               gst timestamptz, res_at timestamptz)
language sql stable as $$
  with r0 as (
    select distinct on (
        payload->'positionResolution'->'market'->>'slug',
        payload->'positionResolution'->>'side',
        payload->'positionResolution'->'beforePosition'->>'netPosition',
        payload->'positionResolution'->'beforePosition'->'cost'->>'value')
      payload->'positionResolution'->'market'->>'slug' as slug,
      payload->'positionResolution'->>'side' as side,
      coalesce((payload->'positionResolution'->'beforePosition'
              ->>'netPositionDecimal')::numeric,
               (payload->'positionResolution'->'beforePosition'
              ->>'netPosition')::numeric) as net,
      (payload->'positionResolution'->'beforePosition'
              ->'cost'->>'value')::numeric as cost,
      coalesce(payload->'positionResolution'->'market'->>'gameStartTime',
               payload->'positionResolution'->>'updateTime') as gst,
      coalesce((payload->'positionResolution'->>'updateTime')::timestamptz,
               (payload->'positionResolution'->'beforePosition'->>'updateTime')::timestamptz,
               at) as res_at
    from poly_activities
    where type = 'ACTIVITY_TYPE_POSITION_RESOLUTION'
      and at > now() - ((p_days + 2) || ' days')::interval
    order by payload->'positionResolution'->'market'->>'slug',
             payload->'positionResolution'->>'side',
             payload->'positionResolution'->'beforePosition'->>'netPosition',
             payload->'positionResolution'->'beforePosition'->'cost'->>'value',
             at desc
  ),
  r as (
    select * from r0 a
    where a.side <> 'POSITION_RESOLUTION_SIDE_UNDEFINED'
       or not exists (
         select 1 from r0 b
         where b.slug = a.slug and b.net = a.net and b.cost = a.cost
           and b.side <> 'POSITION_RESOLUTION_SIDE_UNDEFINED')
  )
  select (gst::timestamptz at time zone 'America/Phoenix')::date,
         slug, side, net, cost, gst::timestamptz, res_at
  from r
  where gst is not null
    and (gst::timestamptz at time zone 'America/Phoenix')::date
         >= ((now() at time zone 'America/Phoenix')::date - p_days);
$$;
