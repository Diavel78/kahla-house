# Kahla Scanner — Plan Review

Review of the build spec, written after scaffolding the repo. Grouped by
severity: what could sink the project, what will hurt within the first month,
and what's nice-to-fix.

---

## 1. Edge definition — the core theory has a flaw

The spec treats **DK/FD devig'd mid** as "public fair value" and **Polymarket
mid** as the "sharp" number. That framing works if Polymarket is actually the
sharpest of the three. **It often isn't.**

- Polymarket US post-July-2025 has thin books on many non-headline games.
  On a $200-depth MLB moneyline, a single $50 ticket moves the mid 2–3%. That
  alone can synthesize a "3% edge" where none exists.
- DK and FD are correlated — they copy each other and Pinnacle within seconds.
  Averaging them doesn't give you two independent estimates; it gives you one
  estimate with a lower variance label.
- On flagship markets (NFL primetime, NBA finals), the true sharp price is
  Pinnacle / Circa / CRIS. Polymarket trails those by 30–90 seconds.

**Fix before M4 fires any real alert:**

1. Add a **no-vig Pinnacle reference** even if it's just pulled from an
   aggregator. Three-way consensus (DK, FD, PIN) beats two-way every time.
2. Require `min_liquidity_usd` to be depth **at the signal price within 2
   cents**, not total market volume. The spec hints at this but the schema
   doesn't enforce it. My scaffold computes it this way.
3. **Skip signals where Poly mid has moved more than 3% in the last 60
   seconds** — that's a stale book about to tighten, not an edge.
4. Track **time-weighted average** of public prob over the last 5–10 minutes,
   not just the single latest snapshot. One flash ML quote on DK during a
   goal/TD drives false positives.

Without these, the first week will produce loud, wrong alerts and you'll
learn to ignore the bot. That's the real failure mode.

---

## 2. Event matching is the biggest hidden timesink

The spec gives it half a section. In practice it's ~30% of the work.

What the spec has right:
- Team aliases table.
- ±30 min event_start tolerance.
- Unmatched fallback to manual review.

What it's missing:
- **Prop / sub-market matching.** Polymarket's "Will the Chiefs win?" is one
  market; DK has an ML, a spread, and a total for the same game. The scaffold
  links at the *event* level, which is right for ML, but if you expand to
  spreads you'll need a market-type mapping layer.
- **Multi-leg / combined markets on Poly.** Markets like "Chiefs win by 7+"
  don't map to any single DK offer. The matcher should skip these, not try
  to force a link.
- **Timezones.** Poly sometimes returns `start_time` as UTC, sometimes as
  the event's local time with no offset. Normalize to UTC at ingest and log
  a warning if `tzinfo` is None.
- **Series vs individual games.** UFC fight cards, CBB tournaments. A Poly
  market may be "Jones beats Miocic at UFC 309" while DK's event is "UFC 309
  Main Card". Plan for this before turning on MMA.

**Concrete recommendation:** build a `/admin/matches` page in the dashboard
from day 1 (not M9). You'll be reviewing unmatched markets weekly during
season rollover.

---

## 3. Scraping DK/FD is more fragile than the spec implies

"Monthly maintenance" in Section 11 is optimistic.

- DK rotates event-group IDs roughly every pro season (NFL gets a new group
  each August, CBB each November). Hard-coded IDs in `SPORT_EVENT_GROUPS`
  will silently break. Add a **scraper-health heartbeat**: if no new
  snapshots for sport X in N minutes, notify ops.
- DK and FD both fingerprint on TLS / JA3 and sometimes 403 behind plain
  `httpx`. You'll hit this on FD first. Plan to drop in `curl_cffi` or
  Playwright for one or both. The scaffold uses plain `httpx`; budget a half
  day for this.
- "Rotate User-Agent" is cargo-culting — it doesn't help against modern
  WAFs and may increase suspicion if the UA doesn't match other fingerprints.
  Better to pick one modern desktop UA and stick to it.

**Concrete additions to the schema:**

```sql
create table scraper_health (
  source text primary key,           -- 'dk','fd','poly'
  last_success timestamptz,
  consecutive_failures int default 0,
  last_error text
);
```

and a 5-minute heartbeat job that alerts ops at `consecutive_failures >= 3`.

---

## 4. Schema issues to fix before first write

Reviewing the spec's SQL against what the code needs:

1. **`book_snapshots.implied_prob` comment is misleading.** Spec says "no-vig
   not assumed" but doesn't say what IS stored. I kept it as raw implied
   prob (with vig) in the scaffold; devig happens in `signals/divergence.py`.
   The schema comment needs to match.
2. **Missing `sport` on `signals`.** Telegram fan-out filters by sport but
   has to join `markets` to get it. Denormalizing sport onto `signals` (and
   `book_snapshots`, and `poly_ticks`) makes the hot-path queries cheaper
   and lets the dashboard filter without joins.
3. **`poly_ticks.outcome` is free text.** The divergence engine looks up
   `outcome='HOME'`, but a real Poly scraper stores team names or "YES"/"NO".
   Either (a) normalize to `'HOME'`/`'AWAY'` at insert time (brittle), or (b)
   add an `outcome_side` column with an enum and keep `outcome` as the raw
   label. I'd pick (b).
4. **Retention.** Ticks are append-only and will balloon. Supabase free tier
   caps you at 500 MB. Add a nightly job that deletes `poly_ticks` older
   than 90 days (or move to S3 / a cold table).
5. **`status` columns are free text.** Consider Postgres enums or a CHECK
   constraint to prevent typos (`'settled'` vs `'resolved'`).

---

## 5. Operational gaps

- **No kill switch.** If the divergence engine starts flooding, there's no
  way to silence the bot without SSHing to the VPS. Add a `scanner_state`
  table with a single row `{paused: bool}` and a `/pause` bot command that
  Rob can hit from his phone.
- **No replay mode.** When alerts are wrong, you'll want to reconstruct the
  exact state at signal time. The append-only ticks help, but add a
  `signals.input_snapshot jsonb` column that captures the DK/FD/Poly values
  used to compute edge. Makes debugging 10× faster.
- **`alerts_log.delivery_status` is insert-once.** If Telegram returns 429
  rate-limit and the retry succeeds, the row still says `'failed'`. Either
  allow updates or add a `retries` counter.
- **APScheduler blocking in-process is fine for 1 sport, risky at 6.** Once
  you enable NFL + CBB + MLB concurrently, the DK scrape for one sport
  blocks the others for 1–2 seconds each. Move scrapers to
  `ThreadPoolExecutor` inside the job (same process, just parallel HTTP).
  Scaffold leaves this as a single sequential loop.
- **No structured logging.** `logging.basicConfig` is fine for journalctl
  but you'll want `json`-format logs once you deploy, so you can grep by
  `sport`, `market_id`, `signal_id`. Low-effort to add; do it before M4.

---

## 6. Stack choices — mostly good, one swap

Agree with:
- Python 3.11 (matches existing kahla-house codebase).
- Supabase (free tier + Realtime = dashboard for free).
- Telegram over SMS (richer formatting, free, push-reliable).
- `systemd` on a VPS (simpler than Docker for one service).

I'd swap:
- **APScheduler in-process → separate asyncio event loop per scraper** once
  you hit multi-sport. APScheduler's blocking scheduler makes concurrent I/O
  awkward. For v1 it's fine; flag it at the scaffold level so future-you
  knows. I kept APScheduler for the scaffold.

Don't swap (common temptations to resist):
- **Don't reach for Kafka / Redis / Celery.** For this data volume,
  Postgres + APScheduler + one VPS is correct. Only add queuing if you
  outgrow one VPS.
- **Don't use SQLAlchemy.** The schema is small and stable; raw
  `supabase-py` table builder is less typing than defining ORM models.

---

## 7. Dashboard integration — one thing to be careful about

The spec says "Supabase JS client reads live from signals". To keep
`subscribers.telegram_chat_id` private, the schema I wrote enables RLS on
all tables and grants `anon` SELECT only on non-PII tables. The anon key is
safe to ship to the browser under that policy. **Double-check the anon
policies in the Supabase dashboard after running the SQL** — RLS failures
are silent and will leak data.

Also: `/markets` should be gated behind the existing kahla-house auth. The
approval-gated user system already exists in `app.py` — reuse
`@firebase_auth_required` + a role check. Don't invent a second auth.

---

## 8. Milestone sequencing — one swap suggested

The spec orders M4 (Telegram single-user) before M5 (multi-user). I'd
collapse these. The multi-user logic is 30 extra lines and saves you from
rewriting the fan-out once you add the second subscriber. The scaffold
treats them as one milestone (both done at code level).

Also: **add an M0 — backtest harness on historical Poly data.** Before
M1, replay 30 days of Poly ticks + book snapshots from wherever you can
source them (even just your own Poly account history) and tune
`MIN_EDGE_PCT_GLOBAL` against outcomes. Right now 2.5% is a guess; if
empirics say 4% is the real threshold, you save yourself weeks of noisy
alerts.

---

## 9. Small things

- `.env.example` is in the spec but `FD_USER_AGENT` is listed and then
  never really used because FD blocks plain `httpx`. Mark it TODO or use
  Playwright.
- The `scan_all` divergence loop queries every active market every 30s.
  At NFL-only scale this is ~16 markets, trivially cheap. At 6-sport scale
  it's thousands — add an index-driven filter (`where updated_at > ...`)
  once that hurts.
- The build spec's `compute_divergence` pseudocode uses
  `abs(edge) * 100 < MIN_EDGE_PCT_GLOBAL` — correct, but the real signal
  has **direction**. Fade-side matters. The scaffold's implementation
  picks fade_side based on `edge > 0` before the abs, which the spec
  elides.

---

## 10. Summary — what I'd change before M1 starts

1. Add Pinnacle (or aggregator-sourced Pinnacle) as a third reference.
   Two-way devig of two correlated US books is the weakest part of the
   theory.
2. Schema: add `sport` to `signals`, add `scraper_health`, add
   `input_snapshot` to `signals`, add retention job. Consider enum types.
3. Build scraper-health heartbeats + ops alerts before building more
   scrapers. You want to know *fast* when DK changes an endpoint.
4. Combine M4 + M5. Skip to multi-user immediately.
5. Add M0: backtest harness on 30 days of historical data to tune
   thresholds before alerting anyone.
6. Plan admin/matches review UI for week 1, not M9.

The overall architecture (VPS + Supabase + Telegram + APScheduler) is
**right for this scope**. Most risk is in the two hardest problems the
spec under-weights: **event matching** and **the theory of edge itself**.
