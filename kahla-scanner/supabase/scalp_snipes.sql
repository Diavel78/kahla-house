-- THE SELL SNIPER's ledger (Sep 6 2026): one row per socket-driven amend.
create table if not exists scalp_snipes (
  id bigserial primary key,
  at timestamptz not null default now(),
  slug text not null,
  from_c numeric, to_c numeric,
  oid text, ok boolean, note text,
  bid_c numeric, ask_c numeric
);
create index if not exists scalp_snipes_at_idx on scalp_snipes (at desc);
create index if not exists scalp_snipes_slug_idx on scalp_snipes (slug, at desc);
