# Deployment

The production Compose stack contains PostgreSQL with pgvector installed, Redis, a one-shot Alembic migration service, the FastAPI API, one main ARQ worker, one chat ARQ worker, and the Next.js frontend. Application containers run as non-root users. Persistent database and Redis data live in named volumes.

The web port binds to loopback by default. For remote access, terminate HTTPS at a trusted reverse proxy and set `PUBLIC_ORIGIN` to the exact browser origin and `SECURE_COOKIES=true` in `.env`. Keep the API, Redis, PostgreSQL, and Docker socket private: `docker-compose.base.yml` publishes PostgreSQL, Redis, graph, n8n and RustFS on `127.0.0.1` only (used by host tooling such as `scripts.account_deletion`), so anyone with a shell on the host can reach them; never change those bindings to `0.0.0.0`. Do not deploy with the example placeholder values. `REDISCLI_AUTH` and a password in `REDIS_URL` are required: outside explicit `ENVIRONMENT=development|test` the services refuse to start with a password-less or placeholder Redis URL. HSTS is sent by the TLS terminator only (Caddy overlay sets it; with a tunnel set it at the edge); the Next.js app does not emit it.

**Modes (`ENVIRONMENT`):** `auto` (default) runs in *development* when `PUBLIC_ORIGIN` is `localhost`, a loopback/LAN IP (10.x, 172.16-31.x, 192.168.x), `*.local` or a single-label host, and in *production* for any public domain. Local `auto` accepts plain http and non-Secure cookies and serves `/docs`; placeholder secrets and `bbd:bbd` are rejected unless `ENVIRONMENT=development` is set explicitly. Production requires an `https://` origin with `SECURE_COOKIES=true` for non-local hosts, rejects `replace-with-*` placeholders and `bbd:bbd` database credentials, and disables `/docs`. Set `development` or `production` explicitly to override `auto`.

## Resource bounds

| Variable | Default | Effect |
|---|---|---|
| `DB_POOL_SIZE` / `DB_MAX_OVERFLOW` | `10` / `10` | API SQLAlchemy pool per process (pool timeout 5 s, recycle 1800 s). Size total connections across API processes, workers and admin below PostgreSQL `max_connections`. |
| `DB_STATEMENT_TIMEOUT_MS` | `60000` | API and chat-worker `statement_timeout` (also bounds lock waits). `0` disables. The chat-worker pool is fixed at 10 + 10 (not affected by `DB_POOL_SIZE` / `DB_MAX_OVERFLOW`). |
| `DB_WORKER_STATEMENT_TIMEOUT_MS` / `DB_MODEL_WORKER_STATEMENT_TIMEOUT_MS` | `120000` / `120000` | `statement_timeout` of the main worker and the model worker. Both used to run unbounded. Parsing, embedding and provider/model calls happen outside DB statements (jobs hold a session, never a long statement), so 120 s only stops a runaway query from pinning a slot. The timeout also bounds blocking lock waits (`pg_advisory_xact_lock`, row locks) in worker paths such as news catch-up, search generation, document identity and sources; a wait longer than 120 s fails the job, which is retried. `0` disables. |
| `PGBOUNCER_ENABLED` / `DATABASE_DIRECT_URL` / `DB_DIRECT_POOL_SIZE` | `false` / unset / `4` | PgBouncer mode: `DATABASE_URL` points at the pooler, `DATABASE_DIRECT_URL` at PostgreSQL (required when enabled; used by migrations and session advisory leases). `DB_DIRECT_POOL_SIZE` bounds the API's direct lease pool (the worker and cron-worker use their `max_jobs`). See "PgBouncer and horizontal scaling". |
| `REDIS_SENTINELS` / `REDIS_MASTER_NAME` / `REDIS_SENTINEL_PASSWORD` | unset / `mymaster` / unset | Redis Sentinel HA: `REDIS_SENTINELS=s1:26379,s2:26379,s3:26379` makes the API and every ARQ worker resolve the master through Sentinel. `REDIS_URL` still supplies the password and database (its host is ignored). |
| `BACKGROUND_LLM_TIMEOUT_SECONDS` / `MODEL_WORKER_JOB_TIMEOUT_SECONDS` | `60` / `900` | Per-call model timeout for background entity/timeline extraction (work leases are this plus 75 s, at least 110 s), and the model-worker arq job timeout. |
| `SEARCH_EMBED_BATCH_SIZE` | `64` | Chunks per embedding request in search indexing (1-128). `1` restores one request per chunk. Each batch is claimed in one keyset query, revalidated and published in one transaction. |
| `DB_IDLE_TX_TIMEOUT_MS` | `240000` | `idle_in_transaction_session_timeout` for every role (API, worker, cron-worker, chat-worker, model-worker); a backstop above the longest legitimate send-fence hold. `0` disables. |
| `MAX_REQUEST_BODY_BYTES` | `5242880` | Request body cap returning 413; `POST /api/v1/documents/upload` allows `UPLOAD_MAX_BYTES` plus 1 MiB. |
| `WEB_CONCURRENCY` | `2` (image `ENV`) | API process count. Above 1 (also `UVICORN_WORKERS` > 1) the API refuses to start unless `CSRF_SIGNING_SECRET` is set, so every process signs sessions with the same secret. |
| `PARSED_TEXT_MAX_CHARS` | `10485760` | Cap on parsed document text. Text past it is truncated, a `parsed_text_truncated` warning is recorded, and the document is shown with a notice (only the first part is searchable). |
| `AUTH_TRUST_FORWARDED_FOR` | `false` | Key the per-IP auth rate limit on the rightmost `X-Forwarded-For` entry. Default `false` keys on the TCP peer (the web container, so effectively one shared 5/min bucket per action). A public (non-local) production `PUBLIC_ORIGIN` makes the API refuse to start unless this is `true` or `AUTH_CLIENT_IP_HEADER` is set; the Caddy overlay sets it for you. Set `true` only behind the front proxy configuration in "Client IP and `X-Forwarded-For`" below.
| `AUTH_CLIENT_IP_HEADER` | unset | Name of a header carrying the real client IP written by a trusted front hop (for example `CF-Connecting-IP` behind Cloudflare Tunnel). Alternative to `AUTH_TRUST_FORWARDED_FOR`; the last comma entry is used. |

**`WEB_CONCURRENCY` is the only supported way to set the API process count.** uvicorn reads it as the `--workers` default, and `create_app()` reads it to enforce the shared-secret guard. Running `uvicorn --workers N` with `WEB_CONCURRENCY` unset (or `1`) starts N processes that bypass the guard and sign sessions with different random secrets; do not pass `--workers`. The production image `CMD` omits it on purpose.

## Recommended values: 2 vCPU / 8 GiB host

| Service | Memory limit / reservation | CPU shares | Key settings |
|---|---|---|---|
| postgres | 2.5 GiB / 1 GiB (`shm_size` 1 GiB) | 768 | `max_connections=120 shared_buffers=1GB effective_cache_size=2GB work_mem=16MB maintenance_work_mem=256MB` |
| redis | 640 MiB / 64 MiB | 128 | `maxmemory 512mb`, `noeviction` (never drop queued ARQ jobs; writes fail loudly instead) |
| api | 1 GiB / 512 MiB | 1024 | `WEB_CONCURRENCY=2`, uvicorn `asyncio` + `httptools` via `apps.api.protocol:NoDelayHttpToolsProtocol` (sets TCP_NODELAY; pre-bound multi-worker sockets lack it) (no `--proxy-headers`: uvicorn never takes the client address from request headers), keep-alive 15 s, graceful shutdown 8 s, `--limit-concurrency 400`; `stop_grace_period` 15 s |
| worker | 1 GiB / 256 MiB | 512 | `max_jobs=6`, DB pool 7 + 10 |
| chat-worker | 768 MiB / 256 MiB | 512 | `max_jobs=15`, `job_timeout=600`, DB pool 10 + 10 |
| cron-worker | 512 MiB / 192 MiB | 512 | queue `arq:cron`, `max_jobs=8`, `job_timeout=180`, DB pool 9 + 5; runs every recurring job (pollers, recovery, retention) so they never take ingestion `max_jobs` slots; each job is lease-guarded, extra replicas are safe |
| model-worker | 768 MiB / 256 MiB | 512 | queue `arq:model`, `max_jobs=4`, `job_timeout=MODEL_WORKER_JOB_TIMEOUT_SECONDS` (900), DB pool 5 + 5; runs entity/timeline extraction and translation so remote model calls never hold main-worker slots or the heavy slot |
| web | 768 MiB / 256 MiB | 512 | `NODE_OPTIONS=--max-old-space-size=512`, `experimental.proxyTimeout=210000` (above the worst-case brief: lease wait + `request_timeout_seconds` up to 180 s; keep it larger than that setting) |
| migrate | 512 MiB (one-shot, exits before the others start) | - | - |

Memory limits sum to 7.66 GiB (2.5 + 0.375 + 1 + 1 + 0.5 + 0.75 + 0.75 + 0.75), leaving about 0.34 GiB for the OS and page cache. The optional connectors overlay adds limits of 768 MiB + 1.5 GiB (2.25 GiB), so with it the limits total about 9.4 GiB, above 8 GiB: they are caps, not reservations, and the stack relies on actual RSS (about 4.2 GiB steady, plus connectors) staying below RAM. CPU shares apply only under contention (no hard CPU caps).

Connection budget without PgBouncer (`max_connections=120`, raised from 100 when the cron worker was split out; see "PgBouncer and horizontal scaling" for N replicas): API 2 x (10 + 10) = 40, main worker 7 + 10 = 17, cron-worker 9 + 5 = 14, chat-worker 10 + 10 = 20, model-worker 5 + 5 = 10, chat SQL reader pool (login role `umwelt_chat_sql_login`, `pool_size=2`, no overflow, role `CONNECTION LIMIT 8`) 2 per API process and 2 for the chat-worker = 6, total 107. The main worker also opens one non-pooled agent checkpoint connection per running agent run (at most 6, so worker worst case 23); plus migrate, backup and admin about 5 gives a worst case of about 118, leaving about 2 spare. The reader role limit of 8 allows four processes with a reader pool (three today): a second API replica exceeds it, so raise the role's `CONNECTION LIMIT` before scaling. A second cron-worker replica adds 14 more; recompute before scaling. If you change `WEB_CONCURRENCY`, `DB_POOL_SIZE` or `DB_MAX_OVERFLOW`, recompute this before applying.

Redis clients use `socket_timeout=5`, `socket_connect_timeout=2`, `health_check_interval=30` and at most 100 connections. Each API process allows 32 concurrent Realtime SSE streams and 64 concurrent chat response streams (503 beyond). No global `lock_timeout` is set because it would turn waits on the Memory privacy lock into errors.

## PgBouncer and horizontal scaling

### Connection budget

PostgreSQL has `max_connections=120`. Without a pooler every process owns its SQLAlchemy pool, so the budget grows with the replica count:

| Role | Per replica (pool + overflow) |
|---|---|
| API | `WEB_CONCURRENCY x (DB_POOL_SIZE + DB_MAX_OVERFLOW + 2 reader)` = 2 x 22 = 44 |
| worker | 7 + 10 = 17, plus up to 6 direct connections (agent checkpoints and leases) = 23 |
| cron-worker | 9 + 5 = 14 |
| chat-worker | 10 + 10 + 2 reader = 22 |
| model-worker | 5 + 5 = 10 |

Direct, with N API replicas, W worker replicas and C cron-worker replicas (one chat-worker and one model-worker): `44N + 23W + 14C + 22 + 10 + 5` (admin/migrate). One of each is 118 and fits; **a second API replica (162), a second worker replica (141) or a second cron-worker replica (132) exceeds `max_connections`** (and a second API replica also exceeds the reader role's limit of 8). This is why the stack ships as a single replica of each service.

With the PgBouncer overlay (`docker compose -f docker-compose.yml -f docker-compose.pgbouncer.yml up -d`) the application pools become *client* pools. PostgreSQL only sees the pooler's server connections, which are fixed per database alias and do not grow with replicas:

| Consumer | Server connections |
|---|---|
| PgBouncer aliases `bbd_api` / `bbd_worker` / `bbd_cron` / `bbd_chat` / `bbd_model` | 20 / 12 / 10 / 12 / 6 = 60 (`PGBOUNCER_POOL_*`, per PgBouncer instance; `bbd_cron` uses the worker statement timeout) |
| Direct connections | worker: up to `max_jobs` (6) per worker replica for session leases and agent checkpoint connections (the LangGraph saver uses `DATABASE_DIRECT_URL`); cron-worker: up to `max_jobs` (8) per replica (account-erasure leases, HNSW index builds); API: up to `DB_DIRECT_POOL_SIZE` (4) per API process |
| Migrations, admin scripts, backup, `psql` | about 5 |

Pooled total (direct engines open connections only when used, so these are worst cases): `60 + 6W + 8C + 4 x WEB_CONCURRENCY x N + 5`. Two API replicas, two workers and one cron-worker: 60 + 12 + 8 + 16 + 5 = 101. A second PgBouncer instance is for availability only; its pools add to the 60.

### How the application stays transaction-pool safe

- `PGBOUNCER_ENABLED=true` disables asyncpg's and SQLAlchemy's prepared-statement caches (`statement_cache_size=0`, `prepared_statement_cache_size=0`, unique prepared-statement names) and sends no startup GUCs, which a transaction pooler rejects. The overlay therefore applies each role's `statement_timeout` and `idle_in_transaction_session_timeout` with a per-alias `connect_query` in `pgbouncer.ini`.
- **Direct connection required** (`core.database.direct_engine`): session advisory leases for agent runs (`modules/agents/worker.py`), account erasure (`core/auth/deletion_worker.py`) and workspace storage leases (`core/storage/admission.py`), the search HNSW `CREATE INDEX CONCURRENTLY` build (`modules/search/indexing/`: per-index session advisory lock plus session `SET statement_timeout/lock_timeout = 0` on one autocommit connection), the agent LangGraph checkpointer (`AsyncPostgresSaver`, named prepared statements held for a whole segment), plus Alembic migrations and one-shot admin scripts (`DATABASE_DIRECT_URL`). `tests/unit/test_pgbouncer_session_state_guard.py` fails if any other module uses a session advisory lock, a non-`LOCAL` `SET`, `LISTEN`, `WITH HOLD` or a temp object; the only session-`SET` exemptions are the HNSW build (on `direct_engine`) and `core/migration_support.py` (Alembic, which always connects with `DATABASE_DIRECT_URL`).
- Safe through the pooler: `pg_advisory_xact_lock` / `pg_try_advisory_xact_lock` (including the heavy-work slot), `SET LOCAL` and `set_config(..., true)` (chat SQL tool, HNSW search settings, Realtime fences), and the chat SQL tool's `TEMP VIEW`s, which live inside one transaction's raw savepoint and are rolled back before it ends.
- The **heavy-work slot** (`core.heavy_work.heavy_job_slot`) deliberately keeps one transaction open for up to 150 s: the transaction-scoped advisory lock *is* the cluster-wide capacity, released by any rollback, crash or connection loss without a TTL takeover. A `SELECT 1` heartbeat every 10 s keeps the connection from idling in the transaction longer than 10 s (limit 240 s), and domain work uses separate sessions. Under PgBouncer the transaction pins one server connection for its length, which is what the lock needs.

**Upload concurrency cap:** in PgBouncer mode workspace storage leases (uploads, cleanup) use the per-process direct pool, so at most `DB_DIRECT_POOL_SIZE` (default 4) run concurrently per API process. A further lease waits 5 s for a connection and then fails as `WorkspaceStorageBusy` (retryable); raise `DB_DIRECT_POOL_SIZE` (and the budget above) if you expect more concurrent uploads. In direct mode these leases share the main pool instead.

### Making PgBouncer the default for production

Recommended for any deployment that runs more than one API or worker replica. Put the overlay in your launch command (`COMPOSE_FILE=docker-compose.yml:docker-compose.pgbouncer.yml` in `.env` makes plain `docker compose up` use it) and keep `DATABASE_URL` pointing at `postgres` in `.env`; the overlay rewrites it per service. Run migrations with the same command (`migrate` receives `DATABASE_DIRECT_URL`). Verify with `SHOW POOLS;` on the PgBouncer admin console and `components.postgres` in `/api/v1/system/health`. The pinned image tag in the overlay must be checked against your registry before first use.

### Scaling guide

1. **Stateless services:** API, worker, cron-worker, chat-worker and model-worker hold no cross-request correctness state. The audited per-process state (`tests/unit/test_scale_readiness.py`, `PER_PROCESS_STATE_ALLOWLIST`) is: SSE wakeup hints (a missed hint on another replica costs at most the 2 s poll), live-map and weather TTL caches and burst pacing (the durable provider budget stays in PostgreSQL), the CoinGecko catalog, the query-vector cache (60 s, workspace/generation scoped), the ingestion scan cursor and its in-process lock (claims are row-fenced with `FOR UPDATE SKIP LOCKED`, so replicas never double-claim), the agent activity reconcile cursor, and the browser sidecar (a deliberate singleton fenced by `service_instance_id`). A new module-level lock or cache must be added to that list with a justification.
2. **Shared files:** `app-data` (`/data`) must be a volume shared by api and worker replicas until a non-local storage backend ships; this remains the blocker for multi-host deployments (single-host replicas on one volume work).
3. **Concurrency that does not scale with replicas:** the heavy slot serialises parsing/embedding cluster-wide, the global privacy lock serialises chat flushes, and the translation slot is global. Add replicas for API, chat-worker and model-worker throughput, not for those.
4. **Order of scaling:** enable PgBouncer, then raise API replicas, then chat-worker replicas (each adds 15 jobs), then workers (each adds 6 jobs but contends for the one heavy slot).

### Redis HA and queue-full behaviour

Redis stays `noeviction`: queued ARQ jobs, sessions and rate-limit counters are never evicted. When memory reaches `maxmemory`, writes fail with `OOM`. The API maps any unhandled Redis failure (down, failing over or `OOM`) to a retryable `503` with `Retry-After: 5` and error code `QUEUE_UNAVAILABLE` instead of a generic 500; durable outbox rows are re-enqueued by their recovery scans once Redis accepts writes again. `/api/v1/system/health` reports `components.redis.queue_writable` (false above 95% of `maxmemory`, which also sets `overall` to `degraded`); alert on that flag rather than on a failed enqueue.

Recommended `maxmemory`: at least 256 MB (shipped: 512 MB) and about 25% of the Redis host's RAM; queue payloads are identity-only, so growth means a stalled consumer, not data volume. For HA run Redis with one replica and three Sentinels (quorum 2, `down-after-milliseconds 5000`, `failover-timeout 30000`), the same `requirepass`/`masterauth` on both nodes and `min-replicas-to-write 1`, then set `REDIS_SENTINELS`, `REDIS_MASTER_NAME` (and `REDIS_SENTINEL_PASSWORD` if the Sentinels are password-protected). The compose stack ships a single Redis; the Sentinel topology is operator-provided. Clients use `retry_on_timeout`, a 5 s socket timeout and a 30 s health check, so a failover shows up as a few `QUEUE_UNAVAILABLE` responses, not a restart.

## Client IP and `X-Forwarded-For`

The per-IP authentication limit (5 attempts/min per action, plus a global 20/min) keys on the TCP peer by default. Behind Next the peer is always the web container, so all clients share one bucket; this cannot be spoofed. Next's `/api` rewrite forwards a client-supplied `X-Forwarded-For` unchanged and never adds a hop, so the header is trustworthy only if the front reverse proxy **overwrites** it (never appends) with the real client address:

- nginx: `proxy_set_header X-Forwarded-For $remote_addr;` (not `$proxy_add_x_forwarded_for`).
- Caddy: `reverse_proxy 127.0.0.1:3000 { header_up X-Forwarded-For {remote_host} }`.

The chain is Caddy -> Next (`/api` rewrite, which adds no hop and forwards `X-Forwarded-For` / `X-Real-IP` unchanged) -> API (uvicorn `--no-proxy-headers`). In production with a public origin the API (`create_app`; workers and migrations are not affected) **refuses to start** unless `AUTH_TRUST_FORWARDED_FOR=true` or `AUTH_CLIENT_IP_HEADER` is configured, because otherwise the login limit and account lockout become instance-global (one attacker could lock the owner out). Failed attempts are only counted after Origin and CSRF validation passed. The Caddy overlay (`docker-compose.proxy.yml`) sets `AUTH_TRUST_FORWARDED_FOR=true` on `api` (the only service that keys on client IP), so that deployment is safe out of the box. Cloudflare Tunnel: set `AUTH_CLIENT_IP_HEADER=CF-Connecting-IP`.

Set `AUTH_TRUST_FORWARDED_FOR=true` (or the header) to get a separate bucket per client (the limiter uses the rightmost entry). If the web port is reachable directly (not loopback-only), the proxy does not overwrite the header, or other containers that reach `api:8000` directly (the optional n8n connector and browser services) can be driven by untrusted input, leave it `false`. Redis memory is reported as `components.redis.memory` (`used_bytes`, `max_bytes`) in the owner-authenticated `/api/v1/system/health`; alert when used approaches the 256 MB `noeviction` cap.

## Deploys and in-flight chats

**Graceful drain.** Every worker class sets arq `job_completion_wait = job_timeout + 5`: on SIGTERM the worker stops taking new jobs, lets running ones finish for up to that long, then cancels the rest (arq re-runs cancelled jobs on the next worker). Compose `stop_grace_period` is therefore larger than the job timeout: worker and cron-worker 240 s (`job_timeout` 180), chat-worker 660 s (600), model-worker 960 s (`MODEL_WORKER_JOB_TIMEOUT_SECONDS` 900; raise it together with that variable). A chat run killed without the hook (SIGKILL or OOM) is failed by `recover_chat_runs` after `RECOVER_STREAMING_AFTER` (660 s). Pending runs not updated for 15 minutes (for example after a long chat-worker outage) are failed instead of re-enqueued. Main-worker jobs are retried automatically with jittered exponential backoff (see Operations), and a job that exhausts its tries is recorded in `worker_dead_letters`.

## Known ceilings

- **Global privacy lock.** Every chat flush (10 Hz per active chat, `STREAM_FLUSH_SECONDS = 0.1`), every SSE batch publish and every export takes the exclusive owner-wide advisory lock. If waits grow, raise `STREAM_FLUSH_SECONDS` to 0.25 (2.5x fewer lock acquisitions, at most 150 ms more delta latency); the real fix is shared locks for chat/SSE readers.
- **Exports hold the lock** through collection, rendering and the final fences, so chat streaming pauses during an export.
- **MCP registry is per API process.** With `WEB_CONCURRENCY=2` a connection refresh updates only the process that served it; the other process keeps its startup registry until restart. Execution re-checks the database, so it is safe but inconsistent.
- **Redis `noeviction` at 512 MB.** When Redis is full, login fails closed, arq enqueue fails (chat stays pending until recovery) and gateway leases fail. Alert on `components.redis.memory`.

## Realtime event stream

The dashboard uses an authenticated Server-Sent Events connection at `/api/v1/realtime/events`. Configure the HTTPS reverse proxy to disable response buffering and caching for this route, preserve `Last-Event-ID`, and permit long-lived responses. Use an idle/read timeout of at least 75 seconds (the server sends a heartbeat every 15 seconds); do not apply a short response-body timeout. For Nginx, the location should include `proxy_buffering off`, `proxy_cache off`, `proxy_read_timeout 75s`, and `proxy_http_version 1.1`. Retain the normal request size limits and forward the original host/protocol headers. The browser reconnects with its last event ID and fetches a fresh snapshot when replay is no longer available.

To apply migrations separately, run `./scripts/dev.ps1 migrate` or `make migrate`. Compose prevents API/worker startup if migrations fail. `docker compose ps` reports health and one-shot migration state; inspect `docker compose logs migrate api worker` for failures without publishing secret-bearing configuration.

## Release operations summary

Clean install, Phase 0 upgrade, protected proxy paths, module enable/disable procedures are listed in the [release checklist](release-checklist.md#release-procedures-documented-not-executed); acceptance remains pending (`functional_acceptance`, `live_integrations`, `target_capacity` pending).

Expected Alembic head: `0022_dashboard_panels` (the former 87-revision chain, including the `p12_*`/`p14_*`/`r*` revisions named below, was squashed into `0001_init`; the chain is `0001_init` → … → `0022_dashboard_panels` in `infrastructure/postgres/migrations/versions/`). Sections below that name older revisions describe behavior now contained in `0001_init`.

- **Protected proxy paths:** besides `/api/v1/realtime/events`, apply the same no-buffering, no-cache, long-read-timeout and `Last-Event-ID` settings to the Chat response stream `/api/v1/responses/{response_id}/events`. The proxy must forward cookies and the CSRF header unchanged for all owner routes, including `/api/v1/system/*` and `/api/v1/settings/*`.
- **Upgrade:** keep existing `.env` secrets, snapshot the Postgres, `app-data`, `n8n-data`, graph-data and (with the `s3` profile) `rustfs-data` volumes with your cloud provider first, then `migrate`. Quiesce old API/worker processes before the additive P12 cleanup migrations described below.
- **n8n template upgrade (C4 `p14_backend_activation`):** the migration leaves every existing n8n source at `applied_template_revision=0`, below the current packaged template, so those sources stop admitting collection until upgraded. After `migrate` and with the worker and n8n running, preview with `docker compose exec worker python -m modules.connectors.upgrade_templates` (dry run, prints `would_upgrade`/`skipped` counts), then run it with `--apply` (`--limit N` bounds a batch, default 50; re-run, or pass `--after <last_source_id>`, until `more` is false). It runs the owner backend-transition saga (target n8n) per source, never activates native, and is idempotent. `upgraded` sources have the old workflow stopped and wait for their owner to press Activate; `reconciliation_required` sources need the owner to confirm in n8n that the old workflow is inactive, then retry or confirm via `backend-transition/resolve`; `skipped` sources are inactive, terms-ineligible or outside an enabled workspace.
- **Module enable/disable:** use owner Settings (`PATCH /api/v1/settings/modules`); see [operations.md](operations.md).
- **Backup/restore:** not handled by the application; it is delegated to the cloud provider (volume snapshots of Postgres, `app-data`, `n8n-data`, `graph-data` and, with the `s3` profile, `rustfs-data`). See the 2026-10-19 ADR in [ARCHITECTURE_DECISIONS.md](ARCHITECTURE_DECISIONS.md).

## Whole-Source cleanup child receipts

The additive `p12_source_cleanup_children` revision follows `p12_chat_evidence_cleanup`. Quiesce old worker processes before applying it and activate the matching API/worker only after migration succeeds: older Source workers cascade Documents without creating child identity receipts. The migration preserves old queued Source operations for safe capture by the new worker, while already-running/succeeded and uncertain failed receipts are exposed as `unavailable` because their immutable copied-evidence identities cannot be reconstructed from legacy `raw_uris`.

Source deletion retains its atomic limit of 10,000 Documents; an over-limit source remains undeleted and returns a failed receipt. New Source transactions capture each Document's raw URI fence and detached version/chunk identities before cascading, commit operation-only cleanup events, and leave filesystem cleanup to the Documents worker under raw-identity locks. `retained_shared` means another surviving document owns the same raw URI. The owner-authenticated `/api/v1/system/operations/{id}` returns only the Sources allowlisted aggregate projection and is used by the existing purge progress poller. Only an empty successfully captured/deleted scope can complete with the current owner set. Nonempty scopes remain running until every copied-owner stage and the Source coverage gate settle. At `00ddd32` Agent, Notifications/Automations and saved-brief Documents stages are integrated, and success additionally requires Source historical Documents receipts and Source-local Memory coverage. `p12_source_coverage` reopens whole-Source purge operations previously `succeeded` to `running` until coverage re-settles (reversible on downgrade; do not treat this as an operational rollback). This migration and source code are not runtime, SQL execution, target-capacity, raw-storage, or full-erasure acceptance evidence; do not run the structural downgrade as an operational rollback after canonical deletion.

## Google sign-in

Create a Google OAuth web client, add the exact `PUBLIC_ORIGIN` to its authorized JavaScript origins, and register `<PUBLIC_ORIGIN>/api/v1/auth/google/callback` as an authorized redirect URI. Set `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` in the server `.env`; the secret is consumed by the API through Compose `env_file` and is never sent to the browser. Sign-in requests only `openid`, `email`, and `profile`. Gmail collection requires separate consent when the Gmail source is configured. Google login is available only after an owner has linked that identity from Account settings; it never creates the owner or links by email.

The design target is a 2-core, 8 GiB host with remote AI inference. Capacity on that target remains unverified; development-host builds do not establish a capacity guarantee. AI connector settings and optional graph, n8n, and browser capabilities have production implementations, but live connectivity, enabled-profile operation, isolated recovery, and provider acceptance remain separate pending gates. Consult the [release checklist](release-checklist.md) and [implementation status](IMPLEMENTATION_STATUS.md) for the current integration boundary before deploying.

## Optional Langfuse telemetry

The Compose deployment does not include a Langfuse container and does not contact Langfuse unless `.env` opts in (`LANGFUSE_ENABLED` defaults to false). For the opt-in HTTPS remote export, follow [Observability and Langfuse](observability.md) and configure the explicit owner egress approval and backend-only project credentials in `.env`. Disable export by setting `LANGFUSE_ENABLED=false` in `.env` and recreating the services. Only metadata-only model-call spans are sent; content capture is unavailable. Remote availability does not affect API/worker health or core writes. No performance comparison has been measured yet; target-host measurements remain deferred.

## Agent Chat lifecycle migration

Before applying p07_agent_chat_lifecycle, disable new Agent run creation and Chat deletion/expiry cleanup, then drain legitimate uncancelled unlinked legacy read-only runs with the prior compatible application. Preserve their owner/session/source/budget/cancellation checks and checkpoints. Do not execute ambiguous runs or edit their rows directly; cancel them only through the supported owner API, or leave them for review. After the drain, stop the old API and worker processes. The migration locks both agent runs and Chat activity links and aborts before DDL if any uncancelled queued, running, or waiting_approval run has no surviving link. That guard preserves existing data; it does not cancel or rewrite runs. Apply the migration with old writers stopped, then activate the marker-aware API/worker and Chat cleanup together. Old application writers must stay stopped because they do not persist the new Chat lifecycle marker. The migration cannot infer a link that already cascaded away, so this prerequisite is required for both safety and legacy read continuity.

If this migration fails, keep the new API, worker and Chat cleanup inactive. The precondition preserves runs and checkpoints. Resume only the prior compatible application for controlled legitimate drain or separately authorized reconciliation, then quiesce all writers and retry the same additive revision. Do not activate marker-aware processes against an incomplete migration.

After successful activation, retain chat_link_required when rolling back application code. Older writers cannot record this marker, so keep Agent creation and publication disabled unless the fallback also preserves it. Dropping the column after linked runs have lost their Chat links destroys historical lifetime evidence; a schema downgrade requires quiescence and separately authorized reconciliation or drain, with effect tombstones retained. The structural downgrade function alone does not establish a safe operational rollback.

## AI endpoint network policy

### Model gateway

Before enabling an AI connector or a configured web-search endpoint, set `AI_ALLOWED_ENDPOINT_HOSTS` to exact hostnames or `host:port` entries and `AI_ALLOWED_ENDPOINT_CIDRS` to the approved IPv4/IPv6 CIDRs in the protected deployment environment. Both lists are required: every DNS answer must fall within the approved CIDRs, and the client connects to a checked numeric address while retaining the original Host and TLS hostname. Empty CIDR policy denies gateway connections. Include a specific private CIDR for a self-hosted gateway; do not use broad private ranges unless the deployment operator intends to authorize them. Redirects and environment proxy variables are disabled for these SDK requests. DNS resolution, the TCP connect and the TLS handshake are each bounded to 5 s (at most 15 s per attempt, 30 s over the 2 attempts). Lock order: a gateway slot is taken first, and database locks (the Memory privacy lock and evidence row locks) are taken only inside `before_send`, so a lock is never held while waiting for a slot; the lock is held per attempt, from `before_send` until `after_send` runs once the request body is written, so the worst-case hold is one attempt's connect phases (at most 15 s) plus the body write, and a failed first attempt releases it before the retry. `MODEL_GATEWAY_SLOTS` (default 24) caps concurrent model calls across the api, worker and chat-worker; a chat stream holds one slot for its whole answer and waits up to 120 s for it (other calls wait 10 s), so keep it above chat `max_jobs` plus worker `max_jobs`. A private certificate authority must be configured explicitly in the trusted TLS context; certificate verification stays enabled.

### Model providers

Each role default (chat, brief, background, embedding, reranker, vision) can use its own provider (Settings > Models): OpenAI, OpenRouter, Groq, Gemini, Anthropic (OpenAI-compatible endpoints), Ollama, LM Studio or a custom OpenAI-compatible URL. Up to 8 providers are stored per workspace, with API keys encrypted by `AI_CREDENTIAL_ENCRYPTION_KEY` (see `.env.example` for the key-generation command; a missing key returns 503 on the AI settings page). Existing single-endpoint settings become the `default` provider. Hosted kinds (OpenAI, OpenRouter, Groq, Gemini, Anthropic) take only an API key: the server uses the vendor's fixed endpoint, which must still be allowed by `AI_ALLOW_PUBLIC_PROVIDERS` or listed in the allowed endpoint hosts. Rows saved earlier with a hosted kind and a different URL are shown as `custom` and keep their URL.

- Run `alembic upgrade head` (`make migrate`) after upgrading: `r16_ai_system_prompts` and `r17_ai_providers` add columns that Settings reads, so an unmigrated database makes `GET /api/v1/settings/ai` return 5xx.
- Local providers (Ollama, LM Studio) stay on your network: no API key or remote-data consent, but `local-private` can only use them and `local_only` sources are never sent. The host must still be in `AI_ALLOWED_ENDPOINT_HOSTS` and its address in `AI_ALLOWED_ENDPOINT_CIDRS`; from Docker use `host.docker.internal`.
- Cloud providers need an https hostname and either an entry in the host and CIDR allowlists or `AI_ALLOW_PUBLIC_PROVIDERS=true`, which accepts public addresses only (private ranges still need the CIDR list). Settings shows "blocked by deployment policy" otherwise. Each remote provider needs its own consent; a new provider or changed endpoint never inherits it.
- URLs that already carry a version path (Gemini `.../v1beta/openai`, OpenRouter `.../api/v1`, Groq `.../openai/v1`) are used as is; otherwise `/v1` is appended.

### Web search

Web search is configured in Settings > Web search (Tavily, Brave, SearXNG). The n8n URL, API key and webhook token are configured in Settings > n8n (stored in the database; `N8N_*` environment variables are the fallback). There are no `OMNIROUTE_*` environment variables: add connectors and tag model types (llm, embedding, reranker, vision, asr, tts) in Settings > Models.

Web search (Tavily or Brave) has its own policy and never uses `AI_ALLOWED_ENDPOINT_CIDRS`; do not add the providers' public ranges there, because that only widens the model-gateway policy.

- Hosts: list `api.tavily.com` or `api.search.brave.com` in `AI_ALLOWED_ENDPOINT_HOSTS` (shared with the gateway), otherwise saving the provider returns 422. The endpoint must be https.
- Destinations: only public addresses are admitted. Tunnel and NAT64 prefixes are denied, and redirects and environment proxy variables are disabled. `WEB_SEARCH_ALLOWED_CIDRS` additionally allows specific (typically private) ranges and exists for the test compose only; leave it empty in production. Ranges shorter than /8 (IPv4) or /16 (IPv6) are rejected at startup.
- `WEB_SEARCH_DAILY_LIMIT` (default `50`): searches per owner per UTC day. Failed searches count, `0` disables web search, and a Redis outage also disables it (fails closed).
- The owner consent toggle in AI settings is required, and the provider key is stored encrypted under the connector credential key.
- The chat-attachments upload path shares `UPLOAD_MAX_BYTES`.

`r15_document_dismissed` adds the owner "Not relevant" state (`document_interactions.dismissed_at`; hides items from dashboard feed gadgets and highlights only). Its structural downgrade deletes dismissed-only interaction rows (rows that also carry read or saved state keep that state and lose only `dismissed_at`), so hidden-item preferences are lost on downgrade; not an operational rollback.

## Optional connector services

`docker-compose.browser.yml` (browser sidecar) and `docker-compose.connectors.yml` (n8n, includes the browser overlay; Compose >= 2.20) are opt-in. Health reports them as optional and excludes them from `overall`. `COLLECTOR_SCHEDULER_ENABLED` (default true) gates scheduled dispatch. Runbook: `docs/connectors/native-and-n8n.md`.

n8n hardening: `docker-compose.connectors.yml` requires `N8N_ENCRYPTION_KEY` (compose fails fast if unset), blocks env access from Code nodes (`N8N_BLOCK_ENV_ACCESS_IN_NODE=true`), and its entrypoint applies the iptables egress rules as root, then drops to the `node` user via `su-exec` so workflow code has no `NET_ADMIN`. Set `N8N_HOST`, `N8N_PROTOCOL` and `N8N_WEBHOOK_URL` in `.env` when n8n has a hostname. Never expose port 5678 beyond loopback; if webhooks must be public, route only the webhook path through the proxy/tunnel.

## Public exposure (Cloudflare Access, Caddy, tunnel)

`make setup` generates every secret the stack needs (database, Redis, graph, browser, n8n, both Fernet keys, setup/CSRF tokens). Set `PUBLIC_ORIGIN=https://<host>` and `SECURE_COOKIES=true` for any HTTPS deployment.

**Option A: Cloudflare Tunnel (recommended with Cloudflare Access).** Run `cloudflared` on the host (or as a separate container on the compose network) with an ingress rule `https://<host>` -> `http://127.0.0.1:3000` (web). Do not publish 80/443. Add an Access application for `<host>` with an allow policy for the owner only (email or IdP group), session duration short, and keep the app's own login enabled (Access is an extra layer, not a replacement). The origin must accept only tunnel/loopback traffic: leave `WEB_PORT` bound to `127.0.0.1` (default) and block inbound 80/443/3000 at the host firewall. cloudflared sets `CF-Connecting-IP` (Cloudflare overwrites any client-supplied value): set `AUTH_CLIENT_IP_HEADER=CF-Connecting-IP` in `.env`, otherwise the API refuses to start. Do not use `AUTH_TRUST_FORWARDED_FOR` here (cloudflared appends to `X-Forwarded-For`).

**Option B: Caddy overlay** (`docker-compose.proxy.yml` + `infrastructure/proxy/Caddyfile`): `docker compose -f docker-compose.yml -f docker-compose.proxy.yml up -d`. Needs `PUBLIC_HOST` in `.env`; obtains certificates automatically, sends HSTS, caps request bodies at 30 MB, disables buffering (`flush_interval -1`) so SSE (`/api/v1/realtime/events`, `/api/v1/responses/{id}/events`) streams, and overwrites `X-Forwarded-For` with the real peer. If Cloudflare proxies the DNS record in front of Caddy, switch the header to `{header.CF-Connecting-IP}` and restrict 80/443 to Cloudflare ranges.

### Pre-publish checklist

- [ ] `.env` generated by `make setup`; no `replace-with-*` placeholders remain; `.env` is not world-readable and never committed.
- [ ] `PUBLIC_ORIGIN` is the exact `https://` origin and `SECURE_COOKIES=true`.
- [ ] Only the proxy/tunnel is internet-reachable; postgres, redis, graph, n8n (5678), browser and web ports are loopback-only or internal.
- [ ] Cloudflare Access (or equivalent) policy restricts the hostname to the owner.
- [ ] `SETUP_TOKEN` used once to create the owner account; owner login works; then rotate/remove it if desired.
- [ ] `AUTH_TRUST_FORWARDED_FOR=true` (Caddy overlay, which overwrites `X-Forwarded-For`) or `AUTH_CLIENT_IP_HEADER=CF-Connecting-IP` (Cloudflare Tunnel) is set; startup fails without one.
- [ ] SSE paths stream without buffering through the proxy; request body limit is set.
- [ ] `make migrate` reached head `0022_dashboard_panels`; `/api/v1/system/ready` is healthy.
- [ ] Provider volume snapshots configured (Postgres, `app-data`, `n8n-data`, graph-data, `rustfs-data` when the `s3` profile is used) and a restore was rehearsed by the operator.
- [ ] `AI_ALLOWED_ENDPOINT_*` lists are as narrow as intended; optional graph profile and connectors stay off unless needed.

## S3-compatible storage

Original uploads live on the local filesystem (`STORAGE_BACKEND=local`, `DATA_DIR`, a volume shared by API and worker) unless `STORAGE_BACKEND=s3` selects an S3-compatible bucket (AWS S3, RustFS or another S3-compatible store). With S3 the API and worker no longer need a shared volume; parsers still receive a bounded temporary copy that is deleted afterwards (`download_to_temp`).

| Setting | Meaning |
| --- | --- |
| `S3_BUCKET` (required), `S3_REGION` | Target bucket and region (`us-east-1` is fine for RustFS). |
| `S3_ENDPOINT_URL` | Empty for AWS; `http://rustfs:9000` (or an HTTPS URL) for the bundled RustFS or another store. |
| `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY` | Static keys (secrets, never logged). Leave both empty to use the AWS default credential chain (instance/task role). |
| `S3_PREFIX` | Optional key prefix inside the bucket. Object keys keep the local layout `workspaces/<ws>/documents/<doc>/<doc><suffix>`, so stored `raw_uri` values are identical on both backends. |
| `S3_SSE`, `S3_SSE_KMS_KEY_ID` | Server-side encryption: `AES256` (SSE-S3) or `aws:kms` with an optional key id. |
| `S3_PATH_STYLE` | `true` (default) for RustFS and most self-hosted stores; set `false` for AWS virtual-hosted addressing. |
| `S3_CONDITIONAL_PUT` | `true` (default): publication uses `If-None-Match: *` so an existing object is never overwritten. Set `false` only for stores without conditional writes: publication then goes through a `staging/` key plus `CopyObject`, with a best-effort existence check (not race free; keys are unique per document, so a collision needs a retry of the same id). |
| `S3_PART_SIZE_BYTES` | Multipart part size (5 MiB to 512 MiB, default 8 MiB). At most one part is buffered per upload. |
| `STORAGE_PRESIGNED_DOWNLOADS`, `STORAGE_PRESIGNED_TTL_SECONDS`, `S3_PRESIGN_ENDPOINT_URL` | See below. |

**Fence.** S3 has no file locks, so uploads and account erasure are serialized per workspace by a PostgreSQL advisory transaction lock plus the durable `workspace_storage_state(deleted_at)` row (migration `0016_workspace_storage_state`). The invariant is unchanged: an erasure sets the flag before deleting anything, the flag is never cleared (a trigger rejects `UPDATE ... deleted_at = NULL` and `DELETE`), and a delayed upload that still holds a pre-deletion SQL admission is rejected with HTTP 503. The fence uses dedicated connections outside the application pool: each in-flight upload holds one PostgreSQL connection for the whole transfer (erasure briefly two), plus the workspace lease connection. In-process concurrency is capped at 16 holds (further requests get HTTP 503), so size `max_connections` for API workers x 16 plus the pool. Connections are bounded to two hours by `idle_in_transaction_session_timeout`. Residual risk: if the fence connection dies mid-upload the lock is lost; an object published after a completed erasure is removed by the orphan sweep (it is unreferenced).

**Downloads.** By default bytes stream through the API after authorization, with `Content-Disposition: attachment`, the stored `Content-Type`, `Cache-Control: private, no-store` and `X-Content-Type-Options: nosniff`. Optionally `STORAGE_PRESIGNED_DOWNLOADS=true` answers with a `307` redirect to a presigned GET whose lifetime is at most 60 s (`STORAGE_PRESIGNED_TTL_SECONDS`, hard-capped) and which signs `response-content-disposition=attachment` and `response-content-type=<stored type>`. Trade-offs: the API no longer sees the bytes (lower load, no egress through the API) but the link is a bearer capability for up to 60 s, the response lacks the API's `nosniff`/cache headers (the forced attachment disposition and content type mitigate sniffing), the object store must be reachable from the browser (set `S3_PRESIGN_ENDPOINT_URL` to the browser-facing host), and S3 access logs, not API logs, record the download. The web client downloads with `fetch()` plus a blob using credentials and a workspace header, so the browser follows the 307 cross-origin: the bucket needs a CORS rule allowing the web origin (`AllowedOrigins`: your `PUBLIC_ORIGIN`, `AllowedMethods`: `GET`, `AllowedHeaders`: `*`, `ExposeHeaders`: `Content-Disposition`); without it the browser blocks the preflight and downloads fail, so leave `STORAGE_PRESIGNED_DOWNLOADS` off unless that is configured.

**RustFS.** `docker-compose.base.yml` ships an optional `rustfs` service (profile `s3`, image pinned by digest, loopback-only port `RUSTFS_PORT`, named volume `rustfs-data`): `docker compose -f docker-compose.base.yml --profile s3 up -d rustfs`. `RUSTFS_ACCESS_KEY` / `RUSTFS_SECRET_KEY` are required in `.env` (secret at least 16 characters, neither equal to the public default `rustfsadmin`); the service refuses to start (exit 64, message in `docker compose logs rustfs`) otherwise, create the bucket, and point `S3_ENDPOINT_URL=http://rustfs:9000` with `S3_PATH_STYLE=true` at it. The disposable test stack overrides the same service with fixed test credentials and no volume. For a real deployment put TLS in front, use a dedicated bucket and a least-privilege access key (`s3:GetObject`, `PutObject`, `DeleteObject`, `ListBucket`, `AbortMultipartUpload`, `ListBucketMultipartUploads`).
