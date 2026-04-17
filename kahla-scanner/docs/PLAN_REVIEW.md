# Kahla Scanner — Plan Review (revised)

Review of the build spec after a conversation that corrected several of my
initial priors. Earlier draft over-weighted "Polymarket is thin / noisy"
concerns; live order books (MLB moneylines, Challenger-tier tennis, etc.)
show $13K–$90K at top of book even on niche markets. Those concerns are
withdrawn. What remains is below.

---

## 1. The real open question — is Poly actually sharper?

Strong structural prior that it is:
- DK/FD aggressively limit winning players. Pros get flagged and cut off
  within weeks of profitability.
- The migration to prediction markets (Poly, Kalshi) is well-documented. The
  sharpest US action increasingly lives there.
- Post-July-2025 Poly has deep books even on obscure markets — depth is
  not a concern.

But "sharps bet there" ≠ "mid-price predicts outcomes better than DK/FD
devig'd consensus". Those are different claims. The whole scanner rests on
the second one. **Measure it before trusting it.** See M0 below.

---

## 2. M0 — the calibration gate (added to the plan)

Before any alert fires, run in log-only mode for 2–3 weeks. Scrapers ingest
Poly ticks + DK/FD snapshots; no signals, no Telegram. Then:

For every settled market, pull each source's HOME-side implied probability
at T-24h, T-6h, T-1h, T-0 before event start. Compute Brier score:
`mean((prob_home − actual_home)²)` per source per checkpoint. Lowest wins.

Three outcomes:
- **Poly Brier < DK/FD Brier** by a meaningful margin at T-1h or T-0 →
  thesis confirmed. Tune edge threshold from the residual distribution,
  flip `SCANNER_MODE=live`.
- **Tie** → Poly and DK/FD are parallel sharp markets. Divergence signal is
  noise around zero. Pivot the thesis: cross-venue arb, per-book lag,
  news-latency windows.
- **Poly loses** → unlikely, but signal means fade Poly / take DK/FD. Only
  plausible on specific market types where Poly has a structural
  mispricing (politics-adjacent novelty, low-info props).

Implementation in repo: `analytics/brier.py`, `analytics/outcomes.py`,
`SCANNER_MODE=log_only` default. README has the workflow.

---

## 3. Event matching is still the biggest execution risk

The spec gives it half a section. In practice it's ~30% of the work.

Under-scoped areas:
- **Prop / sub-market matching.** Event-level linkage works for moneylines.
  Spreads need a market-type mapping layer.
- **Multi-leg / combined markets on Poly.** "Chiefs win by 7+" doesn't map
  to any single DK offer. The matcher should detect and skip these, not
  force a link.
- **Timezone normalization at ingest.** Poly occasionally returns local time
  with no tzinfo. Normalize to UTC at insert and warn if `tzinfo is None`.
- **Series vs single-game.** UFC cards, CBB tournaments. A Poly market may
  be "Jones beats Miocic at UFC 309" while DK's event is the whole card.

**Recommendation:** build the `/admin/matches` review UI in week 1, not M9.
You'll use it constantly during season rollover.

---

## 4. Scraping fragility

"Monthly maintenance" in spec Section 11 is optimistic.

- DK rotates event-group IDs each season (NFL each August, CBB each
  November). Hard-coded IDs silently break. Add a `scraper_health` table +
  5-minute heartbeat that alerts ops after 3 consecutive failures.
- DK and FD fingerprint on TLS/JA3. Plain `httpx` will 403 on FD sooner or
  later. Budget a half day to drop in `curl_cffi` or Playwright.
- "Rotate User-Agent" is cargo-culting. Pick one modern desktop UA and
  keep the rest of the fingerprint consistent.

**Schema addition:**

```sql
create table scraper_health (
  source text primary key,              -- 'dk','fd','poly'
  last_success timestamptz,
  consecutive_failures int default 0,
  last_error text
);
```

Not in the scaffold yet; add when M1/M2 land.

---

## 5. Schema fixes worth doing early

1. **Denormalize `sport` onto `signals`** (and optionally `book_snapshots`,
   `poly_ticks`). Fan-out + dashboard filter by sport constantly; saves a
   join on every query.
2. **Add `input_snapshot jsonb` to `signals`.** Capture the exact DK/FD/Poly
   values used to compute edge at signal time. Makes post-mortem debugging
   10× faster when alerts are wrong.
3. **Fix `book_snapshots.implied_prob` docstring.** Spec says "no-vig not
   assumed" without specifying what IS stored. Scaffold stores raw (with
   vig); devig happens in the signal engine. Schema comment should match.
4. **Retention job.** Ticks are append-only. Supabase free tier caps at 500
   MB. Add a nightly job deleting `poly_ticks` + `book_snapshots` older
   than 90 days, or move to cold storage.
5. **`poly_ticks.outcome` is free text.** Divergence engine expects 'HOME'
   but a real scraper stores team names or 'YES'/'NO'. Add an
   `outcome_side` enum column and keep `outcome` as the raw label.
6. Consider Postgres enums or CHECK constraints on `status` columns to
   prevent typo drift (`settled` vs `resolved`).

---

## 6. Operational gaps

- **No kill switch.** Add a `scanner_state` table + `/pause` Telegram
  command so Rob can silence the bot from his phone without SSHing.
- **`alerts_log.delivery_status` is insert-once.** 429 retries that succeed
  still show as `failed`. Either allow updates or add a retries counter.
- **APScheduler blocking is fine for 1 sport, risky at 6.** Once
  multi-sport is enabled, one scraper's slow tick blocks the others. Move
  scrapers into a `ThreadPoolExecutor` in the job body. Scaffold leaves
  this sequential.
- **Structured logging.** `logging.basicConfig` is fine for journalctl but
  JSON logs let you filter by `sport` / `market_id` / `signal_id` once the
  volume grows. Low-effort add; do it before M4.

---

## 7. Stack — agree mostly

Agree with: Python 3.11, Supabase, Telegram, systemd on a cheap VPS,
APScheduler in-process for now. Don't reach for Kafka/Redis/Celery at this
scale — Postgres + one VPS is correct. Don't add SQLAlchemy — schema is
small and stable; raw `supabase-py` is less code than ORM models.

---

## 8. Dashboard auth — reuse, don't reinvent

`/markets` on thekahlahouse.com should reuse the existing
`@firebase_auth_required` + role check in `app.py`. Don't introduce a second
auth layer.

The scaffold's `schema.sql` enables RLS and grants `anon` SELECT only on
non-PII tables (markets, book_snapshots, poly_ticks, signals, outcomes).
`subscribers` and `alerts_log` stay service-key-only. Verify anon policies
in the Supabase dashboard after running the SQL — RLS mistakes are silent.

---

## 9. Milestone sequencing — revised

Add **M0** (log-only + Brier calibration) as the gate before M4 fires.
Collapse M4 + M5 (multi-user fan-out is 30 extra lines, not worth a
separate milestone). Actual order:

```
M0 (2–3 weeks, parallel with M1/M2/M6 build)
M1 Polymarket poller + Supabase writes
M2 DK scraper + matcher (NFL first)
M6 FD scraper
M3 Divergence engine  (already built)
M0 decision gate — does Poly Brier < DK/FD Brier?
  → yes: M4/M5 flip live
  → no:  re-scope thesis before building more
M7 Dashboard /markets route
M8 Expand sports
M9 Per-market detail page
M10 Kalshi
```

---

## 10. What changed from the first draft

Withdrawn (based on live order-book evidence):
- "$50 ticket moves Poly mid 2–3%" — wrong, depth is solid.
- "Thin niche markets distort edge" — wrong, niche markets still have $10K+
  at top of book.
- "Require depth-within-2¢ filter" — not load-bearing; keep `min_liquidity`
  as a cheap sanity check but it's not protecting against anything.
- "Skip when Poly mid moved >3% in 60s" — stale-book heuristic that isn't
  the real failure mode.

Still valid:
- DK/FD are correlated — averaging them is one sharpness estimate, not two.
  A Pinnacle reference (via aggregator) would help if acquirable.
- Event matching is the biggest under-scoped problem.
- Scraper health + kill switch are ops basics missing from the spec.
- Schema denormalization / input_snapshot / retention should ship early.

New:
- **M0 backtest gate** is the whole game. Everything else is
  execution.
