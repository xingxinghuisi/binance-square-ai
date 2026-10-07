from __future__ import annotations

import asyncio
import logging
import time

import db
from config import BINANCE_BACKOFF_BASE_SEC, BINANCE_BACKOFF_MAX_SEC, BINANCE_MAX_ATTEMPTS, BINANCE_PUBLISHED_TTL_DAYS
from src.publisher.retry import backoff_delay, next_utc_midnight
from src.settings import publishing_enabled

logger = logging.getLogger(__name__)


class Scheduler:
    def __init__(self, store, publisher, *, dry_run: bool = False):
        self.store, self.publisher = store, publisher
        self.dry_run = dry_run

    async def publish_one(self, post: dict) -> str:
        if self.dry_run or not publishing_enabled():
            return "dry_run"
        if not db.claim_binance_post(post["id"]):
            return "skip"
        # Read current text/media only after the atomic claim.
        post = db.get_binance_post(post["id"])
        try:
            result = await self.publisher.publish(post)
        except Exception as exc:
            # A publisher exception can occur after remote acceptance.
            self.store.review_post(post["id"], f"publisher exception: {type(exc).__name__}")
            db.log_history(kind="binance", service="binance_square", status="review",
                           text_preview=post["text"], error=type(exc).__name__)
            return "review"
        if result.kind == "dry_run":
            db.release_binance_post(post["id"])
            return "dry_run"
        if result.ok:
            db.mark_binance_published(post["id"])
            db.log_history(kind="binance", service="binance_square", status="success", text_preview=post["text"],
                           ext_id=result.post_id, ext_url=result.url)
            return "ok"
        error, kind = result.error or "unknown", result.kind or "transient"
        db.log_history(kind="binance", service="binance_square", status="failed", text_preview=post["text"], error=error)
        if kind == "uncertain":
            self.store.review_post(post["id"], error)
            return "review"
        if kind in {"quota", "auth"}:
            until = next_utc_midnight() if kind == "quota" else int(time.time()) + 3600
            db.set_binance_quota_hold(until)
            db.release_binance_post(post["id"], until)
            db.defer_pending_binance(until)
            return kind
        attempts = int(post.get("attempt_count") or 0) + 1
        if kind == "permanent" or attempts >= BINANCE_MAX_ATTEMPTS:
            db.mark_binance_dead(post["id"], error)
            return "dead"
        delay = backoff_delay(attempts, base=BINANCE_BACKOFF_BASE_SEC, maximum=BINANCE_BACKOFF_MAX_SEC)
        db.mark_binance_retry(post["id"], error, int(time.time()) + delay)
        return "retry"

    async def tick(self):
        db.cleanup_published_binance(BINANCE_PUBLISHED_TTL_DAYS)
        if self.dry_run or not publishing_enabled() or db.is_binance_paused() or db.get_binance_quota_hold() > time.time():
            return
        for post in db.get_pending_binance_posts()[:20]:
            if self.dry_run or db.is_binance_paused() or not publishing_enabled():
                break
            outcome = await self.publish_one(post)
            logger.info("Queue post #%s outcome=%s", post["id"], outcome)
            if outcome in {"auth", "quota", "dry_run"}:
                break
            await asyncio.sleep(0.4)
