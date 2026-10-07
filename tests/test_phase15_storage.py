import pytest

from src.storage import Store


def test_draft_metadata_quality_and_restart(db):
    store = Store()
    event = {"type": "news", "source": "Decrypt", "symbol": "ETH", "score": 70}
    assert store.save_event("e", "f", event)
    pid = store.enqueue("待审阅正文 $ETH", event_id="e", ai_provider="gemini")
    post = store.preview()[0]
    assert (post["event_type"], post["source"], post["symbol"], post["score"]) == ("news", "Decrypt", "ETH", 70)
    assert post["ai_provider"] == "gemini" and post["generated_at"]
    assert post["quality_status"] == "pending"
    store.rate_draft(pid, "bad", "开头太模板化")
    db.init_db()
    assert store.draft(pid)["quality_note"] == "开头太模板化"
    assert store.draft(pid)["quality_status"] == "bad"
    with pytest.raises(ValueError):
        store.rate_draft(pid, "published")
    with pytest.raises(ValueError):
        store.rate_draft(pid, "good", "x" * 2001)
    with pytest.raises(LookupError):
        store.rate_draft(999, "good")
    db.mark_binance_published(pid)
    assert not store.preview()
    with pytest.raises(LookupError):
        store.rate_draft(pid, "good")


def test_v6_upgrade_preserves_queue_and_adds_pending_quality(db):
    pid = Store().enqueue("旧草稿")
    with db.get_conn() as conn:
        for column in ("ai_provider", "generated_at", "quality_status", "quality_note"):
            conn.execute(f"ALTER TABLE binance_queue DROP COLUMN {column}")
        conn.execute("PRAGMA user_version=6")
    db.init_db()
    post = Store().draft(pid)
    assert post["text"] == "旧草稿" and post["quality_status"] == "pending"
    assert post["ai_provider"] is None and post["generated_at"] is None
    db.init_db()  # idempotent migration


def test_status_history_and_provider_request_budget_are_durable(db, monkeypatch):
    store = Store()
    monkeypatch.setenv("GEMINI_API_KEY", "real-secret-test")
    store.record_status("source", "Decrypt", success=True, latency_ms=12, items=4)
    previous = store.diagnostic("source", "Decrypt")["last_success"]
    store.record_status("source", "Decrypt", success=False, error="real-secret-test", latency_ms=5, items=0)
    db.init_db()
    state = store.diagnostic("source", "Decrypt")
    assert state["last_success"] == previous and not state["reachable"]
    assert state["last_error"] == "[REDACTED]" and state["items"] == 0
    store.reserve_ai_request(2, "gemini")
    store.reserve_ai_request(2, "groq")
    from src.http import BudgetExceeded
    with pytest.raises(BudgetExceeded):
        store.reserve_ai_request(2, "gemini")
    assert store.ai_request_counts() == {"gemini": 1, "groq": 1}
