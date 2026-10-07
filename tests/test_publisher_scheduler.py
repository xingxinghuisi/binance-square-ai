import json
import time
from unittest.mock import AsyncMock, patch

import pytest

from services import binance
from src.publisher.binance_square import BinanceSquarePublisher
from src.publisher.retry import backoff_delay
from src.scheduler import Scheduler
from src.storage import Store


@pytest.mark.asyncio
async def test_dry_run_blocks_text_image_and_low_level_network(db, tmp_path):
    with patch("services.binance.aiohttp.ClientSession") as session:
        assert (await binance.publish_text("test")).kind == "dry_run"
        assert (await binance.publish_image_post("test", [(b"image", "x.jpg")])).kind == "dry_run"
        assert (await binance._post_json(None, "https://example.test/content/add", {}))["code"] == "dry_run"
        publisher = BinanceSquarePublisher(tmp_path)
        pid = Store().enqueue("dry-run text", image_paths=["nonexistent.jpg"])
        scheduler = Scheduler(Store(), publisher)
        await scheduler.tick()
        assert await scheduler.publish_one(db.get_binance_post(pid)) == "dry_run"
        session.assert_not_called()
        assert db.get_binance_post(pid)["status"] == "pending"
        assert db.get_binance_post(pid)["attempt_count"] == 0


@pytest.mark.asyncio
async def test_text_and_local_images_use_preserved_client(db, tmp_path, monkeypatch):
    monkeypatch.setenv("AUTO_PUBLISH", "true")
    publisher = BinanceSquarePublisher(tmp_path)
    (tmp_path / "photo.png").write_bytes(b"test-image")
    post_id = Store().enqueue("with image", image_paths=["photo.png"])
    with patch("services.binance.publish_post", new_callable=AsyncMock) as publish:
        publish.return_value = binance.BinanceResult(ok=True, post_id="42", url="https://www.binance.com/square/post/42")
        assert await Scheduler(Store(), publisher).publish_one(db.get_binance_post(post_id)) == "ok"
        assert publish.call_args.args[1] == [(b"test-image", "photo.png")]
    assert db.get_binance_post(post_id)["status"] == "published"
    assert db.list_history()[0]["ext_id"] == "42"


@pytest.mark.asyncio
async def test_image_path_escape_and_legacy_media_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTO_PUBLISH", "true")
    publisher = BinanceSquarePublisher(tmp_path)
    result = await publisher.publish({"text": "test", "image_paths": json.dumps(["../outside.jpg"])})
    assert result.kind == "permanent"
    assert (await publisher.publish({"text": "test", "image_file_ids": ["FID"]})).kind == "permanent"


@pytest.mark.asyncio
async def test_partial_image_failure_never_publishes(monkeypatch):
    monkeypatch.setenv("AUTO_PUBLISH", "true")
    monkeypatch.setattr(binance, "BINANCE_API_KEY", "fake-key")
    monkeypatch.setattr(binance, "BINANCE_IMAGE_FALLBACK_TEXT", False)
    with patch("services.binance.upload_image", new_callable=AsyncMock) as upload, patch("services.binance._publish", new_callable=AsyncMock) as publish:
        upload.side_effect = ["https://binance.test/img", binance.UploadError("temporary upload failure")]
        result = await binance.publish_image_post("text", [(b"a", "a.png"), (b"b", "b.png")])
        assert not result.ok and result.kind == "transient"
        publish.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,outcome", [("transient", "retry"), ("permanent", "dead"), ("auth", "auth"),
                                           ("quota", "quota"), ("uncertain", "review")])
async def test_scheduler_result_transitions_preserve_media(db, monkeypatch, kind, outcome):
    monkeypatch.setenv("AUTO_PUBLISH", "true")
    pid = Store().enqueue("retry me", image_paths=["preserved.png"])
    publisher = AsyncMock()
    publisher.publish.return_value = binance.BinanceResult(ok=False, kind=kind, error="failure")
    assert await Scheduler(Store(), publisher).publish_one(db.get_binance_post(pid)) == outcome
    row = db.get_binance_post(pid)
    assert json.loads(row["image_paths"]) == ["preserved.png"]
    if outcome == "retry":
        assert row["next_attempt_at"] >= int(time.time()) + 298
        assert row["attempt_count"] == 1
    if outcome in {"quota", "auth"}:
        assert db.get_binance_quota_hold() > time.time() and row["attempt_count"] == 0
    if outcome in {"review", "dead"}:
        assert row["status"] == outcome


def test_backoff_and_official_504_semantics():
    assert [backoff_delay(x) for x in range(1, 4)] == [300, 600, 1200]
    assert backoff_delay(100) == 21600
    assert binance._interpret_publish_response({"_http_status": 504}).ok
    assert binance._interpret_publish_response({"_http_status": 401}).kind == "auth"
    assert binance._interpret_publish_response({"_http_status": 200}).kind == "uncertain"
