from __future__ import annotations

import logging

import db
from src.ai.provider import build_provider
from src.ai.writer import AIWriter
from src.collectors.binance_market import BinanceMarketCollector
from src.collectors.crypto_news import CryptoNewsCollector
from src.engine.event_engine import EventEngine
from src.engine.rules import allowed
from src.http import HTTPClient
from src.settings import Settings
from src.storage import Store

logger = logging.getLogger(__name__)


class Pipeline:
    def __init__(self, settings: Settings, store: Store, http: HTTPClient):
        self.settings, self.store = settings, store
        self.news = CryptoNewsCollector(http)
        self.market = BinanceMarketCollector(http, settings.market_symbols)
        self.engine = EventEngine(store, min_score=settings.min_score, max_age=settings.max_event_age)
        self.provider = build_provider(settings, http, budget=lambda: store.reserve_ai_request(settings.ai_requests_per_day))

    async def collect_once(self) -> dict:
        stats = {"news": 0, "market": 0, "new_events": 0, "drafts": 0, "failures": 0}
        for collector, field, normalize in ((self.news, "news", self.engine.news_event),
                                             (self.market, "market", self.engine.market_event)):
            try:
                rows = await collector.collect()
                for failure in getattr(collector, "last_errors", []):
                    stats["failures"] += 1
                    db.log_history(kind="collector", service=failure["source"], status="failed", text_preview="",
                                   error=failure["error"])
                stats[field] = len(rows)
                for row in rows:
                    stats["new_events"] += int(self.engine.ingest(normalize(row)))
            except Exception as exc:
                stats["failures"] += 1
                logger.warning("Collector %s failed (%s)", field, type(exc).__name__)
                db.log_history(kind="collector", service=field, status="failed", text_preview="",
                               error=type(exc).__name__)
        stats["drafts"] = await self.generate_pending()
        return stats

    async def generate_pending(self) -> int:
        if not self.provider.providers:
            return 0
        remaining = self.settings.drafts_per_day - self.store.drafts_today()
        if remaining <= 0:
            return 0
        prompt = self.settings.prompt_path.read_text(encoding="utf-8")
        writer = AIWriter(self.provider, max_chars=self.settings.max_chars, prompt=prompt)
        count = 0
        for row in self.store.pending_events(min(remaining, self.settings.drafts_per_cycle), self.settings.event_max_attempts):
            if not allowed(row["event"], min_score=self.settings.min_score, max_age=self.settings.max_event_age):
                self.store.reject_event(row["id"], "event expired or no longer matches rules")
                continue
            if not self.store.claim_event(row["id"]):
                continue
            try:
                draft = await writer.write(row["event"], self.store.recent_openings())
                post_id = self.store.enqueue(draft.text, event_id=row["id"], opening=draft.opening)
                if post_id is None:
                    self.store.reject_event(row["id"], "duplicate draft")
                    continue
                count += 1
                db.log_history(kind="draft", service=self.provider.last_provider, status="prepared",
                               text_preview=draft.text, ext_id=str(post_id))
                logger.info("Prepared draft #%s via %s", post_id, self.provider.last_provider)
            except Exception as exc:
                reason = type(exc).__name__
                self.store.fail_event(row["id"], reason)
                db.log_history(kind="writer", service="ai", status="failed", text_preview="", error=reason)
                logger.warning("Draft generation failed for event %s (%s)", row["id"][:12], reason)
        return count
