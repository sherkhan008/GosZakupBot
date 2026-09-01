"""Application configuration loaded from environment variables (.env)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_env() -> None:
    env_path = BASE_DIR / ".env"
    load_dotenv(dotenv_path=env_path if env_path.exists() else None)


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _str_env(name: str, default: str = "") -> str:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip()


def _float_env(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    goszakup_api_token: str
    telegram_bot_token: str
    telegram_chat_id: str

    check_interval_seconds: int
    min_hours_remaining: int
    max_hours_remaining: int

    app_timezone_name: str
    database_path: Path

    bootstrap_lookback_days: int
    sync_overlap_minutes: int

    # Primary, authoritative detection mechanism (see services/monitor.py
    # module docstring) -- a full re-scan of every keyword with NO
    # lastUpdateDate filter. Deliberately has no "lookback days" setting:
    # lastUpdateDate must never gate eligibility.
    discovery_scan_interval_minutes: int

    min_amount_kzt: float

    log_level: str

    keywords_path: Path = field(default_factory=lambda: BASE_DIR / "config" / "keywords.yaml")
    goszakup_graphql_url: str = "https://ows.goszakup.gov.kz/v3/graphql"
    telegram_api_base: str = "https://api.telegram.org"

    @property
    def app_timezone(self) -> ZoneInfo:
        return ZoneInfo(self.app_timezone_name)


def load_settings() -> Settings:
    _load_env()

    database_path = Path(_str_env("DATABASE_PATH", "data/tenders.db"))
    if not database_path.is_absolute():
        database_path = BASE_DIR / database_path

    return Settings(
        goszakup_api_token=_str_env("GOSZAKUP_API_TOKEN"),
        telegram_bot_token=_str_env("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=_str_env("TELEGRAM_CHAT_ID"),
        check_interval_seconds=_int_env("CHECK_INTERVAL_SECONDS", 300),
        min_hours_remaining=_int_env("MIN_HOURS_REMAINING", 5),
        max_hours_remaining=_int_env("MAX_HOURS_REMAINING", 72),
        app_timezone_name=_str_env("APP_TIMEZONE", "Asia/Qyzylorda"),
        database_path=database_path,
        bootstrap_lookback_days=_int_env("BOOTSTRAP_LOOKBACK_DAYS", 90),
        sync_overlap_minutes=_int_env("SYNC_OVERLAP_MINUTES", 10),
        discovery_scan_interval_minutes=_int_env("DISCOVERY_SCAN_INTERVAL_MINUTES", 60),
        min_amount_kzt=_float_env("MIN_AMOUNT_KZT", 100000),
        log_level=_str_env("LOG_LEVEL", "INFO"),
    )
