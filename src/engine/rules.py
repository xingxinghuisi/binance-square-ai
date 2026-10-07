import math
from datetime import datetime, timezone


def allowed(event: dict, *, min_score: float, max_age: int, now: datetime | None = None) -> bool:
    try:
        if event["type"] not in {"news", "market_snapshot"} or not math.isfinite(float(event["score"])):
            return False
        date = datetime.fromisoformat(event["timestamp"].replace("Z", "+00:00"))
        if date.tzinfo is None:
            return False
        age = ((now or datetime.now(timezone.utc)) - date).total_seconds()
        return -300 <= age <= max_age and min_score <= event["score"] <= 100
    except (ValueError, KeyError, TypeError):
        return False
