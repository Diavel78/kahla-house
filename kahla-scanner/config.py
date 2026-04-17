"""Typed config loaded from environment."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")


def _env(key: str, default: str | None = None, required: bool = False) -> str | None:
    val = os.getenv(key, default)
    if required and not val:
        raise RuntimeError(f"Missing required env var: {key}")
    return val


def _env_int(key: str, default: int) -> int:
    raw = os.getenv(key)
    return int(raw) if raw else default


def _env_float(key: str, default: float) -> float:
    raw = os.getenv(key)
    return float(raw) if raw else default


def _env_list(key: str, default: list[str]) -> list[str]:
    raw = os.getenv(key)
    if not raw:
        return default
    return [s.strip() for s in raw.split(",") if s.strip()]


@dataclass(frozen=True)
class Config:
    # Supabase
    supabase_url: str = field(default_factory=lambda: _env("SUPABASE_URL", required=True))
    supabase_service_key: str = field(
        default_factory=lambda: _env("SUPABASE_SERVICE_KEY", required=True)
    )

    # Polymarket
    poly_api_key_id: str | None = field(default_factory=lambda: _env("POLY_API_KEY_ID"))
    poly_api_secret: str | None = field(default_factory=lambda: _env("POLY_API_SECRET"))
    poly_api_passphrase: str | None = field(default_factory=lambda: _env("POLY_API_PASSPHRASE"))

    # Telegram
    telegram_bot_token: str | None = field(default_factory=lambda: _env("TELEGRAM_BOT_TOKEN"))
    telegram_ops_chat_id: str | None = field(default_factory=lambda: _env("TELEGRAM_OPS_CHAT_ID"))

    # Scrapers
    dk_user_agent: str = field(
        default_factory=lambda: _env(
            "DK_USER_AGENT",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        )
    )
    fd_user_agent: str = field(
        default_factory=lambda: _env(
            "FD_USER_AGENT",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        )
    )

    # Thresholds
    min_edge_pct_global: float = field(
        default_factory=lambda: _env_float("MIN_EDGE_PCT_GLOBAL", 2.5)
    )
    min_liquidity_global: float = field(
        default_factory=lambda: _env_float("MIN_LIQUIDITY_GLOBAL", 500.0)
    )
    dedup_window_min: int = field(default_factory=lambda: _env_int("DEDUP_WINDOW_MIN", 15))
    min_minutes_to_event: int = field(default_factory=lambda: _env_int("MIN_MINUTES_TO_EVENT", 30))

    # Scheduling
    poly_poll_interval: int = field(default_factory=lambda: _env_int("POLY_POLL_INTERVAL", 45))
    book_scrape_interval: int = field(
        default_factory=lambda: _env_int("BOOK_SCRAPE_INTERVAL", 180)
    )
    signal_scan_interval: int = field(
        default_factory=lambda: _env_int("SIGNAL_SCAN_INTERVAL", 30)
    )

    # Sports
    sports_enabled: list[str] = field(
        default_factory=lambda: _env_list("SPORTS_ENABLED", ["NFL"])
    )

    # Logging
    log_level: str = field(default_factory=lambda: _env("LOG_LEVEL", "INFO"))


config = Config()
