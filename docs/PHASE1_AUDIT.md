# Phase 1 仓库审计

审计日期：2026-10-07。当前聊天工作目录原为空；本次仅采用公开 MIT 项目
[SMOService/buffer-poster-bot](https://github.com/SMOService/buffer-poster-bot)，没有读取或整合其他旧项目。
上游 main：`acc720e9aae7748a029400202626eab26dc8c005`。下载归档并比对后建立本地 Git 基线。
原始 `LICENSE` 完整保留。目标参考站点无法通过当前抓取工具访问，不据此臆造功能。

## 原有结构与链路

| 模块 | 职责 / 发现 | Phase 1 处理 |
| --- | --- | --- |
| `bot.py` | 初始化 DB、强制加载 Buffer 频道、启动 Telegram polling | 保留为禁用的旧入口；Docker/Procfile 改用 `src.app` |
| `config.py` | 导入即读取必填 Telegram/Buffer 环境变量、创建 DB 目录、重置 logging | 改为兼容配置；移除必填依赖、支持 DATABASE_URL、日志脱敏 |
| `db.py` | SQLite v1–v5 迁移、队列、KV、MD5 去重、原子 claim、发布历史、TTL | 原地增量扩展 v6；保留原表和迁移，新增事件表、草稿媒体字段 |
| `scheduler.py` | 60s tick、dead letter、指数退避、quota/auth hold；依赖 Telegram 下载和通知 | 原文件保留，旧入口禁止运行；独立 scheduler 复用 DB 状态机 |
| `services/binance.py` | 官方 text/image/article/video OpenAPI；v2 上传、v1 发布 | 保留并修复安全门、HTTP 错误处理、部分图片丢失；通过 publisher 适配器使用 |
| `services/buffer.py` | Buffer GraphQL；默认 aiohttp 超时，无明确 retry | 从默认运行和容器中移除；暂保留源码供后续删除 |
| `services/uploader.py` | imgbb → catbox → 0x0.st | 从默认运行和容器中移除；新图片直接读本地持久卷、上传 Binance |
| `bot_instance.py` | 导入即创建 aiogram 单例、Telegram file_id 下载 | 从默认运行和依赖中解耦 |
| `handlers/post.py` | 转发、album 缓冲、上传图床、Buffer 发布、Binance 入队 | 保留源码，独立管道不导入 |
| `handlers/binance.py` | 队列 CRUD、send now、flush、pause；调用旧 scheduler | 保留源码，独立只读后台无发送按钮 |
| `handlers/menu/channels/queue/logs/common.py` | Telegram UI、鉴权、Buffer 查询、发布日志展示 | 保留源码，后台由 aiohttp 提供 |
| `keyboards.py` / `state.py` | aiogram 菜单和编辑状态 | 保留源码，退出默认运行 |
| Docker / Compose / Procfile | Python 3.12、单 worker、SQLite volume | 保留单进程模式，换独立入口，加 healthcheck、loopback 端口 |
| `tests/test_db.py` | 10 个 DB 测试；迁移、媒体、TTL、claim、恢复、去重、退避 | 全部保留，补充安全恢复和新模块测试 |
| `.github/workflows/ci.yml` | lint、语法、Telegram import smoke、测试、Docker build | 换独立 import smoke 和容器 health smoke |

## 风险与修复决策

- 原发布客户端没有全局 dry-run 安全门；在客户端和 scheduler 两层阻止发布。
- 原 image flow 在部分图片上传失败时仍发布成功图片，导致永久丢失其余图片；改为完整成功才发布。
- 原发布 response JSON 解析只捕获 ContentTypeError，非 JSON 可能丢失 504 语义；补齐解析和 HTTP 分类。
- 原启动恢复把 `publishing` 自动转 pending。请求可能已被 Binance 接收，重试有重复风险；新版本改为 `review`，等待人工核验。
- SQLite `with Connection` 不自动关闭连接；使用关闭连接的子类，设置 busy timeout，并在初始化开启 WAL。
- 原去重 check→enqueue→save_hash 多步存在竞态；新增事件入队以事务和唯一约束完成。
- 新链路不依赖 Telegram file_id；原数据库中携带 Telegram 媒体的条目明确拒绝自动发布，防止退化成纯文本。
- 原 `DATABASE_URL` 不存在；仅支持 SQLite file URL，旧 DB_PATH 仅作兼容 fallback。
- API 网络故障不得把密钥、签名 URL、原始服务响应写入日志；错误类型/状态码足够排障。

## 保留 / 删除 / 重构结论

保留：MIT、SQLite、原队列和日志表、Binance 官方协议、原重试策略、Docker。

删除于默认环境：aiogram 依赖、Telegram/Buffer 必填配置、外部图床、旧启动命令。
旧业务源码暂不批量删除，且 `bot.py` 被明确禁止执行；容器只复制必要模块。

增量重构：独立入口、配置、HTTP 策略、publisher 适配器、事件/草稿持久化、安全恢复。

新增：RSS / 行情 collectors、规则评分与事件去重、Gemini→Groq provider、受约束中文 writer、只读后台和 health。
Futures、Announcement 仅建立扩展接口，Phase 1 不声称实现。
