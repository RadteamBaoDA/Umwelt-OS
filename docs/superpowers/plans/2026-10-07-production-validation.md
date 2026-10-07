# Production Validation và Pilot Rollout Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Chứng minh behavior/privacy/recovery và ghi riêng các live/provider/capacity gates trước khi gọi pilot production-ready.
**Architecture:** Unit fixtures không gọi mạng, integration trên disposable PostgreSQL/Redis/API, E2E nhiều browser context, live smoke có giới hạn và target-host measurement. Không dùng test database hoặc backups thật từ production.
**Tech Stack:** Existing pytest/pytest-asyncio/httpx, scripts/dev.ps1 test harness, Playwright, Docker Compose, system/container metrics.
**Spec:** [Design baseline](../specs/2026-10-07-production-collectors-translation-design.md); [master](2026-10-07-production-readiness-master.md).

## Global Constraints

- Chỉ bắt đầu sau W1–W5/C1–C5/P1–P4/T1–T4 production code/build/review hoàn thành. Lần viết plan không tạo/run test hoặc khởi động service.
- Không dùng smoke success thay thế authorization/failure tests; không dùng fixture pass thay live quota/terms acceptance.
- Không tự deploy/push/merge hoặc xóa volume thật. Existing test harness tạo project bbd-os-test-<random> và kiểm tra identity bbd_test.
- Tất cả report có SHA/config/workload/time/observed outcome; blocked khác pass. Baseline 1103 unit tests là evidence cũ, không phải acceptance của task mới.
- Review P0/P1 phải resolved trước pilot; lỗi cross-user leakage/data loss không thể “accept risk” để đánh dấu đạt.

## Review Focus

1. Membership chưa share, revoke giữa read/cache/job, request workspace giả — V1.
2. Crash/restart/Redis loss/old webhook, duplicate cursor/idempotency — V2.
3. Valid HTTP nhưng payload/quota/license không hợp lệ, giá/unit/time sai — V3.
4. Model injection, citation/numeric corruption, stale workspace UI response — V4.
5. Fresh install/upgrade/restore mất secret/scope, 2-core overload — V5.

## V1 — Multiworkspace security và migration tests

**Files**
- Create tests declared in W plans; add fixtures to `tests/integration/conftest.py` without breaking existing owner_client.
- Create: `tests/integration/test_workspace_isolation.py`, `tests/e2e/workspaces.spec.ts`.
- Modify disposable `docker-compose.test.yml` only to enable multiworkspace test feature; test-only env credentials, never read real .env.
- Create report `docs/release/free-data-pilot-acceptance.md` with evidence sections per V task.

- [ ] **Step 1 — unit regressions.** Implement W1/W2/W3 snippets and boundary variations (expired token, revoked membership, cross-workspace same UUID, member write denied). Fail with observable expected old behavior when run against baseline/reference fixture, then implement any missing fixes and record regression evidence.
- [ ] **Step 2 — invitation fixture.** Create invitee through real APIs, never bypass policy to give successful membership. Add `invited_member_client` fixture using public HTTP:
```python
import os
from urllib.parse import parse_qs, urlparse
from uuid import uuid4
import pytest_asyncio
from httpx import AsyncClient

@pytest_asyncio.fixture
async def invited_member_client(owner_client):
    workspaces = (await owner_client.get("/api/v1/workspaces")).json()["items"]
    owned = next(item for item in workspaces if item["role"] == "owner")
    email = f"member-{uuid4().hex}@example.test"
    issued = await owner_client.post(
        f"/api/v1/workspaces/{owned['id']}/invitations",
        json={"email": email, "expected_revision": owned["configuration_revision"]},
    )
    assert issued.status_code == 201
    token = parse_qs(urlparse(issued.json()["invitation_url"]).query)["token"][0]
    async with AsyncClient(
        base_url=os.environ["BBD_API_URL"],
        headers={"Origin": os.environ["TEST_PUBLIC_ORIGIN"]},
    ) as client:
        accepted = await client.post(
            "/api/v1/workspaces/invitations/accept",
            json={"token": token, "password": "Disposable-Pilot-Password-42"},
        )
        assert accepted.status_code == 200
        login = await client.post(
            "/api/v1/auth/login",
            json={"identifier": email, "password": "Disposable-Pilot-Password-42"},
        )
        assert login.status_code == 200
        client.headers["X-CSRF-Token"] = login.json()["csrfToken"]
        client.headers["X-Workspace-ID"] = owned["id"]
        yield client
```
W1 WorkspaceRead supplies configuration_revision; invitation URL uses token query parameter, frontend removes token from browser history after accept. Token must not be printed in test assertion output/report.
- [ ] **Step 3 — no implicit access.**
```python
import pytest

@pytest.mark.asyncio
async def test_membership_does_not_grant_source_admin(invited_member_client):
    response = await invited_member_client.get("/api/v1/sources")
    assert response.status_code == 403
```
Add Document/Brief fixtures through owner public endpoints using inspected schema; exact fields follow owner contracts, not direct SQL grants. Two workspaces each use matching external IDs and content keywords. Exercise list/detail/download/search/vector/graph/citation/translation/export.
- [ ] **Step 4 — races.** Concurrent accept single token; membership revoke before callback; source purge during worker; share revoked while read transaction yields; SSE reconnect old replay cursor; account switches in same browser tab. Assert no response body/count/title includes private fixture sentinel.
- [ ] **Step 5 — migration.** Upgrade cloned disposable pre-p14 schema with representative source/doc/brief/chat/cleanup/outbox rows; all map to legacy default. Verify orphan fixture blocks before NOT NULL, no data loss. Downgrade single-owner empty expansion allowed; multiaccount downgrade rejected without destructive cleanup. Old sessions invalidated at cutover.
- [ ] **Step 6 — run.**
```powershell
./scripts/dev.ps1 test -PytestTarget tests/unit/core/test_workspace_access.py
./scripts/dev.ps1 test -PytestTarget tests/integration/test_workspace_isolation.py
./scripts/dev.ps1 test -E2eTarget tests/e2e/workspaces.spec.ts
```
Run additional W suites within disposable integration harness, not bare pytest against unknown DB.
- [ ] **Step 7 — acceptance.** Zero unauthorized output/side effect; W2 matrix has no unclassified route/table/job/tool; mark user/membership flag eligible for pilot only after all tests pass.

## V2 — Collection behavior, concurrency và n8n transition

**Files**
- Create all C deferred suites, `tests/integration/test_native_collection.py`, `test_connector_backend_transition.py`, `tests/e2e/native-sources.spec.ts`.
- Add httpx MockTransport fixtures with request counters, failure timing and deterministic clock.
- Extend acceptance report with native/n8n matrices.

- [ ] **Step 1 — scheduler unit tests.** C2 retry_at test plus long Retry-After, clock-aware dates, max attempts, coalesced late schedule, manual+scheduled unique request, no retry on terms/auth/schema.
- [ ] **Step 2 — pipeline tests.** Native RSS/API activation with all N8N_* absent, manual202 polling and scheduled run produce real ingestion/document/observation receipts. No-change/304 advances last_success without duplicate content or false missing series.
- [ ] **Step 3 — concurrency/fault injection.** Pause after durable request commit before enqueue, after provider response before ingest, after ingest commit before request completion. Kill worker/flush only disposable Redis and restart. Same provider_id/version ingests once; queued requests recover; old lease cannot publish.
- [ ] **Step 4 — resource limits.** Five workspaces ready simultaneously: max2 admitted requests, max1/workspace, bounded oldest-first fairness; busy jobs do not starve other workspace. Provider HTTP may fail while checkpoint remains durable. Large compressed response/REST next private IP/redirect/DNS rebind rejected before private connection.
- [ ] **Step 5 — backend transition fake API.** Cases: deactivate acknowledged; response lost; GET confirms inactive; credential unavailable; old webhook arrives; rollback. Exactly one backend admitted; unknown state is reconciliation_required, never optimistic active.
- [ ] **Step 6 — real optional n8n.** With operator-provided disposable n8n management key, run one RSS/native-provider workflow and one generic REST workflow through native↔n8n transitions. Without this environment, mark n8n live acceptance blocked; native acceptance can pass independently, n8n remains not release-enabled.
- [ ] **Step 7 — run.**
```powershell
./scripts/dev.ps1 test -PytestTarget tests/unit/modules/connectors/test_scheduler_policy.py
./scripts/dev.ps1 test -PytestTarget tests/integration/test_native_collection.py
./scripts/dev.ps1 test -PytestTarget tests/integration/test_connector_backend_transition.py
./scripts/dev.ps1 test -E2eTarget tests/e2e/native-sources.spec.ts
```
- [ ] **Step 8 — compose/setup.** Validate base/native, browser-only, n8n+browser legacy overlay and optional n8n with isolated compose config. Native readiness ignores absent n8n; browser absence affects browser sources only. Retained n8n data volume untouched.

## V3 — Provider fixtures, terms và bounded live smoke

**Files**
- Create P suites and `tests/fixtures/providers/{world-bank,frankfurter,ecb,binance,alternative,usgs,coinpaprika,coingecko,alpha-vantage,bbc,vnexpress,gdelt,hn}/` fixtures.
- Create `scripts/check-free-providers.py`, read-only GET smoke with explicit selected provider IDs, no automatic API-key discovery.
- Update `docs/connectors/provider-research-2026-10-07.md` and acceptance report; never overwrite historical probe results.

- [ ] **Step 1 — schema/numeric fixtures.** For each adapter capture sanitized normal response + empty + missing field + changed schema + HTTP429/5xx + HTTP200 error. WorldBank null test from P3; finite decimal rejects NaN/Inf/bool; provider dates distinct from collection dates; USDT never labeled USD.
- [ ] **Step 2 — identity/time.** Repeat payload at new collection time; stable version/dedup. World Bank repeated annual point revision only on value change; USGS same ID newer update becomes version; RSS reorder/GUID preserved. No fabricated publication time for current price.
- [ ] **Step 3 — contract path.** Trusted provider_record validates, ingestion accepts, Observation DTO/gadget reads same metric/unit/timestamp. Generic client batch trying to submit provider_record/world_data remains rejected.
- [ ] **Step 4 — terms gate.** unknown/commercial workspace cannot activate personal/noncommercial-only preset. Key expiry/quota error shows safe reason and next retry, no alternate paid URL attempted. Attribution rendered beside Alternative.me index and RSS publisher.
- [ ] **Step 5 — smoke tool.** CLI arguments provider IDs, timeout8s, at most2 requests/provider, concurrency1. Log URL without query credentials, status, content family, schema success, timestamp and duration; do not store full news article/key. Script exits nonzero for failed selected required provider, report distinguishes timeout from provider-down.
- [ ] **Step 6 — optional keyed providers.** Operator supplies scoped test credential explicitly; quota budget printed before execution without key. Alpha ≤2 daily calls this smoke. No attempts to bypass geoblocking/429 with proxy rotation.
- [ ] **Step 7 — run fixture suites first, then live authorized smoke.**
```powershell
./scripts/dev.ps1 test -PytestTarget tests/unit/modules/connectors/test_macro_providers.py
./scripts/dev.ps1 test -PytestTarget tests/unit/modules/connectors/test_crypto_providers.py
./scripts/dev.ps1 test -PytestTarget tests/unit/modules/connectors/test_public_news_feeds.py
uv run python scripts/check-free-providers.py --providers world_bank frankfurter ecb usgs alternative --timeout 8
```
- [ ] **Step 8 — acceptance.** Mandatory enabled source fixtures pass and live eligible endpoints succeed through app ingestion, not just curl. Experimental GDELT can remain disabled with failed/timeout record. VN quote/SEC/FRED/TwelveData research-only states stay honest.

## V4 — Translation security, fidelity và UI behavior

**Files**
- Create T suites plus `tests/unit/modules/translations/test_cache_identity.py`, `tests/integration/test_content_translation.py`, `test_translation_cleanup.py`, `tests/e2e/content-translation.spec.ts`.
- Model fake implements existing gateway structured API; tracks calls and before_send; returns deterministic translated fixtures.
- Add bilingual review sample appendix to acceptance report.

- [ ] **Step 1 — defaults/scope.** Setting defaults false/vi, CAS conflict409, member write403, workspace settings distinct. Locale changes never enqueue translation. Page batch25 bound, duplicates rejected, arbitrary text field rejected.
- [ ] **Step 2 — output failures.** Run T2 marker tests; missing/duplicated/reordered citation/number/URL/symbol; extra JSON fields; HTML/script; article “ignore instructions” text. Model never invokes tools; invalid output falls back to original, no original record mutation.
- [ ] **Step 3 — cache/races.** Same authorized fingerprint one gateway request; changed content/language/model/privacy/workspace/visibility miss. Revoke after cache lookup, before_send, during gateway await and before publish: no unauthorized output. Purge then stale job finish cannot recreate cache.
- [ ] **Step 4 — failures.** 429/timeout retry within fixed budget; invalid schema not infinite retry; capability missing/remote reasoning false/local_only true produce blocked without network. Long Brief exceeds limit → original + input_too_large, not silent truncation.
- [ ] **Step 5 — browser E2E.** Load News page shows original instantly, translate only loaded IDs, next page sends next IDs, show original works. Daily Brief selected day/revision cached independently. Workspace switch/settings-off abort polling and ignores late results.
- [ ] **Step 6 — live model.** With configured allowed OmniRoute alias, review10 News pairs +3 Briefs spanning vi/en, finance numbers/tickers, international names and citations. Record model/alias/time/policy, mechanical invariants and human semantic review separately. No “translation accurate” claim from only token checks.
- [ ] **Step 7 — run.**
```powershell
./scripts/dev.ps1 test -PytestTarget tests/unit/modules/translations/test_protection.py
./scripts/dev.ps1 test -PytestTarget tests/integration/test_content_translation.py
./scripts/dev.ps1 test -PytestTarget tests/integration/test_translation_cleanup.py
./scripts/dev.ps1 test -E2eTarget tests/e2e/content-translation.spec.ts
```
- [ ] **Step 8 — acceptance.** Zero privacy leaks or altered protected values/citations; original remains accessible under current grants; model cost/capability/live result explicit.

## V5 — Production checklist, restore, load và rollout decision

**Files**
- Update: `docs/release-checklist.md`, `docs/deployment.md`, `docs/backup-recovery.md`, `docs/ARCHITECTURE_DECISIONS.md`, `docs/superpowers/plans/EXECUTION.md`, `docs/IMPLEMENTATION_STATUS.md`.
- Create: `scripts/pilot-load.py`, `docs/release/free-data-pilot-acceptance.md`.
- Update setup guide and OSS inventory for any actual dependency/template code used.

- [ ] **Step 1 — source readiness findings.** Re-review baseline/new changes for auth/authorization, HTTP/SSRF, event-loop blocking, bounded DB/HTTP pools, secret/redaction, migration/cleanup, outdated dependency advisories and operational errors. Findings include severity/path/trigger/impact/fix/evidence; no pass based on build alone. Avoid unrelated UI redesign.
- [ ] **Step 2 — complete regression.** Run focused fixes first, then full prescribed test harness, lint/typecheck now that validation stage is open. Any production change requires build and affected regression rerun, then scoped review. Record exact counts/exit codes/SHA, don't copy baseline totals.
```powershell
./scripts/dev.ps1 lint
./scripts/dev.ps1 typecheck
./scripts/dev.ps1 test
./scripts/dev.ps1 build
```
- [ ] **Step 3 — setup/upgrade.** Fresh instance native no n8n; upgrade disposable pre-p14 data; unsupported downgrade rejection; native/n8n overlay compatibility; key rotation/readiness; app restart. Verify docs by following them with a clean operator account rather than remembered local state.
- [ ] **Step 4 — full encrypted restore.** Create test data in two workspaces with shares, sources, observations, briefs, credentials and translations; encrypted backup through existing supported path; restore into distinct named disposable project. Verify counts/hashes/scope/grants/decryption and queued request recovery. Restored schedulers start paused until operator validates, so clone cannot double-collect upstream. Database-only roundtrip is not full restore.
- [ ] **Step 5 — target load.** 2 vCPU/8 GiB,25 users/private workspaces with5 active. Each active workspace10 source configs (six RSS, two crypto, one FX, one macro),25 News/page, one Brief, translation enabled for eligible policy. Test30 minutes with provider/model fakes bounded to documented latency for deterministic infrastructure test, then separate live smoke. Request schedule jitter avoids artificial synchronized bursts except a deliberate recovery phase.
- [ ] **Step 6 — capacity thresholds.** Target for this pilot: no OOM; aggregate container RSS≤6.5 GiB leaving OS headroom; CPU p95≤85% over1-minute samples; cached/local read API p95≤1s and durable enqueue p95≤500ms; schedule dispatch lag p95≤60s excluding provider Retry-After; no growing backlog after workload returns to steady state. External model latency reported separately, not included in local read SLA. Measure at most2 collection admissions/1 workspace and1 translation call simultaneously. These are acceptance targets, not existing measurements.
- [ ] **Step 7 — isolate optional stack impact.** Measure native baseline; then optional browser/n8n enabled profile separately. If optional profile exceeds host target, mark that profile unsupported at pilot capacity; do not silently increase requirements or claim whole stack fits.
- [ ] **Step 8 — acceptance record.**
```json
{
  "code_build_review": "pending",
  "tenant_isolation": "pending",
  "native_collection": "pending",
  "n8n_optional_live": "pending",
  "enabled_provider_live": "pending",
  "translation_policy_and_fidelity": "pending",
  "encrypted_restore": "pending",
  "target_capacity": "pending",
  "deployment": "not_requested"
}
```
Allowed gate values pending/pass/fail/blocked; attach evidence path/SHA to every pass. Deployment separate.
- [ ] **Step 9 — rollout instructions for later operator action.** Preserve pre-upgrade encrypted backup; maintenance/drain; apply reviewed migration; invalidate sessions; native canary one workspace/one source; expand5 active workspaces only after lag/error/resource checks. On regression pause new collection and translation; retain original reads where safe. Roll back backend through C4; schema/data rollback only via verified restore procedure when necessary, never drop new user data.
- [ ] **Step 10 — final review/report.** Publish feature-level status, remaining blocked optional providers/profile, exact deployment commands and restore reference. No “production ready” if tenant isolation/critical recovery/restore/target capacity mandatory gates fail or lack evidence.
