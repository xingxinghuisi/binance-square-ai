from __future__ import annotations

import asyncio
import base64
import hmac
import html
import logging
import time
from contextlib import suppress

from aiohttp import web

import db
from src.http import HTTPClient
from src.pipeline import Pipeline
from src.publisher.binance_square import BinanceSquarePublisher
from src.scheduler import Scheduler
from src.settings import Settings, publishing_enabled
from src.storage import Store

logger = logging.getLogger(__name__)
SETTINGS = web.AppKey("settings", Settings)
STORE = web.AppKey("store", Store)
WORKERS = web.AppKey("workers", dict)
TASKS = web.AppKey("tasks", list)


@web.middleware
async def admin_auth(request, handler):
    if request.path == "/health":
        return await handler(request)
    token = request.app[SETTINGS].admin_token
    if token:
        value = request.headers.get("Authorization", "")
        supplied = value.removeprefix("Bearer ") if value.startswith("Bearer ") else ""
        if value.startswith("Basic "):
            try:
                user, supplied = base64.b64decode(value[6:], validate=True).decode().split(":", 1)
                if user != "admin":
                    supplied = ""
            except (ValueError, UnicodeError):
                supplied = ""
        if not hmac.compare_digest(supplied.encode(), token.encode()):
            raise web.HTTPUnauthorized(headers={"WWW-Authenticate": 'Basic realm="Binance Square AI"'})
    return await handler(request)


def pagination(request) -> tuple[int, int]:
    try:
        limit, offset = int(request.query.get("limit", "100")), int(request.query.get("offset", "0"))
        if not 1 <= limit <= 200 or not 0 <= offset <= 1_000_000:
            raise ValueError
        return limit, offset
    except ValueError as exc:
        raise web.HTTPBadRequest(text="invalid limit/offset") from exc


async def health(request):
    database_healthy = True
    try:
        with db.get_conn() as conn:
            conn.execute("SELECT 1 FROM binance_queue LIMIT 1").fetchone()
    except Exception:
        database_healthy = False
    healthy = database_healthy
    workers = request.app[WORKERS]
    for task in request.app.get(TASKS, []):
        if task.done():
            healthy = False
    settings = request.app[SETTINGS]
    for name, interval in (("scheduler", settings.scheduler_interval), ("collector", settings.collection_interval)):
        if workers.get(name) and time.time() - workers[name]["tick_at"] > max(180, interval * 2):
            healthy = False
    if workers.get("scheduler", {}).get("state") == "error":
        healthy = False
    return web.json_response({"status": "ok" if healthy else "unhealthy", "database": database_healthy,
                              "auto_publish": publishing_enabled(), "collect_enabled": settings.collect_enabled,
                              "ai_configured": bool(settings.gemini_api_key or settings.groq_api_key),
                              "workers": workers}, status=200 if healthy else 503)


async def queue_api(request):
    return web.json_response(request.app[STORE].queue(*pagination(request)))


async def events_api(request):
    return web.json_response(request.app[STORE].events(*pagination(request)))


async def logs_api(request):
    return web.json_response(db.list_history(*pagination(request)))


async def index(request):
    esc = html.escape
    store = request.app[STORE]
    limit, offset = pagination(request)
    posts, events, logs = store.queue(limit, offset), store.events(limit, offset), db.list_history(limit, offset)
    cards = "".join(f'<article><div class="meta">#{p["id"]} · {esc(p["status"])} · 尝试 {p["attempt_count"] or 0}</div>'
                    f'<pre>{esc(p["text"])}</pre><small>媒体：{esc(p.get("image_paths") or "[]")}</small>'
                    f'<p class="error">{esc(p.get("last_error") or "")}</p></article>' for p in posts)
    event_rows = "".join(f'<tr><td>{esc(e["event"]["source"])}</td><td>{esc(e["event"]["type"])}</td>'
                         f'<td>{esc(e["event"].get("symbol") or "—")}</td><td>{e["event"]["score"]}</td>'
                         f'<td>{esc(e["status"])}</td><td>{esc(e.get("error") or "")}</td></tr>' for e in events)
    log_rows = "".join(f'<tr><td>{log["id"]}</td><td>{esc(log["kind"])}</td><td>{esc(log["status"])}</td>'
                       f'<td>{esc(log.get("text_preview") or "")}</td><td>{esc(log.get("error") or "")}</td></tr>' for log in logs)
    mode = "自动发布开启" if publishing_enabled() else "DRY RUN · 真实发布关闭"
    body = f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Binance Square AI · Phase 1</title><style>
body{{background:#101317;color:#edf0f4;font:16px system-ui;margin:0}}main{{max-width:1080px;margin:auto;padding:28px}}
header{{display:flex;justify-content:space-between;align-items:center;gap:20px;flex-wrap:wrap}}h1{{font-size:26px}}
.badge{{background:#332d15;color:#f0b90b;padding:10px 16px;border-radius:8px}}a{{color:#f0b90b}}
article{{background:#1b2028;border:1px solid #303744;border-radius:12px;padding:20px;margin:16px 0}}
pre{{white-space:pre-wrap;overflow-wrap:anywhere;font:16px/1.8 system-ui}}.meta,small{{color:#a5afbd}}
.error{{color:#f49a9a}}table{{width:100%;border-collapse:collapse;font-size:14px}}td,th{{text-align:left;border-bottom:1px solid #303744;padding:12px;overflow-wrap:anywhere}}
.scroll{{overflow-x:auto}}nav{{margin:24px 0}}h2{{margin-top:38px}}</style><main>
<header><h1>Binance Square AI</h1><span class="badge">{mode}</span></header>
<p>Phase 1 · 采集 → 事件 → 中文草稿 → SQLite 队列</p><nav><a href="#queue">帖子队列</a> · <a href="#events">事件</a> · <a href="#logs">日志</a> · <a href="/health">健康状态</a></nav>
<p>后台只读。刷新页面查看最新草稿；没有真实发布按钮。</p>
<h2 id="queue">帖子队列</h2>{cards or '<p>尚无草稿。配置模型后等待采集，或使用本地 CLI 将图文入队。</p>'}
<h2 id="events">事件记录</h2><div class="scroll"><table><tr><th>来源</th><th>类型</th><th>币种</th><th>评分</th><th>状态</th><th>错误</th></tr>{event_rows}</table></div>
<h2 id="logs">准备 / 发布日志</h2><div class="scroll"><table><tr><th>ID</th><th>类别</th><th>状态</th><th>内容</th><th>错误</th></tr>{log_rows}</table></div>
<nav><a href="/?offset={max(0, offset-limit)}&limit={limit}">上一页</a> · <a href="/?offset={offset+limit}&limit={limit}">下一页</a></nav>
</main></html>'''
    return web.Response(text=body, content_type="text/html", headers={"Cache-Control": "no-store",
                        "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'",
                        "X-Content-Type-Options": "nosniff"})


async def background(app):
    settings, store = app[SETTINGS], app[STORE]
    http = HTTPClient(timeout=settings.http_timeout, attempts=settings.http_attempts)
    pipeline = Pipeline(settings, store, http)
    scheduler = Scheduler(store, BinanceSquarePublisher(settings.media_root))

    async def loop(name, interval, action):
        while True:
            app[WORKERS][name] = {"tick_at": int(time.time()), "state": "running"}
            try:
                result = await action()
                app[WORKERS][name].update(state="idle", result=result)
            except Exception as exc:
                app[WORKERS][name].update(state="error", error=type(exc).__name__)
                logger.error("Worker %s failed (%s)", name, type(exc).__name__)
            await asyncio.sleep(interval)

    app[TASKS] = [asyncio.create_task(loop("scheduler", settings.scheduler_interval, scheduler.tick))]
    if settings.collect_enabled:
        app[TASKS].append(asyncio.create_task(loop("collector", settings.collection_interval, pipeline.collect_once)))
    yield
    for task in app[TASKS]:
        task.cancel()
    for task in app[TASKS]:
        with suppress(asyncio.CancelledError):
            await task


def create_app(settings: Settings | None = None, *, start_workers: bool = True) -> web.Application:
    settings = settings or Settings()
    db.init_db()
    app = web.Application(middlewares=[admin_auth])
    app[SETTINGS], app[STORE], app[WORKERS], app[TASKS] = settings, Store(), {}, []
    app.router.add_get("/health", health)
    app.router.add_get("/", index)
    app.router.add_get("/api/queue", queue_api)
    app.router.add_get("/api/events", events_api)
    app.router.add_get("/api/logs", logs_api)
    if start_workers:
        app.cleanup_ctx.append(background)
    return app


def main():
    settings = Settings()
    logger.info("Starting Phase 1 backend; auto_publish=%s", publishing_enabled())
    web.run_app(create_app(settings), host=settings.host, port=settings.port, access_log=None)


if __name__ == "__main__":
    main()
