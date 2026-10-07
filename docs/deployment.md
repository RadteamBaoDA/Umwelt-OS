# Deployment

The production Compose stack contains PostgreSQL with pgvector installed, Redis, a one-shot Alembic migration service, the FastAPI API, one main ARQ worker, one chat ARQ worker, and the Next.js frontend. Application containers run as non-root users. Persistent database and Redis data live in named volumes.

The web port binds to loopback by default. For remote access, terminate HTTPS at a trusted reverse proxy and set `PUBLIC_ORIGIN` to the exact browser origin and `SECURE_COOKIES=true` in `.env`. Keep the API, Redis, PostgreSQL, and Docker socket private. Do not deploy with the example placeholder values.

## Resource bounds

| Variable | Default | Effect |
|---|---|---|
| `DB_POOL_SIZE` / `DB_MAX_OVERFLOW` | `10` / `10` | API SQLAlchemy pool per process (pool timeout 5 s, recycle 1800 s). Size total connections across API processes, workers and admin below PostgreSQL `max_connections`. |
| `DB_STATEMENT_TIMEOUT_MS` | `60000` | API `statement_timeout` (also bounds lock waits). `0` disables. The main worker never sets a statement timeout; the chat-worker sets 60 s. |
| `DB_IDLE_TX_TIMEOUT_MS` | `240000` | `idle_in_transaction_session_timeout` for API and worker connections; a backstop above the longest legitimate send-fence hold. `0` disables. |
| `MAX_REQUEST_BODY_BYTES` | `5242880` | Request body cap returning 413; `POST /api/v1/documents/upload` allows `UPLOAD_MAX_BYTES` plus 1 MiB. |
| `WEB_CONCURRENCY` | `2` (image `ENV`) | API process count. Above 1 (also `UVICORN_WORKERS` > 1) the API refuses to start unless `CSRF_SIGNING_SECRET` is set, so every process signs sessions with the same secret. |
| `AUTH_TRUST_FORWARDED_FOR` | `false` | Key the per-IP auth rate limit on the rightmost `X-Forwarded-For` entry. Default `false` keys on the TCP peer (the web container, so effectively one shared 5/min bucket per action). Set `true` only behind the front proxy configuration in "Client IP and `X-Forwarded-For`" below. |

**`WEB_CONCURRENCY` is the only supported way to set the API process count.** uvicorn reads it as the `--workers` default, and `create_app()` reads it to enforce the shared-secret guard. Running `uvicorn --workers N` with `WEB_CONCURRENCY` unset (or `1`) starts N processes that bypass the guard and sign sessions with different random secrets; do not pass `--workers`. The production image `CMD` omits it on purpose.

## Recommended values: 2 vCPU / 8 GiB host

| Service | Memory limit / reservation | CPU shares | Key settings |
|---|---|---|---|
| postgres | 2.5 GiB / 1 GiB (`shm_size` 1 GiB) | 768 | `max_connections=100 shared_buffers=1GB effective_cache_size=2GB work_mem=16MB maintenance_work_mem=256MB` |
| redis | 384 MiB / 64 MiB | 128 | `maxmemory 256mb`, `noeviction` (never drop queued ARQ jobs; writes fail loudly instead) |
| api | 1 GiB / 512 MiB | 1024 | `WEB_CONCURRENCY=2`, uvicorn `asyncio` + `httptools` via `apps.api.protocol:NoDelayHttpToolsProtocol` (sets TCP_NODELAY; pre-bound multi-worker sockets lack it) (no `--proxy-headers`: uvicorn never takes the client address from request headers), keep-alive 15 s, graceful shutdown 8 s, `--limit-concurrency 400`; `stop_grace_period` 15 s |
| worker | 1 GiB / 256 MiB | 512 | `max_jobs=6`, DB pool 7 + 10 |
| chat-worker | 768 MiB / 256 MiB | 512 | `max_jobs=10`, `job_timeout=600`, DB pool 10 + 10 |
| web | 768 MiB / 256 MiB | 512 | `NODE_OPTIONS=--max-old-space-size=512`, `experimental.proxyTimeout=120000` |
| migrate | 512 MiB (one-shot, exits before the others start) | - | - |

Memory limits sum to 6.4 GiB (2.5 + 0.375 + 1 + 1 + 0.75 + 0.75), leaving about 1.6 GiB for the OS and page cache. The optional connectors overlay adds limits of 768 MiB + 1.5 GiB (2.25 GiB), so with it the limits total about 8.65 GiB, above 8 GiB: they are caps, not reservations, and the stack relies on actual RSS (about 4.2 GiB steady, plus connectors) staying below RAM. CPU shares apply only under contention (no hard CPU caps).

Connection budget (`max_connections=100`): API 2 x (10 + 10) = 40, main worker 7 + 10 = 17, chat-worker 10 + 10 = 20, total 77. The main worker also opens one non-pooled agent checkpoint connection per running agent run (at most 6, so worker worst case 23); plus migrate, backup and admin about 5 gives a worst case of about 88, leaving about 12 spare. If you change `WEB_CONCURRENCY`, `DB_POOL_SIZE` or `DB_MAX_OVERFLOW`, recompute this before applying.

Redis clients use `socket_timeout=5`, `socket_connect_timeout=2`, `health_check_interval=30` and at most 100 connections. Each API process allows 32 concurrent Realtime SSE streams. No global `lock_timeout` is set because it would turn waits on the Memory privacy lock into errors.

## Client IP and `X-Forwarded-For`

The per-IP authentication limit (5 attempts/min per action, plus a global 20/min) keys on the TCP peer by default. Behind Next the peer is always the web container, so all clients share one bucket; this cannot be spoofed. Next's `/api` rewrite forwards a client-supplied `X-Forwarded-For` unchanged and never adds a hop, so the header is trustworthy only if the front reverse proxy **overwrites** it (never appends) with the real client address:

- nginx: `proxy_set_header X-Forwarded-For $remote_addr;` (not `$proxy_add_x_forwarded_for`).
- Caddy: `reverse_proxy 127.0.0.1:3000 { header_up X-Forwarded-For {remote_host} }`.

Only then set `AUTH_TRUST_FORWARDED_FOR=true` to get a separate bucket per client (the limiter uses the rightmost entry). If the web port is reachable directly (not loopback-only), the proxy does not overwrite the header, or other containers that reach `api:8000` directly (the optional n8n connector and browser services) can be driven by untrusted input, leave it `false`. Redis memory is reported as `components.redis.memory` (`used_bytes`, `max_bytes`) in the owner-authenticated `/api/v1/system/health`; alert when used approaches the 256 MB `noeviction` cap.

## Deploys and in-flight chats

arq cancels running jobs on SIGTERM; `stop_grace_period` only covers shutdown hooks. A chat generation in flight during `docker compose up -d` is not resumed: its run stays `streaming` until `recover_chat_runs` fails it after `RECOVER_STREAMING_AFTER` (660 s), after which the owner can resend. A prompt-recovery shutdown hook is not implemented because it needs changes inside `modules/chat/`; schedule deploys when no chat is generating. Main-worker jobs are retried automatically.

## Realtime event stream

The dashboard uses an authenticated Server-Sent Events connection at `/api/v1/realtime/events`. Configure the HTTPS reverse proxy to disable response buffering and caching for this route, preserve `Last-Event-ID`, and permit long-lived responses. Use an idle/read timeout of at least 75 seconds (the server sends a heartbeat every 15 seconds); do not apply a short response-body timeout. For Nginx, the location should include `proxy_buffering off`, `proxy_cache off`, `proxy_read_timeout 75s`, and `proxy_http_version 1.1`. Retain the normal request size limits and forward the original host/protocol headers. The browser reconnects with its last event ID and fetches a fresh snapshot when replay is no longer available.

To apply migrations separately, run `./scripts/dev.ps1 migrate` or `make migrate`. Compose prevents API/worker startup if migrations fail. `docker compose ps` reports health and one-shot migration state; inspect `docker compose logs migrate api worker` for failures without publishing secret-bearing configuration.

## Release operations summary

Clean install, Phase 0 upgrade, protected proxy paths, module enable/disable and backup/restore procedures are listed in the [release checklist](release-checklist.md#release-procedures-documented-not-executed); acceptance remains pending (`functional_acceptance`, `live_integrations`, `target_capacity` pending; `restore_verified` false).

Expected Alembic head: `p12_evidence_version_index`. P12 lineage: `p12_memory_document_cleanup` → `p12_agent_document_cleanup` → `p12_copied_stage_cleanup` → `p12_source_coverage` → `p12_evidence_version_index` (index on cleanup evidence `document_version_id`; each `down_revision` verified against `infrastructure/postgres/migrations/versions/`; the chain continues from `p12_source_cleanup_children`).

- **Protected proxy paths:** besides `/api/v1/realtime/events`, apply the same no-buffering, no-cache, long-read-timeout and `Last-Event-ID` settings to the Chat response stream `/api/v1/responses/{response_id}/events`. The proxy must forward cookies and the CSRF header unchanged for all owner routes, including `/api/v1/system/*` and `/api/v1/settings/*`.
- **Upgrade:** keep existing `.env` secrets, back up first with `make backup BACKUP=<path>.age` (or `./scripts/dev.ps1 backup -BackupPath <path>.age`), then `migrate`. Quiesce old API/worker processes before the additive P12 cleanup migrations described below.
- **Module enable/disable:** use owner Settings (`PATCH /api/v1/settings/modules`); see [operations.md](operations.md).
- **Backup/restore:** see [backup-recovery.md](backup-recovery.md).

## Whole-Source cleanup child receipts

The additive `p12_source_cleanup_children` revision follows `p12_chat_evidence_cleanup`. Quiesce old worker processes before applying it and activate the matching API/worker only after migration succeeds: older Source workers cascade Documents without creating child identity receipts. The migration preserves old queued Source operations for safe capture by the new worker, while already-running/succeeded and uncertain failed receipts are exposed as `unavailable` because their immutable copied-evidence identities cannot be reconstructed from legacy `raw_uris`.

Source deletion retains its atomic limit of 10,000 Documents; an over-limit source remains undeleted and returns a failed receipt. New Source transactions capture each Document's raw URI fence and detached version/chunk identities before cascading, commit operation-only cleanup events, and leave filesystem cleanup to the Documents worker under raw-identity locks. `retained_shared` means another surviving document owns the same raw URI. The owner-authenticated `/api/v1/system/operations/{id}` returns only the Sources allowlisted aggregate projection and is used by the existing purge progress poller. Only an empty successfully captured/deleted scope can complete with the current owner set. Nonempty scopes remain running until every copied-owner stage and the Source coverage gate settle. At `00ddd32` Agent, Notifications/Automations and saved-brief Documents stages are integrated, and success additionally requires Source historical Documents receipts and Source-local Memory coverage. `p12_source_coverage` reopens whole-Source purge operations previously `succeeded` to `running` until coverage re-settles (reversible on downgrade; do not treat this as an operational rollback). This migration and source code are not runtime, SQL execution, target-capacity, raw-storage, or full-erasure acceptance evidence; do not run the structural downgrade as an operational rollback after canonical deletion.

## Google sign-in

Create a Google OAuth web client, add the exact `PUBLIC_ORIGIN` to its authorized JavaScript origins, and register `<PUBLIC_ORIGIN>/api/v1/auth/google/callback` as an authorized redirect URI. Set `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` in the server `.env`; the secret is consumed by the API through Compose `env_file` and is never sent to the browser. Sign-in requests only `openid`, `email`, and `profile`. Gmail collection requires separate consent when the Gmail source is configured. Google login is available only after an owner has linked that identity from Account settings; it never creates the owner or links by email.

The design target is a 2-core, 8 GiB host with remote AI inference. Capacity on that target remains unverified; development-host builds do not establish a capacity guarantee. OmniRoute settings and optional graph, n8n, and browser capabilities have production implementations, but live connectivity, enabled-profile operation, isolated recovery, and provider acceptance remain separate pending gates. Consult the [release checklist](release-checklist.md) and [implementation status](IMPLEMENTATION_STATUS.md) for the current integration boundary before deploying.

## Optional Langfuse telemetry

The base Compose deployment does not include or contact Langfuse: `api`, `worker` and `chat-worker` force `BBD_LANGFUSE_ENABLED=false`, even when `.env` retains opt-in values. For the opt-in HTTPS remote profile, follow [Observability and Langfuse](observability.md), configure the explicit owner egress approval and backend-only project credentials in `.env`, and include `infrastructure/observability/compose.yml` in the Compose command; its environment mapping overrides the base setting. Disable export by running `docker compose down` and restarting with `docker compose -f docker-compose.yml up -d`. Only metadata-only model-call spans are sent; content capture is unavailable. Remote availability does not affect API/worker health or core writes. No performance comparison has been measured yet; target-host measurements remain deferred.

## Agent Chat lifecycle migration

Before applying p07_agent_chat_lifecycle, disable new Agent run creation and Chat deletion/expiry cleanup, then drain legitimate uncancelled unlinked legacy read-only runs with the prior compatible application. Preserve their owner/session/source/budget/cancellation checks and checkpoints. Do not execute ambiguous runs or edit their rows directly; cancel them only through the supported owner API, or leave them for review. After the drain, stop the old API and worker processes. The migration locks both agent runs and Chat activity links and aborts before DDL if any uncancelled queued, running, or waiting_approval run has no surviving link. That guard preserves existing data; it does not cancel or rewrite runs. Apply the migration with old writers stopped, then activate the marker-aware API/worker and Chat cleanup together. Old application writers must stay stopped because they do not persist the new Chat lifecycle marker. The migration cannot infer a link that already cascaded away, so this prerequisite is required for both safety and legacy read continuity.

If this migration fails, keep the new API, worker and Chat cleanup inactive. The precondition preserves runs and checkpoints. Resume only the prior compatible application for controlled legitimate drain or separately authorized reconciliation, then quiesce all writers and retry the same additive revision. Do not activate marker-aware processes against an incomplete migration.

After successful activation, retain chat_link_required when rolling back application code. Older writers cannot record this marker, so keep Agent creation and publication disabled unless the fallback also preserves it. Dropping the column after linked runs have lost their Chat links destroys historical lifetime evidence; a schema downgrade requires quiescence and separately authorized reconciliation or drain, with effect tombstones retained. The structural downgrade function alone does not establish a safe operational rollback.

## AI endpoint network policy

Before enabling OmniRoute or a configured web-search endpoint, set `AI_ALLOWED_ENDPOINT_HOSTS` to exact hostnames or `host:port` entries and `AI_ALLOWED_ENDPOINT_CIDRS` to the approved IPv4/IPv6 CIDRs in the protected deployment environment. Both lists are required: every DNS answer must fall within the approved CIDRs, and the client connects to a checked numeric address while retaining the original Host and TLS hostname. Empty CIDR policy denies gateway connections. Include a specific private CIDR for a self-hosted gateway; do not use broad private ranges unless the deployment operator intends to authorize them. Redirects and environment proxy variables are disabled for these SDK requests. A private certificate authority must be configured explicitly in the trusted TLS context; certificate verification stays enabled.
