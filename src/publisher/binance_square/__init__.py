from __future__ import annotations

import asyncio
import json
from pathlib import Path

from services import binance
from src.settings import publishing_enabled


class BinanceSquarePublisher:
    def __init__(self, media_root: Path):
        self.media_root = media_root.resolve()

    def image_files(self, paths: list[str]) -> list[tuple[bytes, str]]:
        if len(paths) > 4:
            raise ValueError("at most four images")
        result = []
        for value in paths:
            path = Path(value)
            path = (path if path.is_absolute() else self.media_root / path).resolve()
            if not path.is_relative_to(self.media_root):
                raise ValueError("image must be inside MEDIA_ROOT")
            if not path.is_file() or not 0 < path.stat().st_size <= 10 * 1024 * 1024:
                raise ValueError("image missing, empty, or larger than 10 MB")
            if path.suffix.lower().lstrip(".") not in binance.IMAGE_EXT_TO_MIME:
                raise ValueError("unsupported image extension")
            result.append((path.read_bytes(), path.name))
        return result

    async def publish(self, post: dict) -> binance.BinanceResult:
        if not publishing_enabled():
            return binance.BinanceResult(ok=False, kind="dry_run", error="AUTO_PUBLISH=false")
        # Refuse to drop old Telegram or URL-only media during a gradual migration.
        if post.get("image_file_ids") or post.get("image_urls") or post.get("video_file_id"):
            return binance.BinanceResult(ok=False, kind="permanent", error="legacy media requires manual migration")
        try:
            paths = json.loads(post.get("image_paths") or "[]")
            if not isinstance(paths, list) or any(not isinstance(x, str) for x in paths):
                raise ValueError("invalid image paths")
            payload = await asyncio.to_thread(self.image_files, paths)
        except (ValueError, OSError) as exc:
            return binance.BinanceResult(ok=False, kind="permanent", error=type(exc).__name__ + ": invalid local media")
        return await binance.publish_post(post["text"], payload)
