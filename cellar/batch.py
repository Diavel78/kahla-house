"""THE CELLAR — the batch lane (Phase 1 of §6).

Spec: docs/cellar-migration-spec.md §6 Phase 1.

The ~20 scheduled GitHub Actions workflows -- daily ingests, model computes,
graders, housekeeping -- expressed as jobs this process runs itself.

WHY THIS IS THE FIRST THING TO MOVE. None of it touches money; every job
reads a public API and writes Supabase. So the blast radius is a stale table,
not a bad order. And it fixes a failure that already cost six days: ESPN began
403'ing GitHub's runner IPs on Aug 4 2026 and the spine step -- marked
`continue-on-error` -- painted a green checkmark every minute while creating
nothing. A residential IP does not have that problem, and `work`-based
heartbeats make a silent zero visible even if it does.

SCHEDULES ARE IN ARIZONA TIME, not UTC. The workflows are written in UTC
(`50 10 * * *`) which is 03:50 AZ; stated here as the local wall-clock the
user actually reasons about. Every "today" in this codebase is an AZ day.

Each job runs as a SUBPROCESS, not an import. Three reasons: the kahla-scanner
subproject has its own sys.path expectations, a segfaulting or hung model
compute cannot take the daemon down with it, and the invocation stays
byte-identical to what the workflow runs -- so behavior does not silently
diverge from the thing it replaces.

⚠️ CUTOVER REQUIRES DISABLING THE WORKFLOWS. THE LEASE DOES NOT COVER THIS.

The `cellar_lease` protects the cellar from VERCEL, because both sides claim.
GitHub Actions claims nothing -- it just fires on its cron and runs. So the
moment `batch` is enabled here, every job runs TWICE a day: once on the runner,
once in the cellar.

For these particular jobs that is wasteful rather than dangerous (the ingests
are idempotent upserts and the computes overwrite a snapshot), which is exactly
why Phase 1 is the safe place to learn this. It would NOT be harmless for a
money lane. So the ordering is:

    1. Enable `batch` in CELLAR_LANES, watch it for a day alongside Actions.
    2. Confirm via `--batch-status` that the cellar is doing the work.
    3. THEN disable the schedules in .github/workflows/*.yml (comment the
       `schedule:` block; keep `workflow_dispatch` so they stay usable as the
       standby -- spec §8 item 7: disable, never delete).

Verify at any time with:  python -m cellar --batch-status
"""
from __future__ import annotations

import logging
import os
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .util import retrying

log = logging.getLogger("cellar.batch")

AZ = ZoneInfo("America/Phoenix")

# kahla-scanner runs with its own directory as cwd (the workflows set
# working-directory: kahla-scanner).
SCANNER_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "kahla-scanner")


@dataclass(frozen=True)
class Job:
    name: str
    argv: list[str]                 # after `python -m`
    hour: int                       # AZ wall clock
    minute: int = 0
    weekday: int | None = None      # 0=Mon .. 6=Sun; None = daily
    timeout_s: int = 1800
    note: str = ""
    # Jobs that must run in order (power ratings: ingest THEN compute).
    then: tuple[tuple[str, ...], ...] = field(default_factory=tuple)
    # An importable module this job REQUIRES beyond requirements.txt. Missing
    # => the job is BLOCKED: skipped, never stamped as run, and reported as
    # such by --batch-status. Failing loudly once a week at 3am on a box
    # nobody is watching is exactly the outcome this migration exists to end.
    needs: str | None = None


# The scheduled roster. UTC in the workflow -> AZ here (UTC-7, no DST).
#   09:00 UTC = 02:00 AZ    10:00 UTC = 03:00 AZ    12:00 UTC = 05:00 AZ
JOBS: tuple[Job, ...] = (
    # -- housekeeping ------------------------------------------------------
    Job("snapshot_cleanup", ["scripts.cleanup_snapshots", "--days", "15"],
        hour=2, note="prune snapshot tables (keeps the DB at steady state)"),

    # -- daily ingests (spines) --------------------------------------------
    Job("nhl_goalies", ["scripts.ingest_nhl_goalies", "--delta", "--commit"],
        hour=3, minute=0, note="NHL goalie boxscores"),
    Job("nhl_shots", ["scripts.ingest_nhl_shots", "--delta", "--commit"],
        hour=3, minute=10, note="NHL unblocked shot events (xG spine)"),
    Job("mlb_pitchers", ["scripts.ingest_mlb_pitchers", "--delta", "--commit"],
        hour=3, minute=30, note="MLB pitcher game lines (Diamond/Whiff/Outs spine)"),
    Job("mlb_batters", ["scripts.ingest_mlb_batters", "--delta", "--commit"],
        hour=3, minute=40, note="MLB batter game lines"),
    # --platoon is REQUIRED: the daily delta writes BOTH the xwOBA and the
    # platoon spine in one pass. Without it the platoon table silently stops
    # updating while everything looks healthy.
    Job("savant_xwoba",
        ["scripts.ingest_savant_xwoba", "--delta", "--platoon", "--commit"],
        hour=3, minute=45, note="Statcast xwOBA + platoon spine"),
    # Off-season this is a one-query no-op (game_results has no fresh
    # football finals to fetch summaries for); in-season it fills the prop
    # spine the morning after each slate.
    Job("football_players",
        ["scripts.ingest_football_players", "--delta", "--commit"],
        hour=3, minute=48, note="NFL + NCAAF player game lines (prop spine + QB adjust)",
        # NCAAF joined Sep 13 2026 (backfilled 2025-08 →) — the QB
        # adjustment needs college passers too. Same script, second sport.
        then=(("scripts.ingest_football_players", "--delta", "--sport", "NCAAF",
               "--commit"),)),

    # -- PICK SHEETS — the humans' betting page, built HERE with the box's
    # model (Rob, Oct 3 2026: "the sheets need to run the current damn
    # model… UP TO DATE"). scripts/box_sheets.py prices every game through
    # app._gridiron_proj and writes football_sheets in THIS database, which
    # is the one the site reads — no Actions, no cloud project, no sync.
    # The Monday FULL build (`sheets_build`) lives BELOW the model computes:
    # the lane runs the first due job in list order, so on a catch-up morning
    # it must come after power_ratings/football_qb/cfbd/nfl_market. The three
    # daily refreshes re-price into data_blob.friday (what the site prefers).
    # ⚠ A friday-mode run targets week_key_default() = THIS Monday, so on a
    # Monday before the full build it finds zero rows and is a no-op — the
    # new week's sheets come only from sheets_build.
    # THE LINE IS PINNACLE'S — ONE PULL A DAY, 07:00 AZ, EVERY SPORT (Rob,
    # Oct 6 2026: "7 am pull pinnacle, daily, for all sports, then DK for
    # the updates"; Oct 5 was 3-4 pulls a day for football). `--pin` pulls
    # the sport's parlay-api slate (3 credits a sport, 90-min cache,
    # 900-of-1,000 monthly hard stop in app._pin_slate) and the sheet
    # prices against it; a run WITHOUT `--pin` still reads the cached slate
    # but uses it only while under app._SHEET_PIN_FRESH_S (3h) — so the
    # 07:00 runs are the Pinnacle sheets and the later refreshes re-price
    # off the ESPN book line (tagged by provider on the page). Budget
    # (31-day month): 07:00 football 2 sports + NHL + NBA = 4 pulls × 3 =
    # 12/day ≈ 372; Monday's 05:00 build pulls football too (+6/wk ≈ 26).
    # ≈ 400/mo of the 900 stop. Credits used: /api/cellar/health → parlay.
    Job("sheets_refresh_am", ["scripts.box_sheets", "--mode", "friday", "--commit", "--pin"],
        hour=7, minute=0, timeout_s=1500,
        note="football pick sheets: 7am re-price (box model, THE Pinnacle pull)"),
    Job("sheets_refresh_md", ["scripts.box_sheets", "--mode", "friday", "--commit"],
        hour=11, minute=0, timeout_s=1500,
        note="football pick sheets: midday re-price (box model, book line)"),
    Job("sheets_refresh_pm", ["scripts.box_sheets", "--mode", "friday", "--commit"],
        hour=15, minute=30, timeout_s=1500,
        note="football pick sheets: pre-slate re-price (box model, book line)"),
    Job("sheets_refresh_ev", ["scripts.box_sheets", "--mode", "friday", "--commit"],
        hour=19, minute=0, timeout_s=1500,
        note="football pick sheets: evening re-price (box model, book line)"),
    # Hockey: Crease IQ v2 off the box's own goalie/shot spines (3:00/3:10
    # above), DailyFaceoff goalies + ESPN lines fetched live. Same four
    # clocks the Actions workflow kept (07:00/11:00/14:00/15:30 AZ). The
    # 07:00 run SPENDS the day's Pinnacle pull; the others price off the
    # ESPN book line once the slate is past the 3h freshness gate.
    Job("nhl_sheet_07", ["scripts.nhl_sheet_data", "--days", "1", "--commit", "--pin"],
        hour=7, minute=0, timeout_s=1500, note="NHL pick sheet (box, THE Pinnacle pull)"),
    Job("nhl_sheet_11", ["scripts.nhl_sheet_data", "--days", "1", "--commit"],
        hour=11, minute=5, timeout_s=1500, note="NHL pick sheet (box, book line)"),
    Job("nhl_sheet_14", ["scripts.nhl_sheet_data", "--days", "1", "--commit"],
        hour=14, minute=0, timeout_s=1500, note="NHL pick sheet (box, book line)"),
    Job("nhl_sheet_1530", ["scripts.nhl_sheet_data", "--days", "1", "--commit"],
        hour=15, minute=35, timeout_s=1500, note="NHL pick sheet (box, pre-slate, book line)"),
    # Basketball: Hoops IQ (pace × efficiency + who is OUT), ESPN history
    # cached in ~/.kahla/nba_cache (closed seasons never rescanned), ESPN
    # lines + injuries live. The 07:00 run is the Pinnacle sheet (same pull
    # clock as the rest); injury news lands late — the 15:45 run is the one
    # closest to tip that still lands before the early slate, book line.
    Job("nba_sheet_07", ["scripts.nba_sheet_data", "--days", "1", "--commit", "--pin"],
        hour=7, minute=5, timeout_s=1500, note="NBA pick sheet (box, THE Pinnacle pull)"),
    Job("nba_sheet_12", ["scripts.nba_sheet_data", "--days", "1", "--commit"],
        hour=12, minute=10, timeout_s=1500, note="NBA pick sheet (box, book line)"),
    Job("nba_sheet_1545", ["scripts.nba_sheet_data", "--days", "1", "--commit"],
        hour=15, minute=45, timeout_s=1500, note="NBA pick sheet (box, pre-tip injuries, book line)"),
    # Yesterday's sheet record, every sport, stamped to exec_probe_runs
    # (kind=sheet_grade) so a sandbox can read it through the site.
    Job("sheets_grade", ["scripts.grade_football_sheets", "--sport", "NFL",
                         "--sport", "NCAAF", "--sport", "NHL",
                         "--sport", "NBA", "--days-back", "2", "--stamp", "--write"],
        hour=9, minute=0, timeout_s=600, note="pick-sheet record (yesterday + day before)"),
    # Intraday ✅/❌ ride the `grader` lane every ~5 min (cellar/lanes.py
    # lane_grader) — this job owns only the stamped daily record.

    # -- daily model computes (order matters: after their spines) ----------
    # RETIRED Oct 6 2026 with the Pick Bot and the machine (Rob: "models and
    # lines are all that matters" — the SHEETS' models): diamond_iq (MLB ML
    # snapshot for the opener lane), football_props + whiff_iq (prop-lane
    # models), ufc_model (Fight IQ), tune_prime_window (the Pick Bot's
    # weekly tuner). Their scripts stay; re-add a Job line to revive one.
    # The data SPINES above stay — cheap, idempotent, and the fuel for any
    # future model.
    # After the football_players delta (3:48) — the NFL props model state
    # (per-player decayed mean/SD + league priors, the gate-1 mirror).
    # The betting wire lands separately, from real captured prop shapes.
    Job("power_ratings", ["scripts.ingest_results", "--days", "2"],
        hour=4, minute=0, note="ESPN finals -> game_results, then ratings",
        then=(("scripts.compute_power_ratings",),)),
    # THE QB ADJUSTMENT (Sep 13 2026, docs/football-qb-adjust-spec.md):
    # after the ratings, same window/half-life, so the baseline weights
    # the games the rating did. Reads ESPN depth charts + rosters; writes
    # football_qb_adj, which app._gridiron_proj applies. Stale >8d ⇒ the
    # pricer applies 0, so a failure here is a quiet fallback, not a lie.
    Job("football_qb", ["scripts.compute_football_qb", "--commit"],
        hour=4, minute=10, timeout_s=900,
        note="football QB adjustment (who throws the next game vs who threw the rated games)"),
    # CFBD RATINGS (Sep 17 2026): SP+/FPI/Elo/SRS → cfbd_ratings, the college
    # PRE-MARKET line app._cfbd_consensus centers on before a book line exists.
    # Needs CFBD_API_KEY in the box .env; without it the script exits 2 and the
    # pricer falls back to the results solve exactly as before.
    Job("cfbd_ratings", ["scripts.ingest_cfbd_ratings", "--commit"],
        hour=4, minute=20, timeout_s=600,
        note="CollegeFootballData SP+/FPI/Elo/SRS mirror (college pre-market line)"),
    # NFL pre-market inputs (Sep 17 2026): nfelo pts_vs_avg → cfbd_ratings
    # (source nfelo), nflverse Vegas spread/total → book_lines (book nflverse).
    Job("nfl_market", ["scripts.ingest_nfl_market", "--commit"],
        hour=4, minute=25, timeout_s=600,
        note="nfelo rating + nflverse Vegas lines (NFL pre-market)"),
    # FOOTBALL PICK SHEETS — Monday FULL build at 05:00 AZ (Oct 5 2026,
    # Rob at 08:56 Monday: "Why no new sheets??" — it sat at 17:00, the old
    # Actions Routine's slot, so the page showed last week all day). Sunday's
    # finals land at 04:00 (power_ratings), QB/CFBD/nfelo by 04:25; 05:00 is
    # the first clock where the model is current. Listed after those jobs on
    # purpose (first-due-in-list-order). --days 8 reaches next Monday's MNF.
    Job("sheets_build", ["scripts.box_sheets", "--mode", "monday", "--commit", "--pin"],
        hour=5, minute=0, weekday=0, timeout_s=1500,
        note="football pick sheets: Monday full build (box model)"),

    # -- weekly ------------------------------------------------------------
    # NOT --delta: this script has no such flag. Its delta mode is
    # `--events --fighters --commit --limit 80` (see ufc-stats-ingest.yml).
    # Needs Playwright + Chromium — UFCStats fronts requests with a JS
    # proof-of-work challenge that the fetcher solves in headless Chromium.
    # Deliberately absent from requirements.txt (scanner-poll pip-installs
    # that file every minute), so install it on the box explicitly:
    #     pip install playwright && python -m playwright install chromium
    Job("ufc_stats",
        ["scripts.ingest_ufc_stats", "--events", "--fighters", "--commit",
         "--limit", "80"],
        hour=3, weekday=0, timeout_s=3600, needs="playwright",
        note="UFCStats delta (Mon) — needs playwright"),
)

JOBS_BY_NAME = {j.name: j for j in JOBS}


# ---------------------------------------------------------------------------
# Scheduling
# ---------------------------------------------------------------------------

def due_at(job: Job, now_az: datetime) -> datetime:
    """The most recent scheduled firing time at or before `now_az`."""
    today = now_az.replace(hour=job.hour, minute=job.minute,
                           second=0, microsecond=0)
    if job.weekday is None:
        return today if today <= now_az else today - timedelta(days=1)
    # Weekly: walk back to the most recent matching weekday.
    cand = today
    while cand.weekday() != job.weekday or cand > now_az:
        cand -= timedelta(days=1)
        cand = cand.replace(hour=job.hour, minute=job.minute,
                            second=0, microsecond=0)
    return cand


def is_due(job: Job, last_ok: datetime | None, now_az: datetime) -> bool:
    """Due iff the last successful run predates the most recent firing time.

    Catch-up by construction: a box that was asleep at 03:30 runs the job when
    it wakes, rather than skipping the day. That is the behavior a laptop
    needs and a cron daemon does not give you for free.
    """
    fire = due_at(job, now_az)
    if last_ok is None:
        return True
    return last_ok < fire


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

def _run_one(argv: list[str], timeout_s: int) -> tuple[bool, str]:
    import sys
    cmd = [sys.executable, "-m", *argv]
    log.info("batch: $ %s  (cwd=kahla-scanner)", " ".join(cmd[2:]))
    try:
        p = subprocess.run(cmd, cwd=SCANNER_DIR, capture_output=True,
                           text=True, timeout=timeout_s)
    except subprocess.TimeoutExpired as e:
        # keep what the job printed before it died — a bare TIMEOUT hid a
        # whole day of football refreshes (Oct 6 2026) with no clue which
        # game or read ate the 1,500s.
        def _txt(x):
            return x.decode(errors="replace") if isinstance(x, bytes) else (x or "")
        tail = _txt(e.stdout)[-1200:] + (("\nSTDERR:\n" + _txt(e.stderr)[-1200:]) if e.stderr else "")
        return False, (f"TIMEOUT after {timeout_s}s\n" + tail).strip()
    tail = (p.stdout or "")[-1500:] + (("\nSTDERR:\n" + p.stderr[-1500:]) if p.stderr else "")
    return p.returncode == 0, tail.strip()


def run_job(job: Job) -> tuple[bool, int, str]:
    """Run a job and its `then` chain. Returns (ok, steps_run, log tail).

    A failed step ABORTS the chain -- computing power ratings on top of a
    failed finals ingest would silently produce a snapshot from stale data,
    which is worse than no snapshot.
    """
    steps = 0
    ok, out = _run_one(list(job.argv), job.timeout_s)
    steps += 1
    if not ok:
        return False, steps, out
    for follow in job.then:
        ok2, out2 = _run_one(list(follow), job.timeout_s)
        steps += 1
        out = (out + "\n--- " + follow[0] + " ---\n" + out2)[-3000:]
        if not ok2:
            return False, steps, out
    return True, steps, out


# ---------------------------------------------------------------------------
# The lane
# ---------------------------------------------------------------------------

# Blocked jobs warn on a cooldown, not every tick. A job that is due and
# permanently blocked (ufc_stats without playwright) is due FOREVER, so a
# naive warning re-fires every 60s and drowns the log -- which is how a real
# warning gets missed later. Observed immediately on the first live run.
_BLOCKED_WARNED: dict[str, float] = {}
_BLOCKED_WARN_EVERY_S = 6 * 3600


def _warn_blocked(job_name: str, needs: str) -> None:
    last = _BLOCKED_WARNED.get(job_name, 0.0)
    now = time.time()
    if now - last < _BLOCKED_WARN_EVERY_S:
        log.debug("batch: %s still blocked (missing %r)", job_name, needs)
        return
    _BLOCKED_WARNED[job_name] = now
    log.warning("batch: %s BLOCKED — missing %r. Install it, or leave this "
                "job on GitHub Actions. (silenced for 6h; --batch-status "
                "always shows it)", job_name, needs)


# FAILURE BACKOFF. A job stays due until it SUCCEEDS, so without this a
# persistently broken job re-runs every 60s forever -- observed live the first
# night: ufc_stats failed four times in four minutes and would have run all
# night. Heavy jobs (a browser scrape, a model compute) make that genuinely
# costly, and the log noise buries whatever else is wrong.
#
# In-memory on purpose: a daemon restart clears it, because a restart is a
# human saying "I fixed something, try again now."
_FAIL_BACKOFF: dict[str, tuple[int, float]] = {}     # job -> (fails, next_ts)
_BACKOFF_BASE_S = 300
_BACKOFF_MAX_S = 6 * 3600


def _backoff_ok(job_name: str) -> bool:
    """False => still serving a penalty from a recent failure."""
    entry = _FAIL_BACKOFF.get(job_name)
    if not entry:
        return True
    fails, next_ts = entry
    if time.time() >= next_ts:
        return True
    log.debug("batch: %s backing off (%d consecutive failures, %ds left)",
              job_name, fails, int(next_ts - time.time()))
    return False


def _note_result(job_name: str, ok: bool) -> None:
    if ok:
        _FAIL_BACKOFF.pop(job_name, None)
        return
    fails = _FAIL_BACKOFF.get(job_name, (0, 0.0))[0] + 1
    delay = min(_BACKOFF_MAX_S, _BACKOFF_BASE_S * (2 ** (fails - 1)))
    _FAIL_BACKOFF[job_name] = (fails, time.time() + delay)
    log.warning("batch: %s failed (%d in a row) — next attempt in %dm",
                job_name, fails, delay // 60)


def _have(module: str) -> bool:
    """Is an optional dependency importable on this box?"""
    import importlib.util
    try:
        return importlib.util.find_spec(module) is not None
    except Exception:
        return False


def _last_ok(sb, job_name: str) -> datetime | None:
    """Last successful run, read from cellar_ticks.

    State lives in the DB, not on local disk, so moving the box or rebuilding
    it does not re-run a week of ingests -- and so the standby can see what
    the cellar has already done.
    """
    try:
        rows = retrying(
            lambda: (sb.table("cellar_ticks")
                     .select("started_at")
                     .eq("lane", f"batch:{job_name}").eq("ok", True)
                     .order("started_at", desc=True).limit(1)
                     .execute().data), what=f"last_ok {job_name}") or []
    except Exception as e:
        log.warning("batch: last_ok lookup failed for %s: %s", job_name, e)
        # Fail CLOSED: unknown history means "don't run", so a DB blip can't
        # trigger a stampede of re-ingests.
        return datetime.now(AZ)
    if not rows:
        return None
    ts = rows[0]["started_at"].replace("Z", "+00:00")
    return datetime.fromisoformat(ts).astimezone(AZ)


def lane_batch(ctx) -> int:
    """Run whatever is due. One job per tick -- these are heavy, and a queue
    that drains one-per-minute still catches up in minutes."""
    sb = ctx.sb
    now_az = datetime.now(AZ)

    for job in JOBS:
        if not is_due(job, _last_ok(sb, job.name), now_az):
            continue

        if not _backoff_ok(job.name):
            continue

        if job.needs and not _have(job.needs):
            # BLOCKED, not failed. Skip to the next due job without stamping a
            # run, so --batch-status keeps showing it as outstanding instead of
            # quietly pretending it happened.
            _warn_blocked(job.name, job.needs)
            continue

        if ctx.dry_run:
            log.info("batch: DRY-RUN, would run %s (%s)", job.name, job.note)
            return 0

        t0 = time.time()
        ok, steps, out = run_job(job)
        ms = int((time.time() - t0) * 1000)
        try:
            sb.table("cellar_ticks").insert({
                "lane": f"batch:{job.name}", "owner": "cellar",
                "duration_ms": ms, "ok": ok, "claimed": True,
                "work": steps if ok else 0,
                "detail": {"job": job.name, "note": job.note, "steps": steps},
                "error": None if ok else out[-2000:],
            }).execute()
        except Exception as e:
            log.warning("batch: tick write failed: %s", e)

        log.info("batch: %s %s in %dms", job.name, "ok" if ok else "FAILED", ms)
        _note_result(job.name, ok)
        if not ok:
            log.error("batch: %s output tail:\n%s", job.name, out[-800:])
        return steps if ok else 0

    return 0


def _last_ok_map(sb) -> dict[str, datetime]:
    """Last successful run for EVERY batch job in ONE query.

    The per-job lookup is fine inside the lane (it runs on the box, once a
    minute, and stops at the first due job), but `--batch-status` called it 12
    times = 12 internet round trips before printing a single row. PostgREST has
    no GROUP BY, so pull the recent ok-ticks once and reduce in Python.
    """
    try:
        rows = retrying(
            lambda: (sb.table("cellar_ticks")
                     .select("lane,started_at")
                     .like("lane", "batch:%").eq("ok", True)
                     .order("started_at", desc=True).limit(500)
                     .execute().data), what="last_ok bulk") or []
    except Exception as e:
        log.warning("batch: bulk last_ok lookup failed: %s", e)
        return {}
    out: dict[str, datetime] = {}
    for r in rows:                      # already newest-first; keep the first
        name = (r.get("lane") or "")[len("batch:"):]
        if name and name not in out:
            ts = r["started_at"].replace("Z", "+00:00")
            try:
                out[name] = datetime.fromisoformat(ts).astimezone(AZ)
            except Exception:
                pass
    return out


def status(sb) -> list[dict]:
    """What's due, what ran, when. For `python -m cellar --batch-status`."""
    now_az = datetime.now(AZ)
    seen = _last_ok_map(sb)
    out = []
    for job in JOBS:
        last = seen.get(job.name)
        blocked = bool(job.needs and not _have(job.needs))
        out.append({
            "job": job.name,
            "blocked": blocked,
            "sched": (f"{'Mon ' if job.weekday == 0 else 'daily '}"
                      f"{job.hour:02d}:{job.minute:02d} AZ"),
            "last_ok": last.strftime("%m-%d %H:%M") if last else "never",
            "due": is_due(job, last, now_az) and not blocked,
            "note": job.note,
        })
    return out
