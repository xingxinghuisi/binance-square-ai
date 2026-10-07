"""Real requests only under explicit RUN_LIVE_TESTS=true; no Square imports/routes."""
import os

import pytest

from src.collectors.binance_market import BinanceMarketCollector
from src.collectors.crypto_news import CryptoNewsCollector, RSS_FEEDS
from src.diagnostics import ReadOnlyHTTPClient, test_ai as probe_ai
from src.settings import Settings
from src.storage import Store

pytestmark = [pytest.mark.live, pytest.mark.skipif(os.getenv("RUN_LIVE_TESTS", "false").lower() != "true",
                                               reason="real API tests require RUN_LIVE_TESTS=true")]


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["gemini", "groq"])
async def test_model_live_smoke(db, name):
    settings = Settings()
    if not getattr(settings, name + "_api_key"):
        pytest.skip(f"{name} API key not configured")
    from dataclasses import replace
    settings = replace(settings, **{("groq" if name == "gemini" else "gemini") + "_api_key": ""})
    http = ReadOnlyHTTPClient(timeout=settings.http_timeout, attempts=settings.http_attempts)
    result = await probe_ai(settings, Store(), http)
    assert result[name]["reachable"], f"{name}: {result[name]['last_error']}"
    assert not Store().queue() and not Store().events()


@pytest.mark.asyncio
async def test_binance_market_live_smoke():
    settings = Settings()
    rows = await BinanceMarketCollector(ReadOnlyHTTPClient(timeout=settings.http_timeout, attempts=settings.http_attempts),
                                        settings.market_symbols).collect()
    assert {r["symbol"] for r in rows} == set(settings.market_symbols)
    assert all(r["last_price"] and r["timestamp"] for r in rows)


@pytest.mark.asyncio
@pytest.mark.parametrize("source", list(RSS_FEEDS))
async def test_rss_live_smoke(source):
    settings = Settings()
    collector = CryptoNewsCollector(ReadOnlyHTTPClient(timeout=settings.http_timeout, attempts=settings.http_attempts),
                                    {source: RSS_FEEDS[source]})
    rows = await collector.collect()
    assert not collector.last_errors, f"{source}: {collector.last_errors}"
    assert all(r["source"] == source and r["published_at"] for r in rows)
