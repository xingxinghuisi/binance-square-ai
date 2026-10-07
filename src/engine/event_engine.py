import hashlib

from src.engine import rules, scoring
from src.engine.deduplicator import fingerprint


class EventEngine:
    def __init__(self, store, *, min_score: float = 40, max_age: int = 86400):
        self.store, self.min_score, self.max_age = store, min_score, max_age

    @staticmethod
    def news_event(item: dict) -> dict:
        return {"type": "news", "symbol": next(iter(item["symbols"]), None),
                "score": scoring.news_score(item), "timestamp": item["published_at"],
                "data": item, "source": item["source"]}

    @staticmethod
    def market_event(snapshot: dict) -> dict:
        return {"type": "market_snapshot", "symbol": snapshot["base_asset"],
                "score": scoring.market_score(snapshot), "timestamp": snapshot["timestamp"],
                "data": snapshot, "source": snapshot["source"]}

    def ingest(self, event: dict) -> bool:
        return self.ingest_result(event) == "accepted"

    def ingest_result(self, event: dict) -> str:
        if not rules.allowed(event, min_score=self.min_score, max_age=self.max_age):
            return "rejected"
        key = fingerprint(event)
        event_id = hashlib.sha256((event["type"] + key).encode()).hexdigest()
        return "accepted" if self.store.save_event(event_id, key, event) else "deduplicated"
