from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name, str(default)).strip().lower()
    if value not in {"true", "false", "1", "0"}:
        raise ValueError(f"{name} must be true or false")
    return value in {"true", "1"}


def database_path() -> Path:
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        return Path(os.environ.get("DB_PATH", "data/bot.db"))
    if not url.startswith("sqlite:///"):
        raise ValueError("DATABASE_URL must be a SQLite file URL (sqlite:///...)")
    path = url.removeprefix("sqlite:///")
    if not path or path == ":memory:" or "?" in path or "#" in path:
        raise ValueError("DATABASE_URL must specify a persistent SQLite file")
    return Path(path)


def publishing_enabled() -> bool:
    # Read at the last possible moment. Invalid/absent values fail closed.
    return os.environ.get("AUTO_PUBLISH", "false").strip().lower() in {"true", "1"}


def enforce_dry_run():
    # Staging entry points cannot opt in, even with malformed or overridden values.
    os.environ["AUTO_PUBLISH"] = "false"


@dataclass(frozen=True)
class Settings:
    gemini_api_key: str = field(default_factory=lambda: os.getenv("GEMINI_API_KEY", ""), repr=False)
    gemini_model: str = field(default_factory=lambda: os.getenv("GEMINI_MODEL") or "gemini-flash-latest")
    groq_api_key: str = field(default_factory=lambda: os.getenv("GROQ_API_KEY", ""), repr=False)
    groq_model: str = field(default_factory=lambda: os.getenv("GROQ_MODEL") or "llama-3.3-70b-versatile")
    admin_token: str = field(default_factory=lambda: os.getenv("ADMIN_TOKEN", ""), repr=False)
    allow_unauthenticated_admin: bool = field(default_factory=lambda: env_bool("ALLOW_UNAUTHENTICATED_ADMIN", False))
    host: str = field(default_factory=lambda: os.getenv("HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: int(os.getenv("PORT", "8080")))
    collect_enabled: bool = field(default_factory=lambda: env_bool("COLLECT_ENABLED", True))
    collection_interval: int = field(default_factory=lambda: int(os.getenv("COLLECTION_INTERVAL_SEC", "900")))
    scheduler_interval: int = field(default_factory=lambda: int(os.getenv("SCHEDULER_INTERVAL_SEC", "60")))
    market_symbols: tuple[str, ...] = field(default_factory=lambda: tuple(
        x.strip().upper() for x in os.getenv("MARKET_SYMBOLS", "BTCUSDT,ETHUSDT").split(",") if x.strip()))
    min_score: float = field(default_factory=lambda: float(os.getenv("EVENT_MIN_SCORE", "40")))
    max_event_age: int = field(default_factory=lambda: int(os.getenv("EVENT_MAX_AGE_SEC", "86400")))
    max_chars: int = field(default_factory=lambda: int(os.getenv("WRITER_MAX_CHARS", "650")))
    prompt_path: Path = field(default_factory=lambda: Path(os.getenv("WRITER_PROMPT_PATH", "prompts/writer.txt")))
    drafts_per_cycle: int = field(default_factory=lambda: int(os.getenv("MAX_DRAFTS_PER_CYCLE", "3")))
    drafts_per_day: int = field(default_factory=lambda: int(os.getenv("MAX_DRAFTS_PER_DAY", "10")))
    ai_requests_per_day: int = field(default_factory=lambda: int(os.getenv("MAX_AI_REQUESTS_PER_DAY", "40")))
    event_max_attempts: int = field(default_factory=lambda: int(os.getenv("EVENT_MAX_ATTEMPTS", "3")))
    http_timeout: float = field(default_factory=lambda: float(os.getenv("HTTP_TIMEOUT_SEC", "30")))
    http_attempts: int = field(default_factory=lambda: int(os.getenv("HTTP_MAX_ATTEMPTS", "3")))
    readiness_max_age: int = field(default_factory=lambda: int(os.getenv("READINESS_MAX_AGE_SEC", "3600")))
    media_root: Path = field(default_factory=lambda: Path(os.getenv("MEDIA_ROOT", "data/media")))

    def __post_init__(self):
        for name in ("collection_interval", "scheduler_interval", "max_event_age", "drafts_per_cycle",
                     "drafts_per_day", "ai_requests_per_day", "event_max_attempts", "http_timeout", "http_attempts", "readiness_max_age"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        if not 180 <= self.max_chars <= 2000:
            raise ValueError("WRITER_MAX_CHARS must be between 180 and 2000")
        if not 0 <= self.min_score <= 100:
            raise ValueError("EVENT_MIN_SCORE must be between 0 and 100")
        if not 1 <= self.port <= 65535:
            raise ValueError("PORT is invalid")
        if self.host not in {"127.0.0.1", "localhost", "::1"} and not self.admin_token and not self.allow_unauthenticated_admin:
            raise ValueError("ADMIN_TOKEN is required when HOST is not loopback")
