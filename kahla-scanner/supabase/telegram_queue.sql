-- Telegram 10-minute batch queue (Aug 2 2026 — user: "Telegram messages
-- every 10 minutes max… If something happened in that 10 minutes, it puts
-- them all together? If nothing happened, then don't send one").
-- app.py:_send_fill_telegram inserts (sent_at null = queued; urgent 🚨
-- sends write sent_at immediately so the window counts them);
-- _tg_flush (paperlog tick) delivers one 📬 summary max per 10 min and
-- stamps sent_at; delivered rows >3d old are pruned by the flush.
-- APPLIED to the live DB Aug 2 2026 via run_sql.sh.
create table if not exists telegram_queue (
    id bigserial primary key,
    created_at timestamptz default now(),
    text text not null,
    sent_at timestamptz
);
create index if not exists telegram_queue_unsent_idx
    on telegram_queue (id) where sent_at is null;
