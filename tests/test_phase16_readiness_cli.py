import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
import yaml
from aiohttp.test_utils import TestClient, TestServer

from src.app import create_app
from src.cli import main
from src.diagnostics import staging_readiness
from src.settings import Settings
from src.storage import Store


def settings():
    return Settings(gemini_api_key="fake-private-key", groq_api_key="", admin_token="fake-admin-token")


def test_readiness_cached_critical_spot_rss_noncritical_and_no_secrets(db, monkeypatch):
    monkeypatch.setenv("AUTO_PUBLISH", "true")
    monkeypatch.setenv("BINANCE_SQUARE_API_KEY", "ignored-private-square-key")
    store = Store()
    with patch("src.http.HTTPClient.request", side_effect=AssertionError("readiness made HTTP")):
        assert not staging_readiness(settings(), store)["ready"]  # never probed
        store.record_status("source", "Binance Spot", success=True, items=2)
        store.record_status("source", "CoinDesk", success=False, error="HTTP 403", items=0)
        report = staging_readiness(settings(), store)
        assert report["ready"] and report["binance_spot"]["critical"]
        assert not report["auto_publish"] and report["dry_run_lock"]
        assert "CoinDesk: error (noncritical)" in report["warnings"]
        assert "ignored-private-square-key" not in json.dumps(report)
        assert "fake-private-key" not in json.dumps(report)
        store.record_status("source", "Binance Spot", success=False, error="DNS lookup failed", items=0)
        failed = staging_readiness(settings(), store)
        assert not failed["ready"] and failed["binance_spot"]["last_success"]
        assert failed["binance_spot"]["status"] == "error" and "binance_spot" in failed["blocking"]
    assert Store().preview() == [] and Store().events() == []


@pytest.mark.parametrize("age", [7200, -100])
def test_readiness_does_not_trust_stale_or_future_success(db, age):
    store = Store()
    checked = (datetime.now(timezone.utc) - timedelta(seconds=age)).isoformat()
    db.kv_set("diagnostic:source:Binance Spot", {"reachable": True, "items": 2, "checked_at": checked})
    result = staging_readiness(settings(), store)
    assert not result["ready"] and result["binance_spot"]["status"] == "stale"


def test_readiness_admin_model_and_database_fail_closed(db):
    store = Store()
    store.record_status("source", "Binance Spot", success=True, items=2)
    result = staging_readiness(Settings(gemini_api_key="", groq_api_key="", admin_token=""), store)
    assert not result["ready"] and {"ai_configured", "admin_token"} <= set(result["blocking"])
    with patch("db.get_conn", side_effect=OSError("private-key private-path")):
        result = staging_readiness(settings(), store)
    assert not result["database"] and not result["ready"]
    assert "private" not in json.dumps(result)


def test_readiness_quality_session_cli_no_network_and_preserve_active_writer(db, capsys, monkeypatch):
    for key in ("GEMINI_API_KEY", "GROQ_API_KEY", "ADMIN_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    store = Store()
    store.save_event("active", "active", {"type": "news", "source": "Decrypt", "symbol": "BTC"})
    assert store.claim_event("active")
    pid = store.enqueue("完整正文用于评分")
    store.rate_draft(pid, "bad", "开头太模板化")
    with patch("src.http.HTTPClient.request", side_effect=AssertionError("report made network")), \
         patch("services.binance.aiohttp.ClientSession") as publish:
        for command in ("readiness", "quality-report", "session-report"):
            monkeypatch.setenv("AUTO_PUBLISH", "true")
            expected = 1 if command == "readiness" else 0
            assert main([command, "--json"]) == expected
            report = json.loads(capsys.readouterr().out)
            assert report
        publish.assert_not_called()
    event = Store().events()[0]
    assert event["status"] == "writing" and event["attempts"] == 1
    assert not Store().draft(pid)["published"] and Store().draft(pid)["attempt_count"] == 0
    assert main(["readiness"]) == 1
    text = capsys.readouterr().out
    assert all(label in text for label in ("Database: OK", "Dry Run Lock: OK", "Gemini: not configured",
                                          "Square API Key: ignored/not required", "AUTO_PUBLISH: FALSE", "ready=false"))
    with patch("db.init_db", side_effect=OSError("private-key")):
        assert main(["readiness", "--json"]) == 1
    assert not json.loads(capsys.readouterr().out)["database"]
    db.init_db()  # Actual backend restart retains original recovery behavior.
    assert Store().events()[0]["status"] == "new"


@pytest.mark.asyncio
async def test_backend_environment_change_stays_locked_in_health_and_scheduler(db, monkeypatch):
    store = Store()
    pid = store.enqueue("误设 true 也不能发帖")
    with patch("src.app.BinanceSquarePublisher") as publisher:
        publisher.return_value.publish = AsyncMock(side_effect=AssertionError("real publish"))
        async with TestClient(TestServer(create_app(Settings(collect_enabled=False)))) as client:
            monkeypatch.setenv("AUTO_PUBLISH", "true")
            monkeypatch.setenv("BINANCE_SQUARE_API_KEY", "unused-fake-key")
            result = await (await client.get("/health")).json()
            assert result["auto_publish"] is False
            page = await (await client.get("/")).text()
            assert "DRY RUN" in page and "自动发布开启" not in page
            publisher.return_value.publish.assert_not_called()
    assert store.draft(pid)["attempt_count"] == 0


def test_compose_honors_staging_auth_without_weakening_dry_run():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    service = yaml.safe_load((root / "docker-compose.yml").read_text())["services"]["square-ai"]
    assert service["environment"]["AUTO_PUBLISH"] == "false"
    assert service["environment"]["ALLOW_UNAUTHENTICATED_ADMIN"] == "${ALLOW_UNAUTHENTICATED_ADMIN:-true}"
    assert "RUN_LIVE_TESTS=false" in (root / ".env.example").read_text()
