# Phase 1.6: VPS staging dry run

[简体中文](STAGING.md) | **English**

Use real Gemini/Groq and public data to generate Chinese drafts stored in SQLite and the dashboard.
CLI, backend, scheduler and Compose remain locked to dry run, even if AUTO_PUBLISH is overridden.
**BINANCE_SQUARE_API_KEY is ignored and not required; leave it empty.** No Square publishing or media endpoints are called.
This guide describes deployment steps; it does not claim that your VPS or real AI credentials have been tested.

## 1. Prerequisites and checkout

Prepare Git, curl, OpenSSL, Docker Engine and Compose v2.24+ on a Linux VPS.
Follow [Docker installation](https://docs.docker.com/engine/install/) and
[Compose installation](https://docs.docker.com/compose/install/). Your user needs Docker permissions.
Keep port 8080 bound to host loopback and use an SSH tunnel for the dashboard.

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

For an existing checkout, inspect local changes and run git pull --ff-only.
Keep the same directory/Compose project name so updates reuse the same bot-data volume.

## 2. Configure .env

```bash
umask 077
cp .env.example .env
# First creation only: write a random administrator token without printing it.
sed -i "s/^ADMIN_TOKEN=$/ADMIN_TOKEN=$(openssl rand -hex 32)/" .env
nano .env
```

Enter actual keys locally on the VPS. Do not put them in chat, Git or logs.
Gemini is primary and Groq is fallback. Configure at least one, preferably both, and probe them independently.
Use model names available to your accounts; repository defaults remain unchanged.
Review these fields without replacing the whole .env or deleting the generated ADMIN_TOKEN:

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

Retain a nonempty ADMIN_TOKEN. The database must be inside the mounted /app/data directory.
Start with collection disabled until connectivity and draft quality are reviewed.
The UTC daily request budget includes probes, failed attempts, retries and fallback; it is not a dollar spending limit.
HTTP_TIMEOUT_SEC bounds each attempt's total duration; connect is min(10, total/2) and idle read is total/2.
Requests have bounded timeouts/retries and diagnostics never print keys or raw responses.

## 3. Build and health

```bash
docker compose config --quiet
docker compose build
docker compose up -d --wait --wait-timeout 120
docker compose ps
curl --fail --max-time 5 http://127.0.0.1:8080/health
```

Expect status=ok, database=true, auto_publish=false, initially collect_enabled=false.
Health checks process/database liveness; it does not establish AI or market connectivity.
The non-root container stores data in a named volume. Compose forces AUTO_PUBLISH=false over .env,
and separate CLI/backend/scheduler locks prevent publishing.
Configured ADMIN_TOKEN always enables authentication; Compose also honors ALLOW_UNAUTHENTICATED_ADMIN=false.
Use config --quiet: plain config output can expose secrets.

## 4. Real probes and readiness

```bash
docker compose exec -T square-ai python -m src.cli test-ai
docker compose exec -T square-ai python -m src.cli test-sources
docker compose exec -T square-ai python -m src.cli readiness
docker compose exec -T square-ai python -m src.cli readiness --json
```

test-ai sends a tiny independent JSON request per configured provider, possibly consuming credits;
it generates no post or queue item. test-sources reads public Spot/RSS and persists diagnostics, without AI.
Authenticated /api/status/providers and /api/status/sources only read saved observations; Spot has critical=true.

readiness performs local checks and initializes/migrates SQLite on first use; it never probes the network.
It prints database, dry-run lock, provider configuration, ADMIN_TOKEN, critical Spot and individual RSS status,
Square key ignored/not required, AUTO_PUBLISH: FALSE and ready. Exit 0 means ready=true; exit 1 means false.
Readiness requires a healthy database, an active lock, at least one configured model, ADMIN_TOKEN,
and a nonempty successful latest Spot observation within READINESS_MAX_AGE_SEC (default 3600).
Missing, failed, stale or invalid/future Spot observations fail readiness; earlier success cannot mask a later failure.
Configured does not establish credential validity: run test-ai separately.
RSS sources are noncritical; even unavailable RSS allows market-only drafting when other checks pass.
test-sources/collect-once still return 1 for any source failure while preserving successful results.

Spot errors distinguish DNS, connect timeout, read timeout, TLS and HTTP status.
Exhausting the total budget is reported separately rather than guessed as a read timeout.
Check DNS, outbound port 443, certificates/system time and returned status on your VPS.
Failed market data must never be replaced with invented prices.

## 5. Drafts, preview and ratings

Keep background collection disabled while manually running collect-once.

```bash
docker compose exec -T square-ai python -m src.cli collect-once
docker compose exec -T square-ai python -m src.cli preview --limit 10
# Replace example IDs with real IDs from preview.
docker compose exec -T square-ai python -m src.cli rate-draft 123 good
docker compose exec -T square-ai python -m src.cli rate-draft 124 bad --note "开头太模板化"
docker compose exec -T square-ai python -m src.cli quality-report
docker compose exec -T square-ai python -m src.cli session-report
```

Collection prints source/pair counts, accepted/rejected/deduplicated inputs, model attempts and draft counts.
Preview shows complete text and event/source/symbol/score/provider/time metadata.
Preview, ratings and reports make no network requests. CLI checks no longer reset a background writer's active claim;
backend startup retains crash recovery. Investigate budgets, event rules, writer validation and logs if drafts are absent.

quality-report covers all unpublished drafts, including manual/review/failed/dead records.
good_rate is good/(good+bad), excluding pending; unrated is N/A (JSON null).
Bad notes are grouped after whitespace normalization, empty notes become (no note), and the top 10 are shown.
good/bad counts are grouped by event_type/source/symbol/ai_provider; absent legacy metadata is unknown.
Ratings record feedback without changing prompts or publishing. Reports support --json.

session-report covers a rolling 24 hours in UTC: collected inputs/outcomes, per-model attempts
(including retries, fallback and test-ai), and AI-generated drafts with current quality status.
Manual drafts are excluded. Collections use completion time, drafts use generation time;
ratings reflect current state rather than when a rating changed.
The additive SQLite v7 -> v8 migration preserves queue, ratings and daily budgets but cannot reconstruct earlier
rejected/deduplicated/request history. The first 24 hours show partial coverage, complete_window=false and recording_since.
test-sources does not count as event collection; local review/report commands do not count as model requests.

On your computer, replace login/address and open a tunnel:

```bash
ssh -N -L 8080:127.0.0.1:8080 user@your-vps
```

Visit http://127.0.0.1:8080 with Basic username admin and the VPS ADMIN_TOKEN as password.
Do not put tokens in URLs or open public 8080. Domain/TLS proxy configuration is a separate operation.

## 6. Continuous dry run and logs

After reviewing readiness and samples, set COLLECT_ENABLED=true in .env while keeping AUTO_PUBLISH=false.

```bash
nano .env
docker compose up -d --force-recreate --wait --wait-timeout 120
curl --fail --max-time 5 http://127.0.0.1:8080/health
docker compose logs --since=24h --tail=200 square-ai
docker compose logs -f --tail=100 square-ai
# Ctrl+C exits log streaming only.
docker compose exec -T square-ai python -m src.cli session-report
```

Collection defaults to every 15 minutes, bounded by request and draft limits. Run one backend per database.
Generation failures preserve events and use the existing backoff. restart uses the current container environment;
use up --force-recreate after .env changes. Publishing stays locked regardless of readiness.

## 7. Consistent SQLite backup

Back up before updates and regularly with SQLite's backup API. Copying a live bot.db alone can miss WAL data.
This creates and validates a separate snapshot without modifying the source:

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

Copy host snapshots to independent storage; an in-volume snapshot does not protect against VPS/disk loss.
Backups are excluded from Git and Docker build context. Do not commit .env/databases or run down -v, which removes volumes.
See [Python SQLite backup](https://docs.python.org/3/library/sqlite3.html#sqlite3.Connection.backup).

## 8. Update and restart

Complete the backup first, retain .env, and inspect local changes:

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

Reprobe sources if Spot status expired; reprobe AI after credential/model changes.
Older code rejects a newer database schema: retain backups before migration rather than reverting code against the new database.
This phase includes no production deployment, Phase 2, Futures, market-change rules or Announcement implementation.

## 9. Optional live smoke tests

The seven live tests remain opt-in; ordinary CI uses mocks and no real keys.
The runtime image excludes development dependencies/tests. Use a separate host virtual environment/database if testing on VPS:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
RUN_LIVE_TESTS=true AUTO_PUBLISH=false HOST=127.0.0.1 \
  DATABASE_URL=sqlite:///data/live-smoke.db \
  .venv/bin/python -m pytest tests/integration -q
```

Model keys load from root .env; the database is separate from staging and model fixtures further isolate databases.
Missing keys skip model cases. Each failing source fails its smoke test, including noncritical RSS;
this tests individual protocols rather than readiness's degraded-operation policy.
Tests import/call no Square publisher and may consume model credits. Keep RUN_LIVE_TESTS=false for routine operation.
