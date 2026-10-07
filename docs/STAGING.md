# Phase 1.6：VPS Staging Dry Run

**简体中文** | [English](STAGING.en.md)

本阶段使用真实 Gemini/Groq 和真实公开数据生成中文草稿，仅保存到 SQLite 和后台。
CLI、后台、scheduler 和 Docker Compose 保持禁发；即使环境误设 true，也不会开启发布。
**不需要配置 `BINANCE_SQUARE_API_KEY`，请留空。** 不调用 Square 的 content/add、image/presignedUrl 或其他发布/媒体接口。
以下是部署操作说明，不表示项目已在你的 VPS 上部署或完成真实 AI 验证。

## 1. VPS 前提与代码

使用 Linux VPS，准备 Git、curl、OpenSSL、Docker Engine 和 Docker Compose v2.24+。
Docker 安装以[官方安装说明](https://docs.docker.com/engine/install/)和
[Compose 插件说明](https://docs.docker.com/compose/install/)为准。运行用户需要有 Docker 权限。
保留宿主机 loopback 端口，不开放公网 8080；本阶段通过 SSH 隧道访问后台。

```bash
mkdir -p ~/staging
cd ~/staging
git clone https://github.com/xingxinghuisi/binance-square-ai.git
cd binance-square-ai
git switch main
git log -1 --oneline
docker version
docker compose version
```

已有 checkout 则进入同一目录，先检查本地改动，再执行 `git pull --ff-only`。
部署、更新和备份都在该目录执行，保持 Compose 项目名称不变，以继续使用同一个 `bot-data` volume。

## 2. 创建 .env 与配置模型

```bash
umask 077
cp .env.example .env
# 只在首次创建后执行；随机管理员 token 直接写入文件，不打印出来。
sed -i "s/^ADMIN_TOKEN=$/ADMIN_TOKEN=$(openssl rand -hex 32)/" .env
nano .env
```

在 VPS 本地编辑实际 Key，不把密钥发到聊天、写进 Git 或粘贴到日志。
Gemini 是主模型，Groq 是备用；至少配置一个，建议两个都配置并分别执行 test-ai。
模型名称使用账户可访问的模型；仓库默认值保留不变。本阶段不要设置 Square Key。

关键配置如下（Key 在本地编辑，下面不提供真实 Key）：

```dotenv
GEMINI_API_KEY=
GEMINI_MODEL=gemini-flash-latest
GROQ_API_KEY=
GROQ_MODEL=llama-3.3-70b-versatile
BINANCE_SQUARE_API_KEY=
AUTO_PUBLISH=false
DATABASE_URL=sqlite:////app/data/bot.db
ALLOW_UNAUTHENTICATED_ADMIN=false
COLLECT_ENABLED=false
PORT=8080
READINESS_MAX_AGE_SEC=3600
RUN_LIVE_TESTS=false
MAX_DRAFTS_PER_CYCLE=3
MAX_DRAFTS_PER_DAY=10
MAX_AI_REQUESTS_PER_DAY=40
```

保留刚生成的非空 `ADMIN_TOKEN`。不要用上述片段覆盖整个 .env；它是需要核对的字段。
`DATABASE_URL` 必须落在挂载的 `/app/data`，不要指向容器临时目录。
初次设 `COLLECT_ENABLED=false`，先人工确认连通性和草稿质量。
日请求预算按 UTC 计数，包含 test-ai、失败请求、retry 和 fallback；它是请求上限，不是美元费用保证。
`HTTP_TIMEOUT_SEC` 是单次请求总时间上限；连接上限为 min(10, 总上限/2)，读取空闲上限为总上限/2。
所有外部请求均有有界 timeout/retry，诊断不打印 Key 或原始响应。

## 3. 构建、启动与 health

```bash
# 只校验，不把含 Key 的完整 Compose 配置打印到终端。
docker compose config --quiet
docker compose build
docker compose up -d --wait --wait-timeout 120
docker compose ps
curl --fail --max-time 5 http://127.0.0.1:8080/health
```

health 应为 status=ok、database=true、auto_publish=false；初始 collect_enabled=false。
health 是进程/DB 存活检查，并不代表模型与行情可用。容器以非 root 用户运行，数据在 named volume。
Compose 的 environment 中 `AUTO_PUBLISH=false` 覆盖 .env；CLI 和后台另有强制锁，scheduler 也锁定 dry run。
ADMIN_TOKEN 非空时后台始终鉴权；Compose 现在也尊重 .env 的 `ALLOW_UNAUTHENTICATED_ADMIN=false`。

## 4. 真实连通性检查与 readiness

```bash
docker compose exec -T square-ai python -m src.cli test-ai
docker compose exec -T square-ai python -m src.cli test-sources
docker compose exec -T square-ai python -m src.cli readiness
docker compose exec -T square-ai python -m src.cli readiness --json
```

test-ai 分别发送最小模型 JSON 请求，不写帖子或入队，可能消耗额度；缺 Key 显示 not configured。
test-sources 只读取公开 Spot/RSS，保存诊断，不调用 AI。源超时和失败都会指出名称。
`/api/status/providers` 与 `/api/status/sources` 沿用后台鉴权，只读取这些记录，不触发新请求；Spot 标注 critical=true。

readiness 仅读取配置与历史探测，不发网络请求；首次运行会初始化/增量迁移 DB。
输出 Database、Dry Run Lock、Gemini/Groq 配置、ADMIN_TOKEN、关键 Spot 状态、各 RSS 状态、Square Key ignored/not required、AUTO_PUBLISH: FALSE 和 ready。
返回 0 表示 ready=true，返回 1 表示 ready=false。需要满足：

- DB 检查成功、禁发锁有效。
- 至少配置一个模型，配置非空 ADMIN_TOKEN。
- Binance Spot 最近一次探测成功、返回非空行情，且距离现在不超过 READINESS_MAX_AGE_SEC。

Spot 未探测、最近失败、过期或时间戳异常都返回 ready=false；之后失败不会沿用更早的成功结果。
readiness 不验证 Key 的实际有效性，仍需 test-ai。模型字段 configured 不等于真实调用成功。
RSS 是非关键数据源，CoinDesk 失败或全部 RSS 不可用不会阻止仅行情草稿的 readiness。
test-sources/collect-once 遇到任何源失败会退出 1，但保留成功源的结果；这不等于服务进程无法运行。

Spot 错误类别：DNS lookup failed、connect timeout、read timeout、TLS handshake/certificate failed、HTTP 状态码。
总时间预算耗尽会单独显示 request timeout (total deadline)，不会把未知阶段误报成 read timeout。
检查 VPS 的 DNS、443 出站连接、系统时间/证书及接口返回状态；不要用猜测价格替代失败行情。

## 5. 生成草稿、Preview 与人工评分

保持后台 COLLECT_ENABLED=false，避免后台采集与手动 collect-once 同时运行。

```bash
docker compose exec -T square-ai python -m src.cli collect-once
docker compose exec -T square-ai python -m src.cli preview --limit 10
# 用 preview 中的实际草稿 ID 替换 123 / 124。
docker compose exec -T square-ai python -m src.cli rate-draft 123 good
docker compose exec -T square-ai python -m src.cli rate-draft 124 bad --note "开头太模板化"
docker compose exec -T square-ai python -m src.cli quality-report
docker compose exec -T square-ai python -m src.cli session-report
```

collect-once 输出 RSS/交易对、received/accepted/rejected/deduplicated、实际模型尝试数和草稿数。
Preview 包含类型、来源、币种、score、模型、生成时间和完整正文；它和评分/报告都不调用网络。
CLI 诊断不会重置后台正在 writing 的事件；只有后台启动继续执行崩溃恢复。
没有草稿时检查模型诊断、日预算、事件时效/分数、Writer 验证失败及日志；不会为了生成草稿放松内容校验。

quality-report 针对全部未发布草稿（含手工、review、failed、dead）：total/pending/good/bad；
good_rate=good/(good+bad)，未评分不进入分母，无评分时 N/A（JSON 为 null）。
bad reasons 合并 note 前后/重复空白，未填写记为 (no note)，显示最多 10 项；
按 event_type/source/symbol/ai_provider 分组 good/bad，旧记录缺元数据记为 unknown。
评分仅保存人工反馈，不修改 Prompt，不发布。`--json` 可用于外部报告脚本。

session-report 是滚动过去 24 小时（UTC），包含采集输入、accepted/rejected/deduplicated、
Gemini/Groq HTTP 尝试数（含 retry、fallback、test-ai）、AI 生成草稿及当前 good/bad/pending。
手工草稿不计入 AI 生成数；采集按完成时间计入窗口，草稿按生成时间，评分是当前状态而非评分发生时间。
SQLite v7 -> v8 只新增采集/AI 尝试记录，保留队列、质量与日预算；不补造旧 rejected/dedup/request 历史。
因此升级后前 24 小时报告显示 Coverage: partial / complete_window=false 和 recording_since。
端点 test-sources 不算事件采集，Preview/评分/报告不计入模型请求。

后台访问：在你自己的电脑执行 SSH 隧道，替换登录名与 VPS 地址：

```bash
ssh -N -L 8080:127.0.0.1:8080 user@your-vps
```

浏览器打开 http://127.0.0.1:8080，Basic 用户名 admin，密码为 VPS .env 中的 ADMIN_TOKEN。
不要把 token 放 URL 或开放公网 8080。域名/TLS 反代可另行配置，本轮不自动修改域名。

## 6. 持续 dry run、日志与重启

人工确认 readiness 和样稿后，在 .env 改 COLLECT_ENABLED=true；AUTO_PUBLISH 仍保持 false。

```bash
nano .env
docker compose up -d --force-recreate --wait --wait-timeout 120
curl --fail --max-time 5 http://127.0.0.1:8080/health
docker compose logs --since=24h --tail=200 square-ai
docker compose logs -f --tail=100 square-ai
# Ctrl+C 仅退出 logs -f。
docker compose exec -T square-ai python -m src.cli session-report
```

每 15 分钟采集（可配置），受日预算、每日/每轮草稿上限控制。只运行一个共享该 SQLite 的后台。
模型生成校验失败会保留失败事件并按既有退避策略重试。
`docker compose restart square-ai` 只重启当前配置；修改 .env 后使用 up --force-recreate 才会加载新环境。
禁发锁不依赖 readiness=true；不就绪也不会发布。

## 7. SQLite 一致性备份

更新前和定期使用 SQLite backup API；运行中直接复制 bot.db 可能遗漏 WAL 数据。
以下在容器内部创建独立、一致的 .db 文件并 quick_check，不修改源 DB：

```bash
docker compose exec -T square-ai python - <<'PY'
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import db

os.umask(0o077)
folder = Path('/app/data/backups')
folder.mkdir(mode=0o700, exist_ok=True)
target = folder / ('bot-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ') + '.db')
source_uri = db.DB_PATH.resolve().as_uri() + '?mode=ro'
with closing(sqlite3.connect(source_uri, uri=True, timeout=5)) as source:
    with closing(sqlite3.connect(target)) as backup:
        source.backup(backup)
        assert backup.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
print('Backup:', target)
PY
mkdir -p backups
chmod 700 backups
docker compose cp square-ai:/app/data/backups/. ./backups/
chmod 600 backups/*.db
```

将主机 backups 中的快照另存到独立存储；不要提交 .env 或 DB。备份目录已加入 Git/Docker ignore。
同一个 volume 内的快照不能防范 VPS/磁盘丢失。不要执行 `docker compose down -v`，它会删除数据 volume。
备份原理见 [Python SQLite backup API](https://docs.python.org/3/library/sqlite3.html#sqlite3.Connection.backup)。

## 8. 更新代码

先完成上述备份，保留本地 .env；确认工作树改动后更新：

```bash
git status --short
git pull --ff-only
docker compose config --quiet
docker compose build
docker compose up -d --force-recreate --wait --wait-timeout 120
curl --fail --max-time 5 http://127.0.0.1:8080/health
docker compose exec -T square-ai python -m src.cli readiness
docker compose exec -T square-ai python -m src.cli preview --limit 10
docker compose logs --tail=100 square-ai
```

若旧 Spot 检测已过期，重新 test-sources 后再检查 readiness；修改模型/Key 后重新 test-ai。
升级前必须保留快照；旧版本会拒绝更新后的更高 schema，不可仅回退代码就继续使用新 DB。
不自动部署生产，不进入 Phase 2、Futures、行情异动或 Announcement。

## 9. 可选 live smoke tests

现有 7 项 live tests 保留，只在你显式开启时执行；普通 CI 用 mock，不注入真实 Key。
运行容器没有测试/开发依赖，若要在 VPS 跑测试，使用独立主机 venv 和临时数据库：

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
RUN_LIVE_TESTS=true AUTO_PUBLISH=false HOST=127.0.0.1 \
  DATABASE_URL=sqlite:///data/live-smoke.db \
  .venv/bin/python -m pytest tests/integration -q
```

项目根 .env 的模型 Key 会加载；不共用 staging DB，测试 fixture 还会隔离模型探测 DB。
缺少模型 Key 跳过对应模型；数据源失败使对应 smoke 失败，包括非关键 RSS。
它是逐源协议检查，与 readiness 的“Spot 关键、RSS 可降级”策略不同。
测试不会导入或调用 Square publisher；可能消耗真实模型额度。结束后日常保持 RUN_LIVE_TESTS=false。
