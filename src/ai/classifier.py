"""Phase 1 classification is deterministic; no extra AI request or hidden API cost."""


def classify(event: dict) -> str:
    if event["type"] == "market_snapshot":
        return "market"
    return "news"
