# Native Collectors và Optional n8n Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** RSS, REST, native providers và browser/MCP collection chạy bằng Python/ARQ; n8n còn tùy chọn, không bắt buộc cho nguồn chuẩn.
**Architecture:** Một collection entrypoint, PostgreSQL giữ request/schedule, reuse ingestion lease và outbox; hai execution backends native/n8n có revision fences. Không tạo queue engine mới.
**Tech Stack:** FastAPI, SQLAlchemy/PostgreSQL, ARQ/Redis, httpx, existing browser sidecar, optional n8n.
**Spec:** [Design baseline](../specs/2026-10-07-production-collectors-translation-design.md); [master](2026-10-07-production-readiness-master.md).

## Global Constraints

- Kế thừa master; W2 scope interface phải tích hợp trước C2.
- Tối đa 2 collection/network jobs toàn instance, 1/workspace; không tăng worker max_jobs=4 hoặc heavy-job gate tùy tiện.
- Poll intervals 15/30/60/360/1440 phút; native không yêu cầu n8n secrets.
- Cursor advance chỉ sau ingestion acknowledgement; no-change không tạo dữ liệu giả.
- Reuse scope, grants, lease, source generation, connector revision, backup/maintenance admission và privacy checks.
- Không chạy tests trong code stage; V2 tạo/run regression/integration sau C/P/T code.

## Review Focus

- Crash sau provider fetch hoặc sau DB commit trước enqueue acknowledgement — C2/C3/V2.
- REST next URL/redirect/DNS rebinding tới private host — C3/V2.
- Source pause/reconfigure/delete hoặc quota trong một lượt chạy — C2/C3/V2.
- Deactivate n8n response mất và webhook cũ tới muộn — C4/V2.
- Thiếu n8n key làm native source không thể bật — C1/C5/V2.

## C1 — Sửa contract hiện tại và chọn backend theo capability

**Files**
- Modify: `modules/connectors/n8n.py`, `modules/connectors/catalog.py`, `modules/connectors/registry.py`, `modules/connectors/providers/world_data.py`.
- Modify: `modules/knowledge/documents/schemas.py`, `modules/connectors/routes.py` chỉ nếu cần align trusted envelope; giữ untrusted ReceiveBatch reject.
- Create: `modules/connectors/backends.py`.
- Deferred tests: `tests/unit/modules/connectors/test_backend_selection.py`, `tests/unit/modules/connectors/test_world_provider_envelope.py`.

**Interfaces**
- `CollectionBackend = Literal["native","n8n"]`.
- `native_dispatch_supported(source_type: str, provider: str | None) -> bool`; derive provider identity từ source đã lưu, không nhận provider override từ request.
- Backend support registry là nguồn duy nhất cho catalog/native dispatch/workflow selection; legacy n8n workflow vẫn dùng provider-fetch cho Alpha Vantage/Open-Meteo.

- [ ] **Step 1 — central dispatch.** Đưa supported provider IDs vào connector owner registry; `build_workflow` gọi cùng registry thay hardcoded set. Phân biệt “native provider normalization” với “native execution backend”; RSS generic có native backend nhưng không phải trusted native provider.
- [ ] **Step 2 — typed envelope.** Với mỗi market/weather record tạo `ProviderRecordMetadata` bằng constructor đã có; world measurement nằm trong `provider_record.world_data`. Giữ identity/version/timestamp_basis/coverage/source_fields theo allowlist; don't label collection time as provider-modified.
```python
record.metadata["provider_record"] = metadata.model_dump(mode="json")
record.metadata.pop("world_data", None)
```
Ở đoạn trên `record` là IngestionRecord đang tạo, `metadata` là ProviderRecordMetadata vừa validate; không dùng dict arbitrary client. Đưa import model vào mapper, không relax ingestion validation.
- [ ] **Step 3 — consumer alignment.** Theo toàn chuỗi receive_native_collection → Document metadata → observations extraction, đảm bảo structured values từ nested trusted schema. Không tạo parallel metadata shape chỉ để renderer cũ hoạt động.
- [ ] **Step 4 — catalog fixes.** GitHub/MCP implemented paths phản ánh source thực; bỏ duplicate unsupported row. Tách code_available/runtime_verified/eligibility thay một chữ “available” gây hiểu nhầm.
- [ ] **Step 5 — build/review.** Preserve Alpha quota, Telegram proofs, GitHub segments và existing envelope bounds. V2 phải cho malformed native envelope thất bại và trusted valid envelope đi tới observations.

**Deferred test shape**
```python
def test_existing_world_providers_select_native_dispatch():
    from modules.connectors.backends import native_dispatch_supported
    assert native_dispatch_supported("api", "alpha_vantage")
    assert native_dispatch_supported("api", "open_meteo")
    assert not native_dispatch_supported("api", "unregistered")
```
Generic REST uses provider None; `native_dispatch_supported("api", None)` true cho execution support, không cho metadata privilege.

## C2 — Durable scheduler, admissions và public request contract

**Files**
- Create: `modules/connectors/collection_schemas.py`, `scheduler.py`.
- Modify: `modules/connectors/models.py`, `public.py`, `worker.py`, `apps/worker/main.py`.
- Modify: managed templates under `infrastructure/n8n/workflows/` so every Schedule/Manual path obtains admission before provider I/O; no changes to arbitrary operator workflows outside this integration contract.
- Create migration `infrastructure/postgres/migrations/versions/p14_collection.py` after p14_workspace_scope.
- Deferred tests: `tests/unit/modules/connectors/test_scheduler_policy.py`, `tests/integration/test_native_collection_scheduler.py`.

**Produces**
- `request_collection(session, scope: WorkspaceContext, source_id: UUID, trigger: Literal["manual","scheduled","retry"], expected_revision: int) -> CollectionRequestRead`.
- `dispatch_due_collections(ctx: dict[str, object]) -> int`, ARQ cron every 15 seconds.
- `CollectionRequestRead(request_id: UUID, source_id: UUID, status: Literal["queued","running","succeeded","no_changes","failed","cancelled"], ingestion_run_id: UUID | None, error_code: str | None)`.
- `GET /api/v1/connectors/sources/{source_id}/collection-requests/{request_id}` checks owner source scope.
- `POST /api/v1/connectors/sources/{source_id}/collection-admission` is service-token-only for managed n8n templates, source/revision/backend bound; returns request_id plus fenced admission token or 409 busy. Existing sync/no-changes completion settles that token. External custom workflows must use the same admission and ingestion contract to count as supported collectors.
- Manual existing endpoint keeps existing keys; native returns 202 with additive request_id, status=queued and nullable ingestion run until accepted. Update public response schema/frontend together. n8n legacy acknowledged response remains accepted by client.

**Persistence**
- Extend provisioning backend native/n8n and backend_revision; legacy rows n8n where workflow exists; uncertain legacy no-workflow state retained for reconciliation.
- `connector_schedules`: source_id PK/FK, workspace_id, interval_minutes, next_due_at, last_dispatch_at, failure_count, next_eligible_at; due index.
- `connector_collection_requests`: id, workspace_id, source_id, trigger, generation, connector_revision, backend_revision, captured backend, status, available_at, attempt, active_admission_token, run_id/error_code, created/updated. Partial unique(source_id) WHERE status in queued/running coalesces manual + tick.
- `connector_admission_slots`: exactly rows slot 1,2; current request/workspace/fencing token, expires_at; partial unique workspace while occupied. Renew 20 s, expiry 120 s. Source lease remains existing SourceIngestionState lease, not another lock.

- [ ] **Step 1 — schema/migration.** Add constraints/indexes, backfill disabled schedules from actual active legacy state without activating anything; old workflow backend persisted. New tables use workspace FK constraints.
- [ ] **Step 2 — scheduler claim.** Transaction selects due sources ordered oldest dispatch then next_due/source_id using FOR UPDATE SKIP LOCKED; at most 50 candidates/tick. Insert/coalesce durable request, set next_due_at once; one late tick produces one catch-up request, not one request for each missed interval.
- [ ] **Step 3 — dispatch after commit.** Queue request UUID only. Enqueue failure leaves durable queued row; next tick retries. Duplicate ARQ task ID is an optimization, PostgreSQL request/lease is authority. Limit scanner to bounded pages.
- [ ] **Step 4 — admissions.** Native worker or managed n8n admission route claims one global slot and existing source lease in a consistent documented order, then moves queued→running. Native dispatcher skips n8n schedules: n8n Schedule requests admission directly, avoiding two schedule owners. Busy workspace/source defers without burning provider attempt. n8n workflow carries token into validate/fetch/sync/no-changes calls; generic REST HTTP node runs only after admission. No transaction held while provider I/O.
- [ ] **Step 5 — retry policy.** Pure helper in scheduler.py:
```python
from datetime import datetime, timedelta

def retry_at(now: datetime, attempt: int, provider_deadline: datetime | None) -> datetime:
    """Never retry earlier than provider instructions; cap only local backoff."""
    delay = min(30 * (2 ** min(max(attempt - 1, 0), 5)), 900)
    candidate = now + timedelta(seconds=delay)
    return max(candidate, provider_deadline) if provider_deadline else candidate
```
At most 5 retry attempts per request; beyond that fail request, schedule next regular due with provider_deadline enforced. Invalid credential/schema/terms do not auto retry; source needs action. Use status/error taxonomy, not blanket retry every exception.
- [ ] **Step 6 — crash recovery/build.** Expired slot alone does not override a still-valid existing source lease (currently15min): wait for it to expire or revoke it through the owning ingestion recovery API with matching token under lock. Both fresh fences and new admission are required before requeue; stale worker cannot settle/publish. An uncooperative external HTTP request can physically overlap after timeout; fencing guarantees no duplicate accepted result, not cancellation of remote network work. Validation: Redis flush + worker kill → no durable loss.

**Deferred test**
```python
from datetime import UTC, datetime, timedelta
from modules.connectors.scheduler import retry_at

def test_provider_deadline_outlives_local_backoff():
    now = datetime(2026, 10, 7, tzinfo=UTC)
    deadline = now + timedelta(hours=2)
    assert retry_at(now, 1, deadline) == deadline
```

## C3 — Shared collection service và native executors

**Files**
- Create: `modules/connectors/collection.py`, `modules/connectors/providers/rest.py`.
- Modify: `modules/connectors/routes.py`, `modules/connectors/public.py`, `modules/connectors/registry.py`, `modules/connectors/n8n.py`; reuse `modules/connectors/mcp.py`, `modules/connectors/crawl.py`, `modules/connectors/github/sync.py`.
- Modify: `modules/ingestion/public.py` only owner seams for shared request/lease settlement.
- Deferred tests: `tests/unit/modules/connectors/test_rest_collection.py`, `tests/integration/test_native_collection.py`.

**Interface**
`collect_source(ctx: dict[str, object], request_id: str) -> None`: ARQ job consumes durable request, loads source/configuration/backend/actor scope. Native provider/routine returns existing NativeCollectionReceipt or generic Receipt, mapped into request status. No public HTTP roundtrip to localhost for Python→Python dispatch.

- [ ] **Step 1 — extraction seam.** Move fetch/normalize coordination from provider-fetch route into collection service preserving provider-specific proofs and operation order; both scoped HTTP route and worker call it. Route authenticates external token before constructing trusted args; worker reloads authority.
- [ ] **Step 2 — RSS.** Extract `read_rss` to provider helper if needed and keep import compatibility for n8n route. Preserve page=10, aggregate=25 MiB, one-day cursor overlap and no-change ack. Add ETag/Last-Modified per source/config revision; 304 is no_changes, not empty overwrite.
- [ ] **Step 3 — REST.** Implement items_path/id/title/content/updated mappings from current config; no arbitrary code expressions. Max10 pages/500 records accepted per batch, 10 MiB serialized batch, 25 MiB aggregate transport, 30 s/request, absolute90 s run; next-page same origin, redirect off. Resolve/recheck public destination with SSRF-safe network connection (pin validated IP or equivalent egress enforcement while retaining Host/SNI); DNS precheck alone is insufficient. Limit decompressed response bytes as well as advertised Content-Length.
- [ ] **Step 4 — credentials.** Reuse encrypted provider inputs owned by provisioning. Source-bound native REST credentials must be copied from existing encrypted inputs; if only opaque n8n ID exists, transition state requires re-entry, never scrape n8n DB. Credential/endpoint revision checked before send and persist.
- [ ] **Step 5 — provider/web/MCP.** Native providers reuse reviewed mappings; GitHub retains OAuth sync proof, Telegram retains verified bot/update semantics, MCP retains grant/tool checks. Browser uses existing authenticated sidecar and async durable ingestion path; no n8n dependency for calling it, still one browser job.
- [ ] **Step 6 — settle.** Persist ingestion receipt then request completion; source cursor only owner ingestion API can advance. Error/no-change statuses preserve last-good observations. Hold request fencing through post-I/O validation and commit.
- [ ] **Step 7 — build/review.** Test design in V2 covers non-JSON, schema mismatch, oversized compressed response, same-origin pagination abuse, stale token and worker crash at each settlement boundary.

**Settlement invariant example**
```python
def snapshot_matches(captured: tuple[int, int, int], current: tuple[int, int, int]) -> bool:
    """Compare source generation, connector revision and execution-backend revision."""
    return captured == current
```
Define in collection.py and use under locked fresh source/provisioning rows, in addition to lease token/grant checks. Comparison alone never grants authorization.

## C4 — Native activation và safe backend transition

**Files**
- Modify: `modules/connectors/{activation,provisioning,provisioning_routes,public,worker,backends}.py`.
- Modify: `modules/connectors/models.py` fields in p14_collection migration before it is published.
- Deferred tests: `tests/integration/test_connector_backend_transition.py`.

**Interfaces**
- Add optional `execution_backend` to existing settings/activation DTO; omitted means preserve backend for existing source, native default for a new supported source.
- `POST /api/v1/connectors/{source_id}/backend-transition`: `{expected_revision,target_backend}` → 202 provisioning DTO with transition state.
- Read DTO adds backend and transition phase: idle/draining/deactivating_old/activating_new/reconciliation_required. Existing state field retains current values.

- [ ] **Step 1 — backend branch.** Native activation validates source/credentials/privacy, persists desired+applied revision and schedule in one transaction. No n8n credential provisioning or workflow ID needed. n8n activation keeps reviewed saga.
- [ ] **Step 2 — remove implicit workflow requirement.** Update require_collection_fence, activation_status, validation routes, manual trigger and health readiness to test applied backend state. Native active with workflow_id None is valid; n8n still requires confirmed workflow.
- [ ] **Step 3 — transition saga.** Invalidate scheduling and old backend_revision, cancel queued requests, fence running results. Confirm old n8n workflow inactive through supported API; response ambiguity remains fenced. Never mark transition done merely because local desired_enabled=false.
- [ ] **Step 4 — new activation.** Apply new credential readiness and activate target backend only after old confirms inactive. Preserve source UUID, cursor and accepted provenance; do not re-ingest entire history to establish new backend.
- [ ] **Step 5 — rollback.** Same state machine to switch back. Native paused and jobs fenced before n8n activation; stale n8n webhook rejected by backend_revision + generation/revision.
- [ ] **Step 6 — build/review.** Include credential re-entry state and operator resolution instructions; no n8n secret deletion until rollback retention policy allows it.

**State sequence**
```text
active(n8n,r7) → draining(r8) → deactivating_old(r8)
→ confirmed inactive → activating_new(native,r8) → active(native,r8)
timeout during deactivate → reconciliation_required(r8), neither backend admitted
```

## C5 — Setup/UI, optional services và operational visibility

**Files**
- Modify: `docker-compose.connectors.yml`, `.env.example`, `docs/connectors.md`, `docs/deployment.md`, `docs/ARCHITECTURE_DECISIONS.md`.
- Create: `docker-compose.browser.yml`, `docs/connectors/native-and-n8n.md`.
- Modify: `apps/web/src/modules/sources/connector-editor.tsx`, `api.ts`, `sync-history.tsx`, source message catalog; `core/config.py`, `modules/connectors/catalog.py`.
- Deferred tests: V2 Compose config validation, `tests/e2e/native-sources.spec.ts`.

- [ ] **Step 1 — compose split.** Base native RSS/API needs API/worker/PostgreSQL/Redis only. Browser overlay owns browser service/network/env; n8n overlay owns n8n isolated network/firewall/env. Preserve legacy connectors overlay including both via documented compatibility composition or service definitions; do not silently remove browser for existing command.
- [ ] **Step 2 — environment.** Add collector scheduler flags/limits defaults; document native needs CONNECTOR_CREDENTIAL_ENCRYPTION_KEY only for credentials. N8N_API_KEY is obtained from n8n management API UI, not random. Retain persistent N8N_ENCRYPTION_KEY for n8n users. Browser token only required for browser collection.
- [ ] **Step 3 — source UI.** Normal new source uses native; advanced owner-only backend selector shows n8n capability/license/setup prerequisites. Show queued/running/last-success/next-due/retry and error action; Collect now accepts 202 and polls durable request until terminal.
- [ ] **Step 4 — metrics.** Record pending/due lag, running slots, success/no-change/failure by provider+backend, retry-after, stale leases, transition unresolved. Avoid workspace/source IDs as unbounded metric labels; retain those in scoped logs. Never log raw API keys, invitation/webhook tokens or article content.
- [ ] **Step 5 — runbook.** Base/native, browser and n8n setup commands; key creation; health; dry-run validation; migration/rollback; 429 troubleshooting; license note from official n8n FAQ. Explain n8n optional means no functional dependency for native sources, not automatic removal of installed data.
- [ ] **Step 6 — build/review.** Run build only now. V2 proves an entirely absent n8n service doesn't degrade native activation/manual/scheduled flows or show false global “system down”.
