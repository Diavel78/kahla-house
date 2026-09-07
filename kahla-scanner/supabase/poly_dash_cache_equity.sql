-- Sep 7 2026: equity inputs on the box's cached dashboard row (open stakes at
-- cost + venue mark). Applied LOCAL the same morning — the box's cache upsert
-- silently failed for 25 minutes without them and the dashboard fell back to
-- the live walk (Vercel timeout). New columns ⇒ notify pgrst, 'reload schema'.
alter table poly_dash_cache add column if not exists open_cost numeric(14,4);
alter table poly_dash_cache add column if not exists open_mark numeric(14,4);
