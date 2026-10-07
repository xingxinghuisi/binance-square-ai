"""Local commands prepare drafts only. No publish command is exposed."""
from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime

import db
from src.http import HTTPClient
from src.pipeline import Pipeline
from src.publisher.binance_square import BinanceSquarePublisher
from src.settings import Settings
from src.storage import Store


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("collect-once", help="Collect RSS and market, generate dry-run drafts")
    enqueue = commands.add_parser("enqueue", help="Store a local text/image draft without sending")
    enqueue.add_argument("--text", required=True)
    enqueue.add_argument("--image", action="append", default=[], help="Image path relative to MEDIA_ROOT; up to four")
    enqueue.add_argument("--publish-at", help="ISO-8601 timestamp with timezone")
    args = parser.parse_args()
    settings, store = Settings(), Store()
    db.init_db()
    if args.command == "collect-once":
        print(json.dumps(asyncio.run(Pipeline(settings, store, HTTPClient(
            timeout=settings.http_timeout, attempts=settings.http_attempts)).collect_once()), ensure_ascii=False))
    else:
        if len(args.text) > settings.max_chars:
            parser.error("text exceeds WRITER_MAX_CHARS")
        try:
            BinanceSquarePublisher(settings.media_root).image_files(args.image)
            publish_at = None
            if args.publish_at:
                date = datetime.fromisoformat(args.publish_at.replace("Z", "+00:00"))
                if date.tzinfo is None:
                    raise ValueError("publish-at requires a timezone")
                publish_at = int(date.timestamp())
            pid = store.enqueue(args.text, image_paths=args.image, publish_at=publish_at)
        except (ValueError, OSError) as exc:
            parser.error(str(exc))
        if pid is None:
            parser.error("duplicate draft")
        db.log_history(kind="draft", service="manual", status="prepared", text_preview=args.text, ext_id=str(pid))
        print(f"Prepared draft #{pid}; no post was sent")


if __name__ == "__main__":
    main()
