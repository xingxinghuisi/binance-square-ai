import json
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp.test_utils import TestClient, TestServer

from src.app import create_app
from src.cli import main, print_collection
from src.collectors.binance_market import BinanceMarketCollector
from src.pipeline import Pipeline
from src.settings import Settings
from src.storage import Store
from tests.test_collectors_engine import market_fixture


@pytest.mark.asyncio
async def test_status_reads_quality_api_auth_and_preview_xss(db, monkeypatch):
    pid = Store().enqueue('<script>alert("draft")</script>')
    monkeypatch.setenv("AUTO_PUBLISH", "true")
    app = create_app(Settings(gemini_api_key="private-key", admin_token="admin-secret"), start_workers=False)
    async with TestClient(TestServer(app)) as client:
        auth = {"Authorization": "Bearer admin-secret"}
        assert (await client.get("/api/status/providers")).status == 401
        with patch("src.http.HTTPClient.request", side_effect=AssertionError("status GET made an external request")):
            providers = await (await client.get("/api/status/providers", headers=auth)).json()
            sources = await (await client.get("/api/status/sources", headers=auth)).json()
        assert providers["gemini"]["configured"] and providers["gemini"]["reachable"] is None
        assert "private-key" not in json.dumps(providers)
        assert all(s["status"] == "unknown" for s in sources)
        route = f"/api/drafts/{pid}/quality"
        assert (await client.post(route, json={"quality_status": "good"})).status == 401
        assert (await client.post(route, data={"quality_status": "good"}, headers=auth)).status == 400
        assert (await client.post(route, json={"quality_status": "bad"}, headers={**auth, "Origin": "https://evil.test"})).status == 403
        assert (await client.post(route, json={"quality_status": []}, headers=auth)).status == 400
        assert (await client.post(route, json={"quality_status": "unknown"}, headers=auth)).status == 400
        assert (await client.post("/api/drafts/999/quality", json={"quality_status": "good"}, headers=auth)).status == 404
        response = await client.post(route, json={"quality_status": "bad", "quality_note": "<script>note</script>"}, headers=auth)
        assert response.status == 200 and (await response.json())["quality_status"] == "bad"
        page = await (await client.get("/", headers=auth)).text()
        assert all(label in page for label in ("Event Type", "Symbol", "Score", "Source", "AI Provider", "Generated At"))
        assert "&lt;script&gt;note" in page and "<script>note" not in page
        assert not (await (await client.get("/health")).json())["auto_publish"]
        assert not Store().queue()[0]["published"]


def test_preview_rating_and_no_key_probe_cli_never_publish(db, capsys, monkeypatch):
    for name in ("GEMINI_API_KEY", "GROQ_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AUTO_PUBLISH", "true")
    pid = Store().enqueue("完整中文草稿 $BTC")
    with patch("services.binance.aiohttp.ClientSession") as publish:
        assert main(["preview", "--limit", "10"]) == 0
        assert "完整中文草稿 $BTC" in capsys.readouterr().out
        assert main(["rate-draft", str(pid), "bad", "--note", "开头太模板化"]) == 0
        assert Store().draft(pid)["quality_note"] == "开头太模板化"
        assert main(["test-ai"]) == 2
        assert "SKIP (not configured)" in capsys.readouterr().out
        publish.assert_not_called()
    with pytest.raises(SystemExit):
        main(["preview", "--limit", "0"])


@pytest.mark.asyncio
async def test_collect_stats_distinguish_rejection_dedup_and_source_error(db, capsys):
    settings = Settings(gemini_api_key="", groq_api_key="", market_symbols=("BTCUSDT",))
    pipeline = Pipeline(settings, Store(), AsyncMock())
    pipeline.news.collect = AsyncMock(return_value=[])
    pipeline.news.last_counts = {"CoinDesk": 0}
    pipeline.news.last_errors = [{"source": "CoinDesk", "error": "HTTP 403"}]
    snapshot = BinanceMarketCollector.normalize(market_fixture(), "BTC", "USDT")
    pipeline.market.collect = AsyncMock(return_value=[snapshot, snapshot, snapshot | {"timestamp": "2000-01-01T00:00:00Z"}])
    stats = await pipeline.collect_once()
    assert stats["events"] == {"received": 3, "accepted": 1, "deduplicated": 1, "rejected": 1}
    assert stats["ai"] == {"gemini_requests": 0, "groq_requests": 0, "drafts": 0}
    assert stats["market_symbols"] == {"BTCUSDT": "OK"}
    print_collection(stats)
    text = capsys.readouterr().out
    assert "CoinDesk: 0" in text and "ERROR [CoinDesk]: HTTP 403" in text and "BTCUSDT: OK" in text


@pytest.mark.asyncio
async def test_phase15_scheduler_is_locked_even_when_environment_enables_publish(db, monkeypatch):
    from src.scheduler import Scheduler
    pid = Store().enqueue("仍然仅预览")
    monkeypatch.setenv("AUTO_PUBLISH", "true")
    publisher = AsyncMock()
    scheduler = Scheduler(Store(), publisher, dry_run=True)
    assert await scheduler.publish_one(db.get_binance_post(pid)) == "dry_run"
    await scheduler.tick()
    publisher.publish.assert_not_called()
    assert Store().draft(pid)["attempt_count"] == 0
