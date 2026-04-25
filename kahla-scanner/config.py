"""Typed config loaded from environment.

Trimmed to only what `scrapers/odds_api.py` and `scripts/cleanup_snapshots.py`
need after the divergence/Brier pipeline retirement: Supabase creds,
sports list, log level.
"""
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

    # Sports — workflow default is set in .github/workflows/scanner-poll.yml
    sports_enabled: list[str] = field(
        default_factory=lambda: _env_list("SPORTS_ENABLED", ["NFL", "NBA", "MLB", "NHL", "CBB", "NCAAF", "UFC"])
    )

    # Logging
    log_level: str = field(default_factory=lambda: _env("LOG_LEVEL", "INFO"))


config = Config()
