-- BOOK LINES MEMORY (Sep 5 2026, Rob): every sportsbook line we see for a
-- game is REMEMBERED, keyed by our own market row, so the executor's
-- centering survives the books pulling a look-ahead line on game day
-- ("we record a line from DraftKings Friday; Saturday that line's gonna
-- disappear, but we still have to use that as our centering").
-- line is in rung units: spread = HOME line (negative when home favored),
-- total = the total. One row per (market, market_type, book), upserted.
create table if not exists book_lines (
  market_id   uuid not null,
  market_type text not null,
  book        text not null,
  line        numeric not null,
  seen_at     timestamptz not null default now(),
  primary key (market_id, market_type, book)
);
create index if not exists book_lines_seen_idx on book_lines (seen_at desc);
notify pgrst, 'reload schema';
