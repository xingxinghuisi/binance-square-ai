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
from src.diagnostics import safe_error, source_observer
from src.settings import Settings
from src.storage import Store

logger = logging.getLogger(__name__)


class Pipeline:
    def __init__(self, settings: Settings, store: Store, http: HTTPClient):
        self.settings, self.store = settings, store
        self.news = CryptoNewsCollector(http, observer=source_observer(store))
        self.market = BinanceMarketCollector(http, settings.market_symbols, observer=source_observer(store))
        self.engine = EventEngine(store, min_score=settings.min_score, max_age=settings.max_event_age)
        self.provider = build_provider(settings, http, store=store)

    def request_counts(self):
        return {name: sum(getattr(p, "request_count", 0) for p in self.provider.providers
                          if getattr(p, "name", None) == name) for name in ("gemini", "groq")}

    async def collect_once(self) -> dict:
        before = self.request_counts()
        stats = {"news": 0, "market": 0, "new_events": 0, "drafts": 0, "failures": 0,
                 "rss": {}, "market_symbols": {s: "ERROR" for s in self.settings.market_symbols},
                 "events": {"received": 0, "accepted": 0, "deduplicated": 0, "rejected": 0}, "errors": []}
        for collector, field, normalize, source in ((self.news, "news", self.engine.news_event, "RSS"),
                                                    (self.market, "market", self.engine.market_event, "Binance Spot")):
            rows = []
            try:
                rows = await collector.collect()
                for failure in getattr(collector, "last_errors", []):
                    stats["failures"] += 1
                    stats["errors"].append(failure)
                    db.log_history(kind="collector", service=failure["source"], status="failed", text_preview="",
                                   error=failure["error"])
                stats[field] = len(rows)
                for row in rows:
                    stats["events"]["received"] += 1
                    if field == "market":
                        stats["market_symbols"][row["symbol"]] = "OK"
                    try:
                        outcome = self.engine.ingest_result(normalize(row))
                    except (ValueError, KeyError, TypeError) as exc:
                        outcome = "rejected"
                        stats["errors"].append({"source": row.get("source", source), "error": safe_error(exc)})
                    stats["events"][outcome] += 1
            except Exception as exc:
                error = safe_error(exc)
                stats["failures"] += 1
                stats["errors"].append({"source": source, "error": error})
                logger.warning("Collector %s failed: %s", source, error)
                db.log_history(kind="collector", service=source, status="failed", text_preview="", error=error)
            if field == "news":
                stats["rss"] = dict(self.news.last_counts)
                # Also supports injected fixture collectors without diagnostic hooks.
                for row in rows:
                    stats["rss"].setdefault(row["source"], sum(r["source"] == row["source"] for r in rows))
        stats["new_events"] = stats["events"]["accepted"]
        self.generation_errors = []
        stats["drafts"] = await self.generate_pending()
        stats["errors"].extend(self.generation_errors)
        after = self.request_counts()
        stats["ai"] = {name + "_requests": after[name] - before[name] for name in before}
        stats["ai"]["drafts"] = stats["drafts"]
        return stats

    async def generate_pending(self) -> int:
        self.generation_errors = []
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
                post_id = self.store.enqueue(draft.text, event_id=row["id"], opening=draft.opening,
                                             ai_provider=self.provider.last_provider)
                if post_id is None:
                    self.store.reject_event(row["id"], "duplicate draft")
                    continue
                count += 1
                db.log_history(kind="draft", service=self.provider.last_provider, status="prepared",
                               text_preview=draft.text, ext_id=str(post_id))
                logger.info("Prepared draft #%s via %s", post_id, self.provider.last_provider)
            except Exception as exc:
                reason = safe_error(exc)
                self.generation_errors.append({"source": row["event"]["source"], "error": "AI: " + reason})
                self.store.fail_event(row["id"], reason)
                db.log_history(kind="writer", service="ai", status="failed", text_preview="", error=reason)
                logger.warning("Draft generation failed for event %s (%s)", row["id"][:12], reason)
        return count
