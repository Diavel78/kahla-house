-- THE SHEET ITSELF (Aug 31 2026 — user, finding Temple@PSU enrolled on
-- polymarket.us/rewards while the machine said "not enrolled": "if it's
-- on the damn sheet... why isn't it bet"). The rewards PAGE lists every
-- program's market slugs; the API catalog only answers symbols= for
-- markets we've already touched. _reward_schedule_sync fetches the page
-- every ~10 min — this table keeps the slugs it used to discard.
create table if not exists rent_list_slugs (
  slug        text primary key,
  first_seen  timestamptz not null default now(),
  last_seen   timestamptz not null default now()
);
create index if not exists rent_list_slugs_seen_idx on rent_list_slugs (last_seen desc);
