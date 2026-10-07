"""Тестовое окружение: env-заглушки ДО импорта config/db + чистая БД на каждый тест."""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test:token")
os.environ.setdefault("ALLOWED_USER_ID", "1")
os.environ.setdefault("BUFFER_ACCESS_TOKEN", "test-buffer-token")
os.environ.setdefault("DB_PATH", str(Path(__file__).parent / ".bootstrap.db"))
os.environ["AUTO_PUBLISH"] = "false"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

import db as db_module  # noqa: E402


@pytest.fixture
def db(tmp_path):
    """db-модуль, переключённый на свежую БД в tmp_path."""
    db_module.DB_PATH = tmp_path / "bot.db"
    db_module.init_db()
    return db_module


@pytest.fixture(autouse=True)
def prevent_unrequested_external_http(request, monkeypatch):
    # Even a developer's configured .env cannot make ordinary tests spend credits.
    if request.node.get_closest_marker("live") and os.getenv("RUN_LIVE_TESTS", "false").lower() == "true":
        return
    import aiohttp
    from urllib.parse import urlsplit
    original = aiohttp.ClientSession._request

    async def local_only(self, method, url, *args, **kwargs):
        if urlsplit(str(url)).hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise AssertionError("External HTTP denied in mock test mode")
        return await original(self, method, url, *args, **kwargs)
    monkeypatch.setattr(aiohttp.ClientSession, "_request", local_only)
