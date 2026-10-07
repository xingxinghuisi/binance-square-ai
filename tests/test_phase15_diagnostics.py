import json
from unittest.mock import AsyncMock, patch

import pytest

from src.diagnostics import (
    ReadOnlyHTTPClient, providers_status, sources_status, test_ai as probe_ai, test_sources as probe_sources,
)
from src.http import HTTPClient, HTTPFailure, Response
from src.settings import Settings
from src.storage import Store
from tests.test_collectors_engine import RSS, market_fixture


@pytest.mark.asyncio
async def test_independent_provider_probes_and_secret_free_persistence(db):
    settings = Settings(gemini_api_key="private-gemini", groq_api_key="private-groq")
    http = AsyncMock()
    http.json.side_effect = [HTTPFailure(401), {"choices": [{"message": {"content": '{"ok":true}'}}]}]
    result = await probe_ai(settings, Store(), http)
    assert http.json.await_count == 2  # Groq is tested even when Gemini fails
    assert result["gemini"]["reachable"] is False and result["gemini"]["last_error"] == "HTTP 401"
    assert result["groq"]["reachable"] is True and result["groq"]["last_success"]
    assert not Store().queue() and not Store().events()
    assert Store().ai_request_counts() == {"gemini": 0, "groq": 0}  # HTTP mock bypasses attempt callbacks
    assert "private-gemini" not in json.dumps(result) and "private-groq" not in json.dumps(result)
    db.init_db()
    assert providers_status(settings, Store()) == result
    changed = Settings(gemini_api_key="new-key", groq_api_key="private-groq", groq_model="new-model")
    assert providers_status(changed, Store())["gemini"]["reachable"] is None
    assert providers_status(changed, Store())["groq"]["reachable"] is None


@pytest.mark.asyncio
async def test_probe_without_keys_does_not_call_network(db):
    http = AsyncMock()
    state = await probe_ai(Settings(gemini_api_key="", groq_api_key=""), Store(), http)
    assert not any(s["configured"] for s in state.values())
    http.json.assert_not_called()


@pytest.mark.asyncio
async def test_sources_isolate_failures_and_report_counts_and_latency(db):
    http = AsyncMock()
    http.request.side_effect = [HTTPFailure(403), Response(200, RSS), Response(200, RSS), Response(200, RSS)]
    http.json.side_effect = [{"symbols": [{"symbol": "BTCUSDT", "baseAsset": "BTC", "quoteAsset": "USDT"}]},
                             [market_fixture()]]
    result = await probe_sources(Settings(market_symbols=("BTCUSDT",)), Store(), http)
    assert [s["source"] for s in result] == ["Binance Spot", "CoinDesk", "Cointelegraph", "Decrypt", "The Block"]
    assert result[1]["status"] == "error" and result[1]["last_error"] == "HTTP 403"
    assert all(s["items"] == 1 and s["last_success"] for s in result if s["status"] == "ok")
    assert all(s["latency_ms"] >= 0 for s in result)
    db.init_db()
    assert sources_status(Store()) == result
    assert not Store().events() and not Store().queue()


@pytest.mark.asyncio
@pytest.mark.parametrize("url", [
    "https://www.binance.com/bapi/composite/v1/public/pgc/open-api/content/add",
    "https://www.binance.com/bapi/composite/v2/public/pgc/open-api/image/presignedUrl",
    "https://api.binance.com/api/v3/order", "https://evil.test/rss",
])
async def test_phase15_http_denies_publish_and_unapproved_endpoints_before_network(url):
    with patch.object(HTTPClient, "request", new_callable=AsyncMock) as outbound:
        with pytest.raises(ValueError, match="endpoint denied"):
            await ReadOnlyHTTPClient().request("POST", url)
        outbound.assert_not_called()


@pytest.mark.asyncio
async def test_read_only_redirect_cannot_reach_square_or_forward_model_credentials():
    from src.collectors.crypto_news import RSS_FEEDS
    for method, url in [("GET", RSS_FEEDS["Decrypt"]),
                        ("POST", "https://api.groq.com/openai/v1/chat/completions")]:
        with patch.object(HTTPClient, "request", new_callable=AsyncMock) as outbound:
            outbound.return_value = Response(307, b"", {"Location": "https://www.binance.com/content/add"})
            with pytest.raises(HTTPFailure):
                await ReadOnlyHTTPClient().request(method, url)
            assert outbound.await_count == 1
            assert outbound.call_args.kwargs["allow_redirects"] is False


@pytest.mark.asyncio
async def test_probe_retry_counts_and_shared_budget_with_real_mock_http(db):
    from aiohttp import web
    from aiohttp.test_utils import TestServer
    attempts = {"gemini": 0, "groq": 0}

    async def handler(request):
        name = request.match_info["name"]
        attempts[name] += 1
        if name == "gemini" and attempts[name] == 1:
            return web.Response(status=503)
        if name == "gemini":
            return web.json_response({"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": '{"ok":true}'}]}}]})
        return web.json_response({"choices": [{"finish_reason": "stop", "message": {"content": '{"ok":true}'}}]})

    app = web.Application()
    app.router.add_post("/{name}", handler)
    async with TestServer(app) as server:
        class RoutedHTTP(HTTPClient):
            async def request(self, method, url, **kwargs):
                name = "gemini" if "googleapis.com" in url else "groq"
                return await super().request(method, str(server.make_url("/" + name)), **kwargs)
        settings = Settings(gemini_api_key="fake-gem", groq_api_key="fake-groq", ai_requests_per_day=3)
        result = await probe_ai(settings, Store(), RoutedHTTP(attempts=2, backoff=0))
        assert all(s["reachable"] for s in result.values())
        assert attempts == Store().ai_request_counts() == {"gemini": 2, "groq": 1}
        again = await probe_ai(settings, Store(), RoutedHTTP(attempts=2, backoff=0))
        assert all(not s["reachable"] and "budget exhausted" in s["last_error"] for s in again.values())
        assert attempts == {"gemini": 2, "groq": 1}
        assert not Store().queue()
