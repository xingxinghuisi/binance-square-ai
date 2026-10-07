from decimal import Decimal


def news_score(item: dict) -> float:
    # Initial heuristic, not a prediction or investment recommendation.
    return min(100, 40 + 10 * bool(item.get("symbols")) + 10 * bool(item.get("summary")))


def market_score(snapshot: dict) -> float:
    return round(min(100.0, 40 + float(abs(Decimal(snapshot["change_24h"]))) * 3), 2)
