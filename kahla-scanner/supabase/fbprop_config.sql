-- REMOTE CONTROL for the NFL props bet lane (built Aug 22 2026, the
-- hour before a 6-day box freeze): the box polls this row every tick,
-- so the lane can be activated, tuned, or killed VIA SQL while the box
-- cannot take a code update. config shape:
--   {"enabled": bool,            -- master switch (default false = dormant)
--    "contracts": int,           -- stake (default 1)
--    "max_bets": int,            -- slate cap (default 30)
--    "fams": [                   -- capture patterns, WRITTEN FROM REAL
--       {"fam": "receptions",    --   prop_snapshots captures, never blind
--        "pattern": "python regex with (?P<name>...) and (?P<line>...)"}
--    ]}
-- Activation flow: props list -> read question shapes off prop_snapshots
-- -> update this row via run_sql.sh -> lane live on the next tick.
create table if not exists fbprop_config (
  id         int primary key,
  config     jsonb not null,
  updated_at timestamptz not null default now()
);
insert into fbprop_config (id, config)
  values (1, '{"enabled": false, "contracts": 1, "max_bets": 30, "fams": []}')
  on conflict (id) do nothing;
