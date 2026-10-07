from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from src.ai.gemini import GeminiProvider
from src.ai.groq import GroqProvider
from src.ai.provider import FallbackProvider, ProviderError, build_provider
from src.http import HTTPClient, HTTPFailure
from src.logging_config import redact
from src.settings import Settings


@pytest.mark.asyncio
async def test_gemini_request_and_groq_fallback():
    http = AsyncMock()
    http.json.side_effect = [HTTPFailure(429), {"choices": [{"message": {"content": '{"opening":"消息值得留意"}'}}]}]
    provider = FallbackProvider([GeminiProvider(http, "secret-gem", "gem-test"), GroqProvider(http, "secret-groq", "groq-test")])
    assert "opening" in await provider.generate("system", "event")
    assert provider.last_provider == "groq"
    first, second = http.json.call_args_list
    assert first.kwargs["headers"] == {"x-goog-api-key": "secret-gem"}
    assert "secret-gem" not in first.args[1]
    assert second.kwargs["headers"] == {"Authorization": "Bearer secret-groq"}
    assert first.kwargs["json"]["contents"][0]["parts"][0]["text"] == "event"


@pytest.mark.asyncio
async def test_no_keys_and_both_providers_fail(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    with pytest.raises(ProviderError):
        await build_provider(Settings(), HTTPClient()).generate("", "")
    http = AsyncMock()
    http.json.return_value = {"candidates": []}
    with pytest.raises(ProviderError):
        await FallbackProvider([GeminiProvider(http, "key", "model")]).generate("", "")


@pytest.mark.asyncio
async def test_http_retry_then_success_and_no_retry_400():
    count = 0

    async def handler(request):
        nonlocal count
        count += 1
        return web.json_response({"ok": True}, status=503 if count == 1 else 200)

    app = web.Application()
    app.router.add_get("/retry", handler)
    async def bad(request):
        return web.Response(status=400)
    app.router.add_get("/bad", bad)
    async with TestServer(app) as server:
        http = HTTPClient(attempts=2, backoff=0, timeout=1)
        assert await http.json("GET", str(server.make_url("/retry"))) == {"ok": True}
        assert count == 2
        with pytest.raises(HTTPFailure) as error:
            await http.json("GET", str(server.make_url("/bad")))
        assert error.value.status == 400


@pytest.mark.asyncio
async def test_timeout_and_size_limit_are_bounded():
    import asyncio

    async def slow(request):
        await asyncio.sleep(0.05)
        return web.Response(text="late")

    app = web.Application()
    app.router.add_get("/slow", slow)
    async def big(request):
        return web.Response(text="x" * 100)
    app.router.add_get("/big", big)
    async with TestServer(app) as server:
        http = HTTPClient(attempts=2, backoff=0, timeout=0.005)
        with pytest.raises(HTTPFailure) as error:
            await http.request("GET", str(server.make_url("/slow")))
        assert error.value.transport
        with pytest.raises(HTTPFailure):
            await http.request("GET", str(server.make_url("/big")), max_bytes=10)


def test_secret_redaction_and_settings_repr(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "actual-secret")
    assert "actual-secret" not in repr(Settings())
    assert "actual-secret" not in redact("failed actual-secret https://foo.test/upload?signature=xyz")
    assert "xyz" not in redact("https://foo.test/upload?signature=xyz")
