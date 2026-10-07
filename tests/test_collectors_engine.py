from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from src.collectors.binance_market import BinanceMarketCollector
from src.collectors.crypto_news import CryptoNewsCollector, extract_symbols
from src.engine.event_engine import EventEngine
from src.http import HTTPFailure, Response
from src.storage import Store

RSS = b'''<rss version="2.0"><channel><title>News</title>
<item><title>Bitcoin and Ethereum update</title><link>https://news.test/a?utm_source=rss</link>
<description>&lt;p&gt;Bitcoin context&lt;/p&gt;</description><pubDate>Wed, 07 Oct 2026 00:00:00 GMT</pubDate></item>
<item><title>No date</title><link>https://news.test/b</link></item>
<item><title>Bad link</title><link>javascript:evil</link><pubDate>Wed, 07 Oct 2026 00:00:00 GMT</pubDate></item>
</channel></rss>'''


def market_fixture():
    return {"symbol": "BTCUSDT", "lastPrice": "60000.12", "priceChangePercent": "-1.25", "volume": "123.45",
            "quoteVolume": "7654321.00", "highPrice": "61000.00", "lowPrice": "59000.00",
            "closeTime": int(datetime.now(timezone.utc).timestamp() * 1000)}


def test_news_schema_sanitization_dates_and_symbols():
    item, = CryptoNewsCollector.parse("Test", RSS)
    assert set(item) == {"id", "source", "title", "summary", "url", "published_at", "symbols"}
    assert item["url"] == "https://news.test/a"
    assert item["summary"] == "Bitcoin context"
    assert item["symbols"] == ["BTC", "ETH"]
    assert item["published_at"] == "2026-10-07T00:00:00+00:00"
    assert extract_symbols("tether method Blockchain") == []
    assert extract_symbols("$SOL 比特币 以太坊") == ["BTC", "ETH", "SOL"]


@pytest.mark.asyncio
async def test_one_failed_feed_does_not_block_other_feeds():
    http = AsyncMock()
    http.request.side_effect = [HTTPFailure(403), Response(200, RSS)]
    items = await CryptoNewsCollector(http, {"Blocked": "https://x.test", "Good": "https://y.test"}).collect()
    assert len(items) == 1 and items[0]["source"] == "Good"


@pytest.mark.asyncio
async def test_market_schema_and_exchange_metadata_cache():
    http = AsyncMock()
    http.json.side_effect = [{"symbols": [{"symbol": "BTCUSDT", "baseAsset": "BTC", "quoteAsset": "USDT"}]},
                             [market_fixture()], [market_fixture()]]
    collector = BinanceMarketCollector(http, ("BTCUSDT",))
    first, = await collector.collect()
    assert first["last_price"] == "60000.12" and first["change_24h"] == "-1.25"
    assert first["volume_24h"] == "123.45" and first["base_asset"] == "BTC"
    await collector.collect()
    assert http.json.await_count == 3
    bad = market_fixture() | {"lastPrice": "NaN"}
    with pytest.raises(ValueError):
        collector.normalize(bad, "BTC", "USDT")


def test_event_shape_scoring_rules_and_persistent_dedup(db):
    store = Store()
    engine = EventEngine(store)
    snapshot = BinanceMarketCollector.normalize(market_fixture(), "BTC", "USDT")
    event = engine.market_event(snapshot)
    assert set(event) == {"type", "symbol", "score", "timestamp", "data", "source"}
    assert event["symbol"] == "BTC" and event["score"] == 43.75
    assert engine.ingest(event)
    assert not EventEngine(Store()).ingest(event)
    old = event | {"timestamp": (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()}
    assert not engine.ingest(old)
    future = event | {"timestamp": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()}
    assert not engine.ingest(future)
    assert not engine.ingest(event | {"score": float("nan")})


def test_cross_source_news_dedup_and_atomic_queue(db):
    store = Store()
    engine = EventEngine(store)
    item, = CryptoNewsCollector.parse("Test", RSS)
    item["published_at"] = datetime.now(timezone.utc).isoformat()
    assert engine.ingest(engine.news_event(item))
    assert not engine.ingest(engine.news_event(item | {"source": "Other", "url": "https://other.test/news"}))
    row, = store.pending_events(3, 3)
    assert store.claim_event(row["id"])
    assert not store.claim_event(row["id"])
    pid = store.enqueue("消息仍需进一步观察 $BTC $ETH", event_id=row["id"], opening="消息仍需进一步观察")
    assert pid
    assert store.enqueue("消息仍需进一步观察 $BTC $ETH") is None
    assert store.enqueue("different text", event_id=row["id"]) is None
    assert not db.is_duplicate("different text")  # conflict rolls the whole transaction back
    assert store.recent_openings() == ["消息仍需进一步观察"]
    assert len(store.queue()) == 1 and store.drafts_today() == 1
