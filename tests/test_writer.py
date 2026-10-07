import json
from unittest.mock import AsyncMock

import pytest

from src.ai.writer import AIWriter, WriterError
from src.collectors.binance_market import BinanceMarketCollector
from src.engine.event_engine import EventEngine
from tests.test_collectors_engine import market_fixture


def event():
    return EventEngine.market_event(BinanceMarketCollector.normalize(market_fixture(), "BTC", "USDT"))


@pytest.mark.asyncio
async def test_writer_only_uses_verified_numbers_and_correct_tag():
    provider = AsyncMock()
    provider.generate.return_value = json.dumps({"opening": "先观察，再形成判断", "commentary": "若关注短期变化，建议结合后续快照核验。"})
    draft = await AIWriter(provider, max_chars=400, prompt="口语化").write(event(), [])
    assert "$BTC" in draft.text and "$ETH" not in draft.text
    assert "60000.12 USDT" in draft.text and "-1.25%" in draft.text
    assert "123.45 BTC" in draft.text
    assert len(draft.text) <= 400 and "口语化" in provider.generate.call_args.args[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("commentary", ["价格到了999999美元", "将翻倍", "收益百分之一百", "最新价十万美元", "$ETH可以买入"])
async def test_writer_rejects_hallucinated_prices_and_tags(commentary):
    provider = AsyncMock()
    provider.generate.return_value = json.dumps({"opening": "先观察，再形成判断", "commentary": commentary})
    with pytest.raises(WriterError):
        await AIWriter(provider).write(event(), [])


@pytest.mark.asyncio
async def test_repeated_opening_regenerates_and_is_persistable():
    provider = AsyncMock()
    provider.generate.side_effect = [json.dumps({"opening": "先观察，再形成判断", "commentary": "若关注短期变化，建议核验。"}),
                                     json.dumps({"opening": "这份快照值得留作对照", "commentary": "后续可以继续观察，避免过早下结论。"})]
    result = await AIWriter(provider).write(event(), ["先观察，再形成判断！"])
    assert result.opening == "这份快照值得留作对照" and provider.generate.await_count == 2


@pytest.mark.asyncio
async def test_news_keeps_source_without_inventing_price():
    provider = AsyncMock()
    provider.generate.return_value = json.dumps({"opening": "消息还需要进一步核验", "commentary": "若关注后续影响，建议对照原文并持续观察。"})
    news = {"type": "news", "symbol": "ETH", "score": 60, "timestamp": "2026-10-07T00:00:00+00:00",
            "source": "Test", "data": {"title": "Ethereum update", "url": "https://test.example/news",
                                      "symbols": ["ETH", "FAKE"]}}
    draft = await AIWriter(provider).write(news, [])
    assert "$ETH" in draft.text and "$FAKE" not in draft.text
    assert "https://test.example/news" in draft.text and "60000" not in draft.text


@pytest.mark.asyncio
async def test_invalid_json_and_impossible_length_fail_closed():
    provider = AsyncMock()
    provider.generate.return_value = "not json"
    with pytest.raises(WriterError):
        await AIWriter(provider).write(event(), [])
    with pytest.raises(WriterError):
        await AIWriter(provider, max_chars=20).write(event(), [])
