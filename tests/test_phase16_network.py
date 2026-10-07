import asyncio
import socket
import ssl
from unittest.mock import MagicMock, patch

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from src.diagnostics import safe_error, source_observer
from src.collectors.binance_market import BinanceMarketCollector
from src.http import HTTPClient, HTTPFailure
from src.storage import Store


@pytest.mark.asyncio
@pytest.mark.parametrize("exc,category,message", [
    (aiohttp.ClientConnectorDNSError(MagicMock(), socket.gaierror("private-key private-host")), "dns", "DNS"),
    (aiohttp.ConnectionTimeoutError("private-key private-host"), "connect_timeout", "connect timeout"),
    (aiohttp.SocketTimeoutError("private-key private-host"), "read_timeout", "read timeout"),
    (ssl.SSLError("private-key private-host"), "tls", "TLS"),
    (asyncio.TimeoutError("private-key private-host"), "timeout", "total deadline"),
    (aiohttp.ClientConnectionError("private-key private-host"), "connection", "connection failed"),
])
async def test_network_categories_survive_retry_without_raw_details(db, exc, category, message):
    session = MagicMock()
    session.request.side_effect = exc
    http = HTTPClient(session=session, attempts=2, backoff=0)
    collector = BinanceMarketCollector(http, observer=source_observer(Store()))
    with pytest.raises(HTTPFailure) as failure:
        await collector.collect()
    assert failure.value.transport and failure.value.category == category
    assert session.request.call_count == 2
    state = Store().diagnostic("source", "Binance Spot")
    assert not state["reachable"] and message in state["last_error"]
    assert "private-key" not in str(failure.value) + str(state)
    assert "private-host" not in str(state)


@pytest.mark.asyncio
async def test_real_local_read_timeout_and_http_status():
    async def slow(request):
        await asyncio.sleep(0.1)
        return web.Response(text="late")

    async def unavailable(request):
        return web.Response(status=451, text="sensitive response body")

    app = web.Application()
    app.router.add_get("/slow", slow)
    app.router.add_get("/unavailable", unavailable)
    async with TestServer(app) as server:
        http = HTTPClient(timeout=1, attempts=1)
        with pytest.raises(HTTPFailure) as failure:
            await http.request("GET", str(server.make_url("/slow")),
                               timeout=aiohttp.ClientTimeout(total=1, sock_read=0.01))
        assert safe_error(failure.value) == "read timeout"
        with pytest.raises(HTTPFailure) as failure:
            await http.request("GET", str(server.make_url("/unavailable")))
        assert safe_error(failure.value) == "HTTP 451"
        assert "sensitive" not in str(failure.value)
    with patch("src.http.HTTPClient.request", side_effect=AssertionError("unexpected network")):
        assert safe_error(HTTPFailure(503)) == "HTTP 503"
