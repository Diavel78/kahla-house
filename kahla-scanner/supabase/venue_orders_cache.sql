-- Sep 7 2026: the box's resting-orders snapshot for the slim dashboard's
-- Resting Orders card, written every repeg lap from the venue mirror. The
-- site reads this instead of asking the venue on every 30s poll (Vercel
-- function time + a venue call per poll). New table ⇒ notify pgrst.
create table if not exists venue_orders_cache (
  id          integer primary key,
  computed_at timestamptz not null default now(),
  orders      jsonb not null
);
