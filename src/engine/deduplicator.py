import hashlib
import re
from datetime import datetime


def fingerprint(event: dict) -> str:
    if event["type"] == "news":
        # Same headline syndicated by different RSS sources is only processed once.
        text = re.sub(r"\W+", "", event["data"]["title"].casefold())
        value = f"news:{text}"
    else:
        hour = int(datetime.fromisoformat(event["timestamp"]).timestamp()) // 3600
        value = f"market:{event['symbol']}:{hour}"
    return hashlib.sha256(value.encode()).hexdigest()
