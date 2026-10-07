from datetime import datetime, timezone
import sqlite3

import pytest

from src.http import BudgetExceeded
from src.storage import Store


def add_draft(store, i, status="pending", *, source="Decrypt", symbol="BTC", provider="gemini", note=""):
    store.save_event(str(i), str(i), {"type": "news", "source": source, "symbol": symbol, "score": 70})
    pid = store.enqueue(f"草稿 {i}", event_id=str(i), ai_provider=provider)
    store.rate_draft(pid, status, note)
    return pid


def test_v7_additive_migration_preserves_quality_and_does_not_invent_history(db):
    store = Store()
    pid = add_draft(store, 1, "bad", note="开头太模板化")
    with db.get_conn() as conn:
        conn.execute("DROP TABLE collection_runs")
        conn.execute("DROP TABLE ai_request_log")
        conn.execute("DELETE FROM kv WHERE key='session_recording_since'")
        conn.execute("PRAGMA user_version=7")
    db.kv_set("ai_requests:2026-10-07:gemini", 20)
    db.init_db()
    recorded = db.kv_get("session_recording_since")
    db.init_db()
    assert db.kv_get("session_recording_since") == recorded
    assert store.draft(pid)["quality_note"] == "开头太模板化"
    report = store.session_report()
    assert report["ai_requests"]["total"] == 0
    assert not report["complete_window"]
    assert db.kv_get("ai_requests:2026-10-07:gemini") == 20


def test_quality_report_counts_denominator_grouping_and_empty_notes(db):
    store = Store()
    assert store.quality_report()["good_rate"] is None
    add_draft(store, 1, "good")
    add_draft(store, 2, "bad", note="  开头  太模板化\n")
    add_draft(store, 3, "bad", provider="groq", note="开头 太模板化")
    add_draft(store, 4, "bad", source="Cointelegraph", symbol="ETH", provider="groq")
    add_draft(store, 5, "pending", symbol="ETH")
    store.enqueue("手工未评分")
    published = add_draft(store, 6, "good")
    db.mark_binance_published(published)
    result = store.quality_report()
    assert (result["total"], result["pending"], result["good"], result["bad"]) == (6, 2, 1, 3)
    assert result["good_rate"] == 0.25  # pending is excluded from the denominator
    assert result["bad_reasons"] == [{"reason": "开头 太模板化", "count": 2}, {"reason": "(no note)", "count": 1}]
    assert result["by"]["source"]["Decrypt"] == {"good": 1, "bad": 2}
    assert result["by"]["symbol"]["ETH"] == {"good": 0, "bad": 1}
    assert result["by"]["ai_provider"]["groq"] == {"good": 0, "bad": 2}
    assert result["by"]["event_type"]["unknown"] == {"good": 0, "bad": 0}
    db.init_db()
    assert store.quality_report() == result


def test_quality_bad_reasons_are_limited_to_top_ten(db):
    store = Store()
    for i in range(12):
        add_draft(store, i, "bad", note=f"reason {i:02}")
    add_draft(store, 12, "bad", note="reason 11")
    reasons = store.quality_report()["bad_reasons"]
    assert len(reasons) == 10 and reasons[0] == {"reason": "reason 11", "count": 2}


def test_session_rolling_24_hours_across_midnight_counts_retries_and_excludes_old_future(db, monkeypatch):
    store = Store()
    now = int(datetime(2026, 10, 8, 1, tzinfo=timezone.utc).timestamp())
    db.kv_set("session_recording_since", now - 172800)
    for timestamp, provider in ((now - 86401, "gemini"), (now - 7200, "groq"),
                                (now - 7200, "groq"), (now, "gemini"), (now, "gemini"), (now + 1, "groq")):
        with monkeypatch.context() as clock:
            clock.setattr("src.storage.time.time", lambda timestamp=timestamp: timestamp)
            store.reserve_ai_request(10, provider)
    with monkeypatch.context() as clock:
        clock.setattr("src.storage.time.time", lambda: now)
        with pytest.raises(BudgetExceeded):
            store.reserve_ai_request(3, "groq")  # future fixture already reserved a third request today
    for timestamp in (now - 86401, now - 7200, now, now + 1):
        with monkeypatch.context() as clock:
            clock.setattr("src.storage.time.time", lambda timestamp=timestamp: timestamp)
            store.record_collection({"received": 9, "accepted": 3, "rejected": 2, "deduplicated": 4}, started_at=timestamp - 1)
    ids = [add_draft(store, i, status) for i, status in enumerate(("good", "bad", "pending", "good", "bad"))]
    with db.get_conn() as conn:
        for pid, timestamp in zip(ids, (now - 7200, now, now, now - 86401, now + 1), strict=True):
            conn.execute("UPDATE binance_queue SET generated_at=? WHERE id=?", (timestamp, pid))
    store.enqueue("手工草稿不计入 AI 生成数")
    result = store.session_report(now=now)
    assert result["complete_window"]
    assert result["events"] == {"received": 18, "accepted": 6, "rejected": 4, "deduplicated": 8}
    assert result["ai_requests"] == {"total": 4, "gemini": 2, "groq": 2, "unknown": 0}
    assert result["drafts"] == {"total": 3, "good": 1, "bad": 1, "pending": 1}
    db.init_db()
    assert store.session_report(now=now) == result


def test_request_reservation_rolls_back_telemetry_if_budget_or_database_rejects(db):
    store = Store()
    store.reserve_ai_request(1, "gemini")
    with pytest.raises(BudgetExceeded):
        store.reserve_ai_request(1, "groq")
    assert store.session_report()["ai_requests"]["total"] == 1
    with db.get_conn() as conn:
        conn.execute("CREATE TRIGGER fail_telemetry BEFORE INSERT ON ai_request_log "
                     "BEGIN SELECT RAISE(ABORT,'fixture write failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        store.reserve_ai_request(10, "groq")
    assert store.ai_request_counts() == {"gemini": 1, "groq": 0}
    assert store.session_report()["ai_requests"]["total"] == 1
    with pytest.raises(ValueError):
        store.record_collection({"received": 3, "accepted": 1, "rejected": 1, "deduplicated": 0}, started_at=1)
    assert store.session_report()["events"]["received"] == 0


@pytest.mark.asyncio
async def test_collection_outcomes_are_saved_before_writer_failure(db):
    from unittest.mock import AsyncMock
    from src.pipeline import Pipeline
    from src.settings import Settings
    from src.collectors.binance_market import BinanceMarketCollector
    from tests.test_collectors_engine import market_fixture

    pipeline = Pipeline(Settings(gemini_api_key="", groq_api_key=""), Store(), AsyncMock())
    pipeline.news.collect = AsyncMock(return_value=[])
    pipeline.market.collect = AsyncMock(return_value=[BinanceMarketCollector.normalize(market_fixture(), "BTC", "USDT")])
    pipeline.generate_pending = AsyncMock(side_effect=RuntimeError("fixture writer failure"))
    with pytest.raises(RuntimeError):
        await pipeline.collect_once()
    assert Store().session_report()["events"] == {"received": 1, "accepted": 1, "rejected": 0, "deduplicated": 0}
