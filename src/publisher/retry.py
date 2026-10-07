from datetime import datetime, timedelta, timezone


def backoff_delay(attempts_done: int, *, base: int = 300, maximum: int = 21600) -> int:
    return min(maximum, base * 2 ** min(30, max(0, attempts_done - 1)))


def next_utc_midnight() -> int:
    return int((datetime.now(timezone.utc) + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0).timestamp())
