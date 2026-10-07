import logging
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import AsyncMock, patch

import pytest
import yaml
from aiohttp import web
from aiohttp.test_utils import TestServer

from services import binance
from src.collectors.crypto_news import extract_symbols
from src.http import BudgetExceeded, HTTPClient, HTTPFailure
from src.logging_config import SecretFilter
from src.scheduler import Scheduler
from src.storage import Store


def test_atomic_dedup_under_concurrent_enqueues(db):
    with ThreadPoolExecutor(max_workers=4) as pool:
        result = list(pool.map(lambda i: Store().enqueue("Concurrent identical draft"), range(8)))
    assert sum(x is not None for x in result) == 1
    assert len(Store().queue()) == 1


def test_budget_is_atomic_persistent_and_has_no_secret_logs(db, monkeypatch):
    store = Store()
    store.reserve_ai_request(2)
    db.init_db()
    Store().reserve_ai_request(2)
    with pytest.raises(BudgetExceeded):
        store.reserve_ai_request(2)
    monkeypatch.setenv("GEMINI_API_KEY", "full-private-api-key")
    db.log_history(kind="test", service="ai", status="failed", text_preview="full-private-api-key",
                   error="full-private-api-key https://upload.test/x?secret=abc")
    log = db.list_history()[0]
    assert "full-private-api-key" not in str(log) and "secret=abc" not in str(log)
    record = logging.LogRecord("test", logging.ERROR, "", 0, "key=%s", ("full-private-api-key",), None)
    SecretFilter().filter(record)
    assert "full-private-api-key" not in record.getMessage()


@pytest.mark.asyncio
async def test_http_retries_count_against_daily_model_budget(db):
    count = 0

    async def fail(request):
        nonlocal count
        count += 1
        return web.Response(status=503)

    app = web.Application()
    app.router.add_post("/ai", fail)
    async with TestServer(app) as server:
        with pytest.raises(BudgetExceeded):
            await HTTPClient(attempts=5, backoff=0).request("POST", str(server.make_url("/ai")),
                before_attempt=lambda: Store().reserve_ai_request(2))
    assert count == 2  # Third attempt was stopped before opening a network connection.


@pytest.mark.asyncio
async def test_official_text_image_upload_contract_on_mock_server(monkeypatch):
    monkeypatch.setenv("AUTO_PUBLISH", "true")
    monkeypatch.setattr(binance, "BINANCE_API_KEY", "mock-square-key")
    monkeypatch.setattr(binance, "_HEADERS", {"X-Square-OpenAPI-Key": "mock-square-key", "clienttype": "binanceSkill"})
    captured = []

    async def presign(request):
        assert (await request.json())["imageName"] == "photo.png"
        assert request.headers["X-Square-OpenAPI-Key"] == "mock-square-key"
        return web.json_response({"code": "000000", "data": {"presignedUrl": str(server.make_url("/upload")), "fileTicket": "ticket"}})

    async def upload(request):
        assert "X-Square-OpenAPI-Key" not in request.headers
        assert request.headers["Content-Type"] == "image/png"
        assert await request.read() == b"fixture-image"
        return web.Response()

    async def status(request):
        assert await request.json() == {"fileTicket": "ticket"}
        return web.json_response({"code": "000000", "data": {"status": 1, "imageUrl": "https://mock.test/processed.png"}})

    async def publish(request):
        captured.append(await request.json())
        return web.json_response({"code": "000000", "data": {"id": "mock-id", "shareLink": "https://mock.test/post"}})

    app = web.Application()
    app.router.add_post("/v2/image/presignedUrl", presign)
    app.router.add_put("/upload", upload)
    app.router.add_post("/v2/image/imageStatus", status)
    app.router.add_post("/v1/content/add", publish)
    async with TestServer(app) as server:
        monkeypatch.setattr(binance, "BINANCE_API_V1", str(server.make_url("/v1")))
        monkeypatch.setattr(binance, "BINANCE_API_V2", str(server.make_url("/v2")))
        assert (await binance.publish_text("fixture text")).ok
        assert (await binance.publish_image_post("fixture image", [(b"fixture-image", "photo.png")])).ok
    assert captured == [{"contentType": 1, "bodyTextOnly": "fixture text"},
                        {"contentType": 1, "bodyTextOnly": "fixture image", "imageList": ["https://mock.test/processed.png"]}]


@pytest.mark.asyncio
async def test_transport_uncertainty_never_retries_real_post(db, monkeypatch):
    monkeypatch.setenv("AUTO_PUBLISH", "true")
    monkeypatch.setattr(binance, "BINANCE_API_KEY", "mock-key")
    with patch("services.binance._post_json", new_callable=AsyncMock) as http:
        http.side_effect = HTTPFailure(transport=True)
        result = await binance.publish_text("fixture")
        assert result.kind == "uncertain" and http.await_count == 1
    pid = Store().enqueue("unknown result")
    publisher = AsyncMock()
    publisher.publish.return_value = result
    scheduler = Scheduler(Store(), publisher)
    assert await scheduler.publish_one(db.get_binance_post(pid)) == "review"
    await scheduler.tick()
    assert publisher.publish.await_count == 1


@pytest.mark.asyncio
async def test_max_attempts_dead_letter_and_pause_prevents_send(db, monkeypatch):
    monkeypatch.setenv("AUTO_PUBLISH", "true")
    pid = Store().enqueue("exhausted post")
    with db.get_conn() as conn:
        conn.execute("UPDATE binance_queue SET attempt_count=5 WHERE id=?", (pid,))
    publisher = AsyncMock()
    publisher.publish.return_value = binance.BinanceResult(ok=False, kind="transient", error="failure")
    scheduler = Scheduler(Store(), publisher)
    db.set_binance_paused(True)
    await scheduler.tick()
    publisher.publish.assert_not_awaited()
    db.set_binance_paused(False)
    assert await scheduler.publish_one(db.get_binance_post(pid)) == "dead"
    assert db.get_binance_post(pid)["attempt_count"] == 6


def test_symbol_alias_avoids_common_words():
    assert extract_symbols("follow the link and move a ton of data") == []
    assert extract_symbols("Chainlink and TON $BTC") == ["BTC", "LINK", "TON"]


def test_compose_dry_run_volume_port_health_and_licenses():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    compose = yaml.safe_load((root / "docker-compose.yml").read_text())
    service = compose["services"]["square-ai"]
    assert service["environment"]["AUTO_PUBLISH"] == "false"
    assert service["ports"][0].startswith("127.0.0.1:")
    assert service["volumes"] == ["bot-data:/app/data"]
    assert "/health" in str(service["healthcheck"])
    assert "aiogram" not in (root / "requirements.txt").read_text()
    assert "Copyright (c) 2026 SMOService" in (root / "LICENSE").read_text()
    assert 'CMD ["python", "-m", "src.app"]' in (root / "Dockerfile").read_text()
