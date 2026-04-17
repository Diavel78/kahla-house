"""APScheduler jobs.

All jobs coalesce + use a single-instance lock so slow runs don't pile up.
Every job catches broad exceptions and notifies ops via Telegram — the
scheduler itself must never crash.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.blocking import BlockingScheduler

from alerts import telegram
from config import config
from scrapers import draftkings, fanduel, polymarket
from signals import divergence
from storage import supabase_client as db

log = logging.getLogger(__name__)


def _safe(name: str, fn, *args, **kwargs) -> None:
    try:
        fn(*args, **kwargs)
    except Exception as e:
        log.exception("%s failed: %s", name, e)
        telegram.notify_ops(f"{name} failed: {e}")


def job_poll_polymarket() -> None:
    _safe("poly_poll", polymarket.poll_once)


def job_scrape_books() -> None:
    for sport in config.sports_enabled:
        _safe(f"dk_scrape:{sport}", draftkings.scrape, sport)
        _safe(f"fd_scrape:{sport}", fanduel.scrape, sport)


def job_scan_signals() -> None:
    try:
        emitted = divergence.scan_all()
    except Exception as e:
        log.exception("divergence.scan_all failed: %s", e)
        telegram.notify_ops(f"scan_signals failed: {e}")
        return

    if not emitted:
        return

    # Fetch freshly-inserted rows (we need their ids) + their markets, then fan out.
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=2)
    rows = db.list_open_signals_since(cutoff)
    markets_by_id: dict[str, dict] = {}
    for r in rows:
        mid = r["market_id"]
        if mid not in markets_by_id:
            # look up once; list_active_markets is already cached at DB level
            for m in db.list_active_markets():
                if m["id"] == mid:
                    markets_by_id[mid] = m
                    break
        market = markets_by_id.get(mid)
        if not market:
            continue
        _safe("telegram.fan_out", telegram.fan_out, r, market)


def build_scheduler() -> BlockingScheduler:
    sched = BlockingScheduler(
        job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 30},
        timezone="UTC",
    )
    sched.add_job(
        job_poll_polymarket,
        "interval",
        seconds=config.poly_poll_interval,
        id="poll_polymarket",
        next_run_time=datetime.now(timezone.utc),
    )
    sched.add_job(
        job_scrape_books,
        "interval",
        seconds=config.book_scrape_interval,
        id="scrape_books",
        next_run_time=datetime.now(timezone.utc) + timedelta(seconds=5),
    )
    sched.add_job(
        job_scan_signals,
        "interval",
        seconds=config.signal_scan_interval,
        id="scan_signals",
        next_run_time=datetime.now(timezone.utc) + timedelta(seconds=10),
    )
    return sched
