import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp.test_utils import TestClient, TestServer

from src.app import create_app
from src.pipeline import Pipeline
from src.settings import Settings
from src.storage import Store
from tests.test_collectors_engine import market_fixture
from src.collectors.binance_market import BinanceMarketCollector


@pytest.mark.asyncio
async def test_pipeline_end_to_end_dry_run_persistence_and_daily_cap(db, tmp_path):
    prompt = tmp_path / "writer.txt"
    prompt.write_text("口语化，谨慎观察", encoding="utf-8")
    settings = Settings(gemini_api_key="fake", prompt_path=prompt, drafts_per_day=1)
    pipeline = Pipeline(settings, Store(), AsyncMock())
    pipeline.news.collect = AsyncMock(return_value=[])
    pipeline.market.collect = AsyncMock(return_value=[BinanceMarketCollector.normalize(market_fixture(), "BTC", "USDT")])
    pipeline.provider.providers = [object()]
    pipeline.provider.generate = AsyncMock(return_value=json.dumps({"opening": "这份快照值得留作对照", "commentary": "后续可以继续观察，避免过早下结论。"}))
    pipeline.provider.last_provider = "mock-gemini"
    with patch("services.binance.aiohttp.ClientSession") as external:
        first = await pipeline.collect_once()
        second = await pipeline.collect_once()
        assert first["drafts"] == 1 and second["drafts"] == 0
        external.assert_not_called()
    assert len(Store().queue()) == 1 and db.list_history()[0]["status"] == "prepared"
    db.init_db()  # process restart preserves draft and opening history
    assert Store().recent_openings() == ["这份快照值得留作对照"]


@pytest.mark.asyncio
async def test_backend_queue_events_logs_health_and_xss(db):
    Store().enqueue('<script>alert("xss")</script> $BTC')
    app = create_app(Settings(collect_enabled=False), start_workers=False)
    async with TestClient(TestServer(app)) as client:
        health = await (await client.get("/health")).json()
        assert health["status"] == "ok" and health["auto_publish"] is False
        page = await client.get("/")
        text = await page.text()
        assert "&lt;script&gt;" in text and '<script>alert' not in text
        assert "DRY RUN" in text
        assert len(await (await client.get("/api/queue")).json()) == 1
        assert await (await client.get("/api/events")).json() == []
        assert await (await client.get("/api/logs")).json() == []
        assert (await client.get("/api/queue?limit=999999")).status == 400
        assert (await client.post("/api/publish")).status == 404
        assert (await client.post("/api/queue")).status == 405


@pytest.mark.asyncio
async def test_admin_auth_and_health_without_token(db):
    app = create_app(Settings(admin_token="private-admin-secret"), start_workers=False)
    async with TestClient(TestServer(app)) as client:
        assert (await client.get("/")).status == 401
        assert (await client.get("/api/queue?token=private-admin-secret")).status == 401
        assert (await client.get("/health")).status == 200
        assert (await client.get("/", headers={"Authorization": "Bearer private-admin-secret"})).status == 200


@pytest.mark.asyncio
async def test_health_reports_scheduler_fault_separately_from_database(db):
    import time
    from src.app import WORKERS

    app = create_app(Settings(collect_enabled=False), start_workers=False)
    app[WORKERS]["scheduler"] = {"state": "error", "tick_at": time.time(), "error": "RuntimeError"}
    async with TestClient(TestServer(app)) as client:
        response = await client.get("/health")
        assert response.status == 503
        payload = await response.json()
        assert payload["database"] is True and payload["status"] == "unhealthy"


@pytest.mark.asyncio
async def test_workers_start_without_telegram_buffer_or_ai_keys(db, monkeypatch):
    for key in ("TELEGRAM_BOT_TOKEN", "BUFFER_ACCESS_TOKEN", "GEMINI_API_KEY", "GROQ_API_KEY", "BINANCE_SQUARE_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    app = create_app(Settings(collect_enabled=False))
    async with TestClient(TestServer(app)) as client:
        response = await client.get("/health")
        assert response.status == 200
        assert (await response.json())["workers"]["scheduler"]["state"] == "idle"


def test_sqlite_url_and_fail_closed_config(monkeypatch):
    from src.settings import database_path, publishing_enabled

    monkeypatch.setenv("DATABASE_URL", "sqlite:///data/square.db")
    assert database_path() == Path("data/square.db")
    monkeypatch.setenv("DATABASE_URL", "postgresql://secret@host/db")
    with pytest.raises(ValueError):
        database_path()
    monkeypatch.setenv("AUTO_PUBLISH", "typo")
    assert not publishing_enabled()
    monkeypatch.setenv("HOST", "0.0.0.0")
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    monkeypatch.delenv("ALLOW_UNAUTHENTICATED_ADMIN", raising=False)
    with pytest.raises(ValueError):
        Settings()
