# Phase 1 实际验证记录

日期：2026-10-07（Asia/Shanghai）。代码基线：`SMOService/buffer-poster-bot` main
`acc720e9aae7748a029400202626eab26dc8c005`，已从下载 ZIP 的 commit comment 再次核对。

## 已执行且通过

| 检查 | 实际结果 |
| --- | --- |
| SQLite / 原队列回归 | 11 passed；原有 10 项保留，其中崩溃恢复改为 review；新增未来 schema 拒绝 |
| Provider / HTTP | 5 passed；Gemini 失败切 Groq、无 Key、timeout、retry、response limit、密钥脱敏 |
| Collectors / Event Engine | 5 passed；RSS schema、日期/HTML/链接、故障隔离、行情字段、时效、评分、跨源去重、原子入队 |
| Writer | 9 passed；真实数值、标签、中文、可配 Prompt/长度、重复开头重写、假行情与坏 JSON 拒绝 |
| Publisher / Scheduler | 10 passed；dry-run 阻止请求、图文适配、媒体保留、路径边界、状态机、指数退避 |
| Backend / Pipeline | 6 passed；完整事件→草稿→DB、日草稿上限、重启、health、鉴权、XSS、独立启动 |
| Safety / API contracts | 8 passed；并发去重、持久预算、重试预算、官方文本/图片协议 mock、未知结果不重发、dead/pause、配置/许可证 |
| 全量 pytest | **54 passed，0 failures，0 warnings**（本次末轮 4.90s） |
| Ruff | `ruff check .`：All checks passed |
| 语法 | `python -m compileall -q src config.py db.py services/binance.py`：通过 |
| Diff | `git diff --check`：通过，无 whitespace errors |

每个独立模块完成后运行了对应测试；发现 Writer 中文百分比漏检后已修复并重跑。
再次审查补充模型请求预算（涵盖 HTTP retry/fallback）、跨源常用词币种误判、未来 DB schema 保护和健康状态区分。

## 本地进程实启

在临时工作目录模拟 Dockerfile 的 COPY 布局，只复制 config/db、src、prompts 和 Binance service，
不复制 Telegram handlers / Buffer / uploader。

随后真实启动 `python -m src.app`，删除 Telegram、Buffer、Gemini、Groq、Binance Key 等环境变量，
固定 `AUTO_PUBLISH=false`、`COLLECT_ENABLED=false`：

- `/health` 返回 200：status=ok、database=true、auto_publish=false、ai_configured=false。
- 独立 scheduler 已运行，状态 idle。
- 独立 CLI 成功插入草稿；`/api/queue` 和 HTML 后台均可查看。
- 草稿为 pending，未调用外部发布；验证结束后关闭本地测试进程。

此项是实际 Python 进程与接口测试；**不是 Docker Engine 实启测试**。

## 免费 RSS / 行情网络检查

使用公开、无 Key 的 GET 请求：

- Cointelegraph：HTTP 200，下载的真实 RSS 可解析 30 条（collector 单源上限）。
- Decrypt：HTTP 200，可解析 30 条。
- The Block：HTTP 200，可解析 19 条。
- CoinDesk：当前网络连接超时；系统已有有界 retry、单源故障隔离与采集日志。
- Binance Spot ticker：当前网络连接超时；字段依据官方文档与 fixture/mock 合同验证，未声称已完成实时 API 联调。

可访问性是本次网络观测，不是对 VPS 网络和未来 RSS 端点的保证。

## 已核验远程 CI

- **Phase 1 远程 CI 已实际执行且成功**：commit `9dd457a69691dae1f4c2228b12f275f8f6f9a78d`，
  [run 37589905304](https://github.com/xingxinghuisi/binance-square-ai/actions/runs/37589905304)；pytest、Ruff、compileall、独立 import、Compose build/up/health 均通过。

## 未执行的验证

- 当前机器无 Docker CLI/Engine。Compose YAML、volume、port、dry-run、healthcheck 和 Docker COPY 布局已静态/模拟验证；
  **docker compose up --build 的真实容器启动未在本机执行**。

- 未使用真实 Gemini/Groq Key，未调用真实模型。模型适配器使用官方 REST 协议和 mock response 验证。
- 未使用真实 Binance Square Key，未创建真实帖子或上传真实 Binance 媒体；官方发布协议通过本地 mock HTTP 服务验证。
- 未连接 VPS、未更改域名、未部署生产环境、未启用真实自动发帖。

## 许可证 / 交付安全

`LICENSE` 与上游归档逐字节一致。API Key 示例留空，测试使用显式 fake/mock 值。
发行 ZIP 排除 `.git`、`.env`、数据库、cache、虚拟环境和测试运行目录。
原 Git 基线保留在源码目录内，交付增量 patch 可对准确上游版本审查或应用。


## Phase 1.5 实际验证（2026-10-07）

- 保留全部 Phase 1 测试；全量 **70 passed、7 skipped**，Ruff、compileall 通过。
- 新增迁移/质量/元数据持久化、独立探测、HTTP 重试按模型计数和日预算、只读状态不发请求、
  来源统计、鉴权/跨域/JSON 写保护、Preview/XSS、环境误设 true 仍不发布，以及禁发端点和重定向隔离验证。
- 本地真实 Python 进程（模拟 Docker COPY 布局）：/health、状态接口、质量写 API、CLI Preview、
  手动入队与队列展示通过；启动环境 AUTO_PUBLISH=true 被强制置 false。无外部发布。
- 实启脚本首次复用了 Phase 1 临时 DB，正确触发重复草稿拒绝；改用隔离的新 DB 后实启通过。
- 真实 `test-sources`：Cointelegraph 30 条、Decrypt 30 条、The Block 19 条；
  CoinDesk、Binance Spot timeout/connection/TLS error，记录 source/latency/items；退出 1 表示部分源失败。
- 真实 `collect-once`：received=79、accepted=54、deduplicated=0、rejected=25；
  Gemini requests=0、Groq requests=0、drafts=0（当前未配置模型）。成功数据保存为事件，未发布。
- `test-ai`：Gemini/Groq 均明确 SKIP，因当前无 API Key，退出 2。**未声称真实 AI 验证通过**。
- 普通全量测试默认跳过全部 7 项 live cases；未开启 RUN_LIVE_TESTS，未消耗真实模型额度。
- 本机 `docker compose build/up/ps` 因找不到 Docker 命令而未执行容器，
  **Phase 1.5 真实容器验证仍待新提交推送后的 CI**。Phase 1 成功不替代 Phase 1.5 结果。
- CI 显式 RUN_LIVE_TESTS=false、AUTO_PUBLISH=false、真实 Key 留空；新增测试继续用 mock。
- 未连接 VPS、未部署生产环境，未调用 content/add、image/presignedUrl 或其他真实发布接口。
