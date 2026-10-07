# Private Workspaces và Explicit Sharing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Mỗi user có private workspace riêng và được mời đọc đúng nội dung đã chia sẻ ở workspace khác.
**Architecture:** Mở rộng account hiện có, thêm membership và access context; data owners kiểm tra scope ở query và publish. Không dựa vào UI filter hoặc membership đơn thuần để cấp quyền dữ liệu.
**Tech Stack:** FastAPI, SQLAlchemy/Alembic, PostgreSQL, existing auth/CSRF, Next.js/shadcn.
**Spec:** [Design baseline](../specs/2026-10-07-production-collectors-translation-design.md); [master](2026-10-07-production-readiness-master.md).

## Global Constraints

- Kế thừa mọi Global Constraints của master; code/build trước, V1 mới tạo/run tests.
- Owner của workspace quản lý nội dung; member read-only explicit Document/Brief shares.
- Chat/Memory/personal OAuth credentials luôn private; operator instance permissions tách workspace owner.
- Không thêm public signup, billing, organization role hierarchy, workspace ownership transfer hoặc shared chat.
- Mỗi account một default workspace; v1 không hỗ trợ nhiều owned workspaces. Dữ liệu tồn tại được backfill cho legacy owner, không auto-share.

## Review Focus

- Hai user nhìn cùng UUID/cursor/cache key: server phải phân biệt principal và scope — W2/W3/V1.
- Member được mời nhưng chưa share: News/Search không được lộ title/count/citation — W3/V1.
- Revoke trong SSE/gateway request đang chạy: không publish data sau thu hồi — W4/V1/V4.
- Legacy singleton constraints và jobs owner=1: không chạy dữ liệu user mới dưới account cũ — W1/W2/V1.
- Backup/export/operator route: workspace owner mới không lấy được toàn bộ instance — W4/V1/V5.

## W1 — Accounts, default workspace và invitation lifecycle

**Files**
- Modify: `core/auth/models.py`, `core/auth/service.py`, `core/auth/schemas.py`, `core/auth/routes.py`, `core/auth/google.py`, `core/auth/dependencies.py`, `apps/api/main.py`.
- Create: `core/workspaces/__init__.py`, `models.py`, `schemas.py`, `public.py`, `routes.py`.
- Create migration: `infrastructure/postgres/migrations/versions/p14_identity.py`.
- Deferred tests: `tests/unit/core/test_workspace_invites.py`, `tests/integration/test_workspace_identity.py`.

**Interfaces**
- `WorkspaceRead(id: UUID, name: str, owner_user_id: int, is_default: bool, role: Literal["owner","member"], configuration_revision: int)`; configuration_revision tăng khi sửa tên, membership hoặc lời mời để CAS quản trị.
- `GET /api/v1/workspaces` returns `{"items": list[WorkspaceRead]}` for current account only.
- `POST /api/v1/workspaces/{id}/invitations`: `{email, expected_revision}` → 201 `{invitation_id, invitation_url, expires_at}`; token returned only once.
- `DELETE /api/v1/workspaces/{id}/invitations/{invitation_id}` → 204; owner only.
- `POST /api/v1/workspaces/invitations/accept`: `{token,password?}` → 200 membership + new default workspace if new account. Authenticated account email must match; password only for creating invited account.
- `DELETE /api/v1/workspaces/{id}/members/{user_id}` → 204; cannot remove owner.
- Login adds optional identifier; omission resolves legacy bootstrap owner only. Never select “first matching password” across accounts.

- [ ] **Step 1 — schema.** Keep SQL table `owner`, integer PK/password hashes/Google issuer-subject uniqueness. Add nullable normalized email unique, account state, default_workspace_id. Workspace owner unique ensures one owned default/user; workspace configuration_revision defaults1. Membership composite PK(workspace_id,user_id), role check owner/member, revision defaults1; unique owner membership; invitation hash unique, expires/accepted/revoked timestamps.
- [ ] **Step 2 — migration.** Create legacy workspace with new UUID, link owner 1, create owner membership, backfill account default. Preserve old credential hashes and data IDs. Do not invalidate sessions until full W2 cutover. Downgrade checks number of owners/workspaces and raises clear error if data cannot fit singleton schema.
- [ ] **Step 3 — token construction.** Use exact helper in `core/workspaces/public.py`, compare supplied hash inside row lock and verify expiry/revocation/matching email:
```python
from hashlib import sha256
from secrets import token_urlsafe

def new_invitation_secret() -> tuple[str, str]:
    """Return a one-time browser token and its storage-only SHA-256 digest."""
    token = token_urlsafe(32)
    return token, sha256(token.encode("utf-8")).hexdigest()
```
- [ ] **Step 4 — accept atomically.** Lock invitation; reject expired/replayed with 410, revoked with 410, wrong signed-in email 403. New account creation + default workspace + membership + consume invitation one transaction. Unique email races load winning account only after authenticating it; do not join existing account by email alone. Public invite acceptance requires same-origin, one-time token, rate-limit; authenticated acceptance retains CSRF. Never log token/password.
- [ ] **Step 5 — API/auth.** Create scoped routes above, add rate limits to invite accept/login through existing policy if available; otherwise bounded Redis limiter keyed by hashed IP+token prefix, no raw secrets. Google signup only from valid invitation; Google sign-in itself grants no data-source access.
- [ ] **Step 6 — review/build.** Explain invalidation timing and error mapping; run prescribed build. V1 will prove races/replay; keep feature flag `BBD_MULTI_WORKSPACE_ENABLED=false` until W4 scope gate.

**Deferred validation example**
```python
from core.workspaces.public import new_invitation_secret
from hashlib import sha256

def test_invitation_secret_is_stored_only_as_digest():
    token, digest = new_invitation_secret()
    assert len(token) >= 40
    assert digest == sha256(token.encode("utf-8")).hexdigest()
    assert token != digest
```
Integration: two accepts concurrently give one membership, one account/default workspace; repeated token cannot register another account. New account password-only login without identifier cannot log into it.

## W2 — Tenant context và complete persistence scope

**Files**
- Create: `core/workspaces/dependencies.py`, `access.py`, `docs/workspace-access-matrix.md`.
- Modify: `core/auth/dependencies.py`, `core/events.py`, `core/realtime.py`, `core/database.py`.
- Modify owner persistence/query/public modules listed in matrix below; migration `infrastructure/postgres/migrations/versions/p14_workspace_scope.py`.
- Deferred tests: `tests/unit/core/test_workspace_access.py`, `tests/integration/test_workspace_scope.py`.

**Produces**
```python
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

@dataclass(frozen=True)
class WorkspaceContext:
    """Authenticated workspace principal; never construct from client body alone."""
    user_id: int
    workspace_id: UUID
    role: Literal["owner", "member"]
    membership_revision: int
```
Define in `core/workspaces/schemas.py`. Dependencies `require_workspace_read(request, session) -> WorkspaceContext` and `require_workspace_write(request, session) -> WorkspaceContext` resolve membership and existing CSRF/backup admission. Write requires owner unless a route is explicitly account-scoped. Context is explicit input of domain public methods; no global `current_owner` variable.

- [ ] **Step 1 — classify all mapped tables and entrypoints.** Produce matrix with one row per mapped table and route/job/tool, not only folders. Use SQLAlchemy metadata in a non-production audit script at validation, and these root ownership rules now:

| Domain | Scope/root strategy | Public access rule |
| --- | --- | --- |
| sources/connectors/ingestion | sources.workspace_id; descendants via source FK; durable detached records carry workspace_id | Workspace owner only; member never sees credential/config |
| documents/versions/chunks/observations | inherit source workspace; detached cleanup/export/evidence rows retain workspace_id | Owner or explicit document share; raw URLs subject to same rule |
| news/topics/relevance | story/topic roots workspace_id; never cluster across workspaces | Member stories projected only from shared documents; no hidden counts |
| dashboard/briefs | roots workspace_id + creator user_id where personal; schedule per owner workspace | Member only shared saved Brief revisions; no global dashboard exposure |
| tasks/goals/entities/timeline | root workspace_id, child joins through roots | Owner-only in v1; entity names not leaked through shared citations |
| chat/memory | user_id + default workspace; all runs/streams/checkpoints inherit | Current user/default workspace only |
| agents/tools/MCP/automations/notifications | root workspace_id/user_id, retained approvals/effects/jobs carry scope | Owner-only; worker/agent cannot broaden source permissions |
| settings/model/privacy | workspace-scoped AI/privacy/settings; personal preferences remain user-scoped | Member no secrets/settings edits; translation settings read projection only |
| search/graph/vector | workspace + principal-aware authorized document IDs | Apply filter before ranking/model calls; Graphiti namespace per workspace |
| backup/system/observability | instance operations bootstrap operator only; personal exports scoped | User cannot enumerate cross-workspace logs/raw trace/backup contents |

- [ ] **Step 2 — expand/backfill/contract migration.** Add nullable scope to roots/detached rows; backfill all legacy rows from legacy default workspace; fail migration with table/count if orphan lineage cannot be resolved. Add NOT NULL, scoped indexes/uniques and workspace-consistent composite FK where a row carries both workspace and parent. Owner-specific singleton checks removed only where account/workspace-scoped; retain deliberate instance-global singleton controls.
- [ ] **Step 3 — update every public seam.** Require WorkspaceContext or explicit internal job scope. Reject missing context by default; legacy fallback only bootstrap owner default as specified. No endpoint may authorize by existence of UUID alone. Domain owners keep their own queries/ORM; cross-module calls pass detached DTOs.
- [ ] **Step 4 — scoped identities.** Include workspace in dedup, search index IDs, vector filters, graph namespace, cursor fingerprints, Redis caches, document storage prefix and outbox payload. Preserve existing source-local identity (already unique source ID); do not duplicate existing records solely because key format changes.
- [ ] **Step 5 — freeze identity cutover.** Maintenance admission stops writes/new jobs; drain/fence, migrate, invalidate all sessions, deploy same-version API/worker, require fresh login. Old payload lacking scope maps only through proven legacy source→workspace lineage; otherwise quarantine, not owner 1 fallback.
- [ ] **Step 6 — review/build.** Matrix must contain zero unclassified tables/routes/jobs. Explicitly review all `owner_id = 1`, singleton constraints and unscoped select/cache calls; do not mass replace numeric IDs.

**Deferred unit assertion**
```python
from uuid import uuid4
from core.workspaces.schemas import WorkspaceContext

def test_context_is_immutable():
    from dataclasses import FrozenInstanceError
    import pytest
    scope = WorkspaceContext(1, uuid4(), "owner", 1)
    with pytest.raises(FrozenInstanceError):
        scope.user_id = 2
```
V1 integration needs two owners with same document external_id, same topic/title and same search text: list/search/vector/graph/cursors cannot cross scope.

## W3 — Explicit shares và permission-safe News/Brief projections

**Files**
- Modify: `core/workspaces/models.py`, `public.py`, `access.py`, `routes.py`; W2 migration before publication.
- Modify: `modules/knowledge/documents/public.py`, `modules/news/stories.py`, `modules/news/public.py`, `modules/search/public.py`, `modules/dashboard/briefs.py`.
- Deferred tests: `tests/unit/core/test_workspace_access.py`, `tests/integration/test_workspace_sharing.py`.

**Interfaces**
- `PUT /api/v1/workspaces/{id}/shares/{resource_type}/{resource_id}/{member_user_id}`, type `document|brief`, body `{expected_revision, resource_revision}` → share DTO; DELETE same path → 204.
- `can_read_resource(scope: WorkspaceContext, resource_workspace_id: UUID, shared_to_actor: bool) -> bool`.
- `assert_resource_access(session, scope, kind, resource_id, expected_revision=None) -> None` raises 404 for invisible resource, 409 for visible stale revision.
- Document share is resource-level and includes current revisions while grant active. Brief share binds a saved immutable revision. UI states these semantics before saving.

- [ ] **Step 1 — authorization predicate.** Keep membership lookup in dependency; resource predicate:
```python
def can_read_resource(scope, resource_workspace_id, shared_to_actor):
    """Membership alone does not grant a member access to a resource."""
    return (
        scope.workspace_id == resource_workspace_id
        and (scope.role == "owner" or shared_to_actor)
    )
```
Implement with typed signature from Interfaces; no database hidden inside predicate.
- [ ] **Step 2 — shares.** Store composite unique(workspace,resource_type,resource_id,member), revision, grantor, revoked_at. Reject target member outside workspace; resource owner module verifies exact resource scope/revision before grant. Regrant increments revision. Missing or revoked grants never imply access.
- [ ] **Step 3 — news/search.** Filter authorized document versions before story title/excerpt/cluster count/relevance projection. Member gets no private evidence count, source config, entity graph, or aggregate revealing hidden records. Search authorization occurs before pagination/ranking; any result enrichment rechecks scope.
- [ ] **Step 4 — Brief dependency rule.** Refuse share with 409 `brief_evidence_not_shared` if any cited content is unavailable/unshared for recipient. Saved brief output is visible only while all its dependencies remain authorized; revoke any dependency hides brief and its translations. Do not recompute member-specific text silently.
- [ ] **Step 5 — revoke fences.** Membership/share revisions included in read fingerprints. Return reads after fresh authorization; stale projection cannot be published or cached. Update deletion hooks through owning public contracts.
- [ ] **Step 6 — review/build.** Independent review checks list/detail/raw download/export paths and source-title leakage.

**Deferred test**
```python
from uuid import uuid4
from core.workspaces.schemas import WorkspaceContext
from core.workspaces.access import can_read_resource

def test_membership_without_share_is_not_read_access():
    workspace_id = uuid4()
    member = WorkspaceContext(2, workspace_id, "member", 1)
    assert not can_read_resource(member, workspace_id, False)
    assert can_read_resource(member, workspace_id, True)
    assert not can_read_resource(member, uuid4(), True)
```

## W4 — Background jobs, AI, SSE và export authorization

**Files**
- Modify: `apps/worker/main.py`, `core/realtime.py`, `core/realtime_routes.py`, `modules/ingestion/public.py`, `modules/settings/public.py`.
- Modify: `modules/{chat,memory,agents,tools,automations,notifications,backup}/public.py` where present; tool dispatch `modules/tools/mcp_dispatch.py`; realtime/UI client handled W5.
- Modify owner cleanup/backup/export modules named in W2 matrix, avoiding foreign ORM access.
- Deferred tests: `tests/integration/test_workspace_background.py`, `tests/integration/test_workspace_export.py`.

**Consumes:** WorkspaceContext, scoped domain DTOs, share revisions W2/W3.
**Produces:** every job/event payload includes workspace_id and subject user_id or proven source identity; realtime subscription scoped to current principal/workspace.

- [ ] **Step 1 — worker admission.** Job reloads current subject/workspace/source, checks enabled/membership/revision, then obtains domain lease. Do not trust role serialized at enqueue. Permission loss cancels rather than retries indefinitely.
- [ ] **Step 2 — commit/publish fence.** Capture policy revision before external I/O, release transaction during network; recheck on return before durable publish. Retain existing source generation, deletion and maintenance fences.
- [ ] **Step 3 — AI isolation.** Scope gateway settings/cache capability, retrieval, graph, tools and memory by workspace/principal. Preserve before_send callback on every retry/fallback. Shared-document questions run in user's private Chat with explicitly authorized retrieval, never another user's Memory.
- [ ] **Step 4 — SSE.** Authenticate query workspace_id, bind replay cursor to user/workspace/revision. Recheck membership on each delivery/replay and heartbeat (15 s); stop stream on revoke. Unknown workspace or wrong cursor cannot fall back to global replay.
- [ ] **Step 5 — export and deletion.** Ordinary export restricted to user-owned workspace; member cannot export another workspace wholesale. Instance encrypted backup operator-only. Resource deletion removes grants/translation-derived copies via owner hooks; tombstones prevent stale jobs resurrecting them.
- [ ] **Step 6 — review/build.** Internal service functions do not construct owner contexts from constant 1. Document every operator-only route in matrix.

**Job payload example**
```json
{"workspace_id":"8d939a2f-ad12-4b69-aa5d-38115dc7fd4a","actor_user_id":2,"resource_id":"a3489cc3-c5ab-4e53-b285-faaf80f04515","expected_revision":3}
```
IDs are illustrative contract data, never a seed or runtime owner assumption. V1 uses queued-job revocation, SSE replay, cross-workspace export and privacy race tests.

## W5 — Workspace UX và invitation/share controls

**Files**
- Create: `apps/web/src/core/app-shell/workspace-switcher.tsx`, `apps/web/src/core/workspace-context.tsx`, `apps/web/src/modules/settings/workspace-members.tsx`, `apps/web/src/modules/knowledge/document-sharing.tsx`.
- Create: `apps/web/src/app/invite/page.tsx` for token acceptance; add authenticated CSRF and anonymous one-time invitation flows to existing auth API helper.
- Modify: `apps/web/src/core/api.ts`, `apps/web/src/core/realtime-provider.tsx`, `apps/web/src/core/app-shell/workspace-shell.tsx`, `apps/web/src/modules/knowledge/document-detail.tsx`, `apps/web/src/modules/dashboard/daily-brief.tsx`, auth pages/helpers that call LoginRequest.
- Create localized catalog `apps/web/src/core/messages/workspaces.ts`, wire existing catalog owner.
- Deferred E2E: `tests/e2e/workspaces.spec.ts`.

**Interfaces**
```typescript
type WorkspaceSelection = {
  id: string;
  role: 'owner' | 'member';
  revision: number;
};
```
Context exposes current selection and selectWorkspace(id); API injects X-Workspace-ID. Selection state per browser tab, no global mutable server module variable.

- [ ] **Step 1 — selector.** Load only GET workspaces results, default own private workspace. Owner workspace shows complete app; invited workspace shows Shared Documents/Brief content through existing surfaces. Hide unsupported controls but keep server enforcement.
- [ ] **Step 2 — switch lifecycle.** Abort in-flight fetches; close previous SSE; clear private query/render caches; switch header; fetch new scoped snapshot; reopen scoped SSE. Ignore late responses tagged with old selection revision.
- [ ] **Step 3 — invite UX.** Owner enters email, copies one-time invitation link; expiry/revocation shown. No email sending provider added. Existing signed-in wrong email is explained without exposing invitation identity broadly.
- [ ] **Step 4 — shares UX.** Explicit member selection, current document vs immutable brief semantics shown; unshared dependencies error links only owner-visible documents. No “share entire workspace” toggle.
- [ ] **Step 5 — errors/accessibility.** 401 logout; 403 role denied; 404 invisible resource; 409 refresh revision; revoked membership returns to private workspace. shadcn dialogs/focus, mobile selector, vi/en UI catalogs.
- [ ] **Step 6 — build/review.** Feature remains operator gated until V1 proves isolation; provide review screenshots only during implementation, not synthetic runtime claims.

**Deferred E2E scenario:** A shares one doc with B; B sees only that doc in A's workspace, cannot see A's sources/Chat/Memory, switches to own workspace and uses own Chat. Revoke while B has doc open clears access and later translation/SSE responses. V1 implements this with two isolated browser contexts.
