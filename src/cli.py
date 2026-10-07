"""Phase 1.6 staging diagnostics and draft review. Never publish to Binance Square."""
from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime, timezone

import db
from src.diagnostics import ReadOnlyHTTPClient, staging_readiness, test_ai as probe_ai, test_sources as probe_sources
from src.logging_config import redact
from src.pipeline import Pipeline
from src.settings import Settings, enforce_dry_run
from src.storage import Store


def print_collection(stats):
    print("RSS:")
    from src.collectors.crypto_news import RSS_FEEDS
    for source in RSS_FEEDS:
        print(f"  {source}: {stats['rss'].get(source, 0)}")
    print("Market:")
    for symbol, status in stats["market_symbols"].items():
        print(f"  {symbol}: {status}")
    print("Events:")
    for field, value in stats["events"].items():
        print(f"  {field}: {value}")
    print("AI:")
    print(f"  Gemini requests: {stats['ai']['gemini_requests']}")
    print(f"  Groq requests: {stats['ai']['groq_requests']}")
    print(f"  drafts: {stats['drafts']}")
    for error in stats["errors"]:
        print(f"ERROR [{error['source']}]: {error['error']}")
    print("DRY RUN: no Binance Square requests or image uploads")


def print_preview(posts):
    if not posts:
        print("No drafts")
    for post in posts:
        print(f"--- Draft #{post['id']} ---")
        for title, field in (("Event Type", "event_type"), ("Source", "source"), ("Symbol", "symbol"),
                             ("Score", "score"), ("AI Provider", "ai_provider"), ("Generated At", "generated_at"),
                             ("Quality", "quality_status"), ("Quality Note", "quality_note")):
            value = post.get(field)
            if field == "generated_at" and value:
                from datetime import timezone
                value = datetime.fromtimestamp(value, timezone.utc).isoformat()
            print(f"{title}: {value if value is not None else '—'}")
        print(post["text"])


def print_readiness(report):
    print(f"Database: {'OK' if report['database'] else 'FAILED'}")
    print(f"Dry Run Lock: {'OK' if report['dry_run_lock'] else 'FAILED'}")
    for name, configured in report["providers"].items():
        print(f"{name.title()}: {'configured' if configured else 'not configured'}")
    print(f"ADMIN_TOKEN: {'configured' if report['admin_token_configured'] else 'not configured'}")
    spot = report["binance_spot"]
    print(f"Binance Spot (critical): {spot['status']}")
    if spot.get("last_error"):
        print(f"  ERROR [Binance Spot]: {redact(spot['last_error'])}")
    print("RSS:")
    for source in report["rss"]:
        print(f"  {source['source']}: {source['status']} (noncritical)")
    print("Square API Key: ignored/not required")
    print("AUTO_PUBLISH: FALSE")
    print(f"ready={str(report['ready']).lower()}")
    if report["blocking"]:
        print("Blocking: " + ", ".join(report["blocking"]))


def print_quality(report):
    for field in ("total", "pending", "good", "bad"):
        print(f"{field}: {report[field]}")
    rate = report["good_rate"]
    print("good_rate: " + (f"{rate:.2%}" if rate is not None else "N/A (no rated drafts)"))
    print("Bad reasons (top 10):")
    for reason in report["bad_reasons"]:
        print(f"  {redact(reason['reason'])}: {reason['count']}")
    for field, groups in report["by"].items():
        print(f"By {field}:")
        for name, counts in groups.items():
            print(f"  {redact(name)}: good={counts['good']} bad={counts['bad']}")


def print_session(report):
    print("Past 24 hours (UTC):")
    for field in ("since", "until", "recording_since"):
        print(f"  {field}: {datetime.fromtimestamp(report[field], timezone.utc).isoformat()}")
    if not report["complete_window"]:
        print("Coverage: partial; recording started within this window, older counters are unavailable")
    print("Collected events:")
    for field, count in report["events"].items():
        print(f"  {field}: {count}")
    print("AI requests (attempts, including retries and test-ai):")
    for field, count in report["ai_requests"].items():
        print(f"  {field}: {count}")
    print("Generated drafts (current quality):")
    for field, count in report["drafts"].items():
        print(f"  {field}: {count}")


def main(argv=None):
    enforce_dry_run()
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("collect-once", help="Collect RSS and market, generate dry-run drafts")
    commands.add_parser("test-ai", help="Probe each configured AI with a tiny JSON request; no posts")
    commands.add_parser("test-sources", help="Read public market/RSS data; no AI, events or posts")
    for name, help_text in (("readiness", "Read staging configuration and cached source status; no network"),
                            ("quality-report", "Summarize all unpublished draft ratings; no network"),
                            ("session-report", "Summarize the past rolling 24 hours; no network")):
        report = commands.add_parser(name, help=help_text)
        report.add_argument("--json", action="store_true", help="Machine-readable report")
    preview = commands.add_parser("preview", help="Print complete latest drafts; no network")
    preview.add_argument("--limit", type=int, default=10)
    rate = commands.add_parser("rate-draft", help="Record manual draft quality; no network")
    rate.add_argument("id", type=int)
    rate.add_argument("quality_status", choices=("pending", "good", "bad"))
    rate.add_argument("--note", default="")
    enqueue = commands.add_parser("enqueue", help="Store a local text/image draft without sending")
    enqueue.add_argument("--text", required=True)
    enqueue.add_argument("--image", action="append", default=[], help="Image path relative to MEDIA_ROOT; up to four")
    enqueue.add_argument("--publish-at", help="ISO-8601 timestamp with timezone")
    args = parser.parse_args(argv)
    settings, store = Settings(), Store()
    if args.command == "readiness":
        initialized = True
        try:
            db.init_db(recover_interrupted=False)
        except Exception:
            initialized = False
        report = staging_readiness(settings, store, database_initialized=initialized)
        print(redact(json.dumps(report, ensure_ascii=False))) if args.json else print_readiness(report)
        return int(not report["ready"])
    # CLI diagnostics/review must not reset a concurrently running writer's claim.
    db.init_db(recover_interrupted=False)
    if args.command in {"quality-report", "session-report"}:
        report = store.quality_report() if args.command == "quality-report" else store.session_report()
        if args.json:
            print(redact(json.dumps(report, ensure_ascii=False)))
        elif args.command == "quality-report":
            print_quality(report)
        else:
            print_session(report)
        return 0
    http = ReadOnlyHTTPClient(timeout=settings.http_timeout, attempts=settings.http_attempts)
    if args.command == "test-ai":
        states = asyncio.run(probe_ai(settings, store, http))
        for name, state in states.items():
            status = "SKIP (not configured)" if not state["configured"] else "OK" if state["reachable"] else "FAILED"
            print(f"{name.title()}: {status}; model={state['model']}")
            if state["last_error"]:
                print(f"  {state['last_error']}")
        configured = [s for s in states.values() if s["configured"]]
        return 2 if not configured else int(any(not s["reachable"] for s in configured))
    if args.command == "test-sources":
        states = asyncio.run(probe_sources(settings, store, http))
        for state in states:
            print(f"{state['source']}: {state['status']}; items={state['items']}; latency_ms={state['latency_ms']}")
            if state["last_error"]:
                print(f"  ERROR [{state['source']}]: {state['last_error']}")
        return int(any(s["status"] == "error" for s in states))
    if args.command == "collect-once":
        stats = asyncio.run(Pipeline(settings, store, http).collect_once())
        print_collection(stats)
        if not (settings.gemini_api_key or settings.groq_api_key):
            print("AI: SKIP (no configured provider); accepted events remain in SQLite")
        return int(bool(stats["errors"]))
    if args.command == "preview":
        if not 1 <= args.limit <= 200:
            parser.error("limit must be between 1 and 200")
        print_preview(store.preview(args.limit))
        return 0
    if args.command == "rate-draft":
        try:
            post = store.rate_draft(args.id, args.quality_status, args.note)
        except (ValueError, LookupError) as exc:
            parser.error(str(exc))
        print(f"Draft #{post['id']}: {post['quality_status']}")
        return 0
    if len(args.text) > settings.max_chars:
        parser.error("text exceeds WRITER_MAX_CHARS")
    try:
        from src.publisher.binance_square import BinanceSquarePublisher
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
