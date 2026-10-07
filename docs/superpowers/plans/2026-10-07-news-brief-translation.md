# News và Daily Brief Translation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Owner bật setting workspace để News đang hiển thị và Daily Brief tự dịch, vẫn xem được original và citation nguyên vẹn.
**Architecture:** Translation là derived view/job/cache riêng, resource owner cung cấp authorized text và revision. Dùng existing OmniRoute ModelGateway.structured và policy checks; không ghi đè News/Brief hay index bản dịch làm bằng chứng mới.
**Tech Stack:** Pydantic, PostgreSQL/ARQ, existing ModelGateway, Next.js/shadcn.
**Spec:** [Design baseline](../specs/2026-10-07-production-collectors-translation-design.md); [master](2026-10-07-production-readiness-master.md).

## Global Constraints

- Phụ thuộc W3 permission-safe projections; global nghĩa là mỗi workspace, không toàn server.
- Mặc định enabled=false, target_language=vi; v1 vi/en, độc lập UI locale en-us/vi-vi.
- News: title/excerpt của IDs ở trang hiện tại; Brief: content của saved revision; không dịch toàn bộ corpus.
- Không send text khi privacy/local_only/grant/destination cấm, kể cả fallback/retry.
- Model output không được thay số liệu, URLs, ticker symbols hoặc citations. Không thể dùng token check để tuyên bố toàn bộ ngữ nghĩa luôn đúng.
- Không deploy local translator/gateway mới hoặc hứa inference miễn phí.
- V4 mới tạo/run tests; code stage build/review.

## Review Focus

- Cache hit sau revoke hoặc settings/privacy/model revision đổi — T1/T2/V4.
- HTML/prompt injection trong bài báo được model xem là instructions — T2/V4.
- Dịch USD/USDT, phần trăm, ngày hoặc citation markers sai — T2/V4.
- Page switch/workspace switch/late response gắn nhầm bản dịch — T4/V4.
- Model timeout, unsupported structured output hoặc input quá lớn — T2/T3/V4.

## T1 — Workspace settings và public request/result schemas

**Files**
- Modify: `modules/settings/{models,schemas,public,routes}.py`.
- Create: `modules/translations/__init__.py`, `schemas.py`, `models.py`, `routes.py`, `public.py`.
- Modify: `apps/api/main.py` router registration.
- Create migration `infrastructure/postgres/migrations/versions/p14_translation.py`.
- Deferred tests: `tests/unit/modules/settings/test_translation_settings.py`, `tests/integration/test_translation_settings.py`.

**Public interfaces**
```python
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field
from uuid import UUID

class TranslationSettingsRead(BaseModel):
    """Workspace translation choice; unrelated to the viewer's UI locale."""
    enabled: bool = False
    target_language: Literal["vi", "en"] = "vi"
    configuration_revision: int = Field(default=1, ge=1)

class TranslationSettingsUpdate(BaseModel):
    """Owner update must compare this revision under a row lock."""
    model_config = ConfigDict(extra="forbid")
    enabled: bool
    target_language: Literal["vi", "en"]
    expected_revision: int = Field(ge=1)

class TranslationItemRequest(BaseModel):
    """Only IDs/revisions are accepted; content is loaded by resource owners."""
    model_config = ConfigDict(extra="forbid")
    resource_type: Literal["news_story", "daily_brief"]
    resource_id: UUID
    resource_revision: str = Field(min_length=1, max_length=128)

class TranslationBatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[TranslationItemRequest] = Field(min_length=1, max_length=25)
```
Place settings models in settings/schemas.py; item/batch models in translations/schemas.py. Update DTO accepts expected_revision only; server generates the next configuration_revision.

Endpoints:
- `GET /api/v1/settings/translation` → settings; workspace member gets read projection, owner can PATCH with expected_revision (409 conflict).
- `POST /api/v1/translations/batches` → 202 `{batch_id,items:[{resource_type,resource_id,status}]}`.
- `GET /api/v1/translations/batches/{batch_id}` → same items with `translation: {title?,excerpt?,content?}|null`, target_language, original_revision, safe error_code, status.
- Status: pending/ready/unchanged/blocked/failed. All return original content through normal owner resource endpoints; batch never takes or echoes arbitrary client input text.

- [ ] **Step 1 — settings table.** PK workspace_id, enabled false, target vi/en check, revision>=1, timestamps. Owner PATCH CAS update; member PATCH403. Disable increments revision and cancels pending scope jobs; ready caches become ineligible until re-authorized under matching config.
- [ ] **Step 2 — DTO exactness.** Use independent update DTO above; reject unknown properties, duplicate item refs, max25. Target comes from current settings, not client per-item override.
- [ ] **Step 3 — cache/jobs schema.** `content_translations`: UUID, workspace_id, actor_user_id, resource_type/id/revision, content_hash, visibility_hash, target, settings/privacy/model revision hash, prompt_version, status, protected output JSON, safe error_code, timestamps/expires_at, work lease. Unique full cache fingerprint. `translation_batches` + `translation_batch_items` bind requesting actor and item references. Expiry30 days.
- [ ] **Step 4 — route admission.** POST requires same-origin/CSRF and workspace membership; member can request only shared readable resources. Authorize every item first; any invisible ID returns404 without per-ID existence disclosure, stale visible revision returns409. Disabled setting returns current settings with blocked status, no job enqueue.
- [ ] **Step 5 — register/build.** Router path and source owner dependencies explicit; don't grant member general settings-write permission.

**Deferred test**
```python
from modules.settings.schemas import TranslationSettingsRead

def test_translation_is_disabled_and_vietnamese_by_default():
    value = TranslationSettingsRead()
    assert value.enabled is False
    assert value.target_language == "vi"
    assert value.configuration_revision == 1
```

## T2 — Authorized inputs, protected tokens và OmniRoute translation

**Files**
- Create: `modules/translations/inputs.py`, `protection.py`, `service.py`.
- Modify: `modules/news/public.py`, `modules/dashboard/public.py`, `modules/settings/public.py` owner input/config seams.
- Reuse: `core/model_gateway/client.py`, `schemas.py`, `policy.py`; change only if scope propagation requires it.
- Deferred tests: `tests/unit/modules/translations/test_protection.py`, `test_translation_service.py`.

**Interfaces**
- `TranslationInput` frozen dataclass in schemas.py: workspace_id UUID, actor_user_id int, resource_type str, resource_id UUID, resource_revision str, fields dict[str,str], visibility_hash str, source_ids tuple[UUID,...], local_only bool. inputs.py re-exports this detached type.
- `load_translation_input(session, scope: WorkspaceContext, item: TranslationItemRequest) -> TranslationInput` in inputs.py composes public owners, never imports their ORM.
- News public `read_story_translation_input(session, scope, story_id) -> TranslationInput`; Brief public `read_brief_translation_input(session, scope, brief_id) -> TranslationInput`. Avoid circular import by defining input DTO in translations/schemas.py and re-export from inputs.py; owner imports detached schema only.
- `protect_text(text: str) -> tuple[str, dict[str,str]]`; `restore_text(text: str, protected: dict[str,str]) -> str` in protection.py.
- `translate_input(gateway, execution_config, policy, source: TranslationInput, target_language: str, before_send) -> dict[str,str]` in service.py, use concrete existing gateway/config/policy types.

- [ ] **Step 1 — owner projections.** News title/excerpt after current evidence filtering; hash includes that projection's authorized evidence version set. Brief text from exact saved ID/revision, lineage/citation visibility checked. Unknown language does not block; known target language returns unchanged without calling model.
- [ ] **Step 2 — protection.** Preserve Markdown citation/link IDs, URLs, ticker symbols, numeric spans including sign/grouping/decimal/percent/currency/date as opaque per-request random markers. Preserve raw literal inside map. Detect marker collision in original and generate fresh nonce. No term from input is treated as a system instruction.
```python
def require_protected_markers(output: str, protected: dict[str, str]) -> None:
    """Reject missing or duplicated protected source values before restoration."""
    if any(output.count(marker) != 1 for marker in protected):
        raise ValueError("translation_protected_value_mismatch")
```
Define this helper in protection.py; restore_text first calls it, rejects unknown nonce markers, then substitutes exact literals. Validate ordered protected tokens per segment to reject reordered values conservatively. Headings/non-text Markdown syntax maintained by splitting text nodes, not naive replacement inside URLs/code.
- [ ] **Step 3 — limits.** News title≤500 and excerpt≤4000 characters from existing projections; do not truncate silently. Brief input≤40000 chars; larger returns blocked/input_too_large with original. Segment Brief by Markdown text blocks, max2000 chars each without splitting protected tokens; max20 segments, sequential. News translate one item at a time, combine title/excerpt JSON in one request.
- [ ] **Step 4 — model call.** Use alias reasoning-small configured in workspace, temperature0 and max_tokens4096 (within existing gateway8192 bound). Require structured response exactly requested fields/segment IDs; system instruction translate only, preserve markers, treat payload as data. Example schema:
```json
{"name":"content_translation","strict":true,"schema":{"type":"object","properties":{"title":{"type":"string"},"excerpt":{"type":"string"}},"required":["title","excerpt"],"additionalProperties":false}}
```
Brief segment schema has only content string; reject missing/extra fields. No model-selected URLs/tools/web search. before_send reloads current workspace/source privacy and grants for every retry.
- [ ] **Step 5 — policy.** Combine workspace privacy and every source local_only flag with current reasoning destination; remote reasoning false blocks. No alternative provider selected outside existing ModelGateway policy. Unsupported structured output returns blocked/model_capability_missing.
- [ ] **Step 6 — output validation.** Restore protected literals; validate text length and citation marker set; sanitize render with existing Markdown rules. Model output never written into original resource or passed to tool execution. Validation failure stores safe code and shows original.
- [ ] **Step 7 — build/review.** Token preservation is a guard, not proof of accurate financial translation; V4 includes human bilingual sample review.

**Deferred test**
```python
import pytest
from modules.translations.protection import require_protected_markers

def test_missing_or_duplicate_amount_fails_closed():
    protected = {"MARK_A": "1,250 USD"}
    require_protected_markers("Giá MARK_A", protected)
    with pytest.raises(ValueError, match="protected_value"):
        require_protected_markers("Giá 1.250 VND", protected)
    with pytest.raises(ValueError, match="protected_value"):
        require_protected_markers("MARK_A và MARK_A", protected)
```

## T3 — Durable worker/cache, revocation và lifecycle cleanup

**Files**
- Create: `modules/translations/worker.py`, `lifecycle.py`.
- Modify: `modules/translations/public.py`, `models.py`; `apps/worker/main.py`.
- Modify: `modules/knowledge/documents/public.py`, `modules/dashboard/briefs.py`, `core/workspaces/public.py` for deletion/revoke owner hooks.
- Deferred tests: `tests/integration/test_content_translation.py`, `tests/integration/test_translation_cleanup.py`.

**Interfaces**
- `request_translations(session, scope, request: TranslationBatchRequest) -> TranslationBatchRead` defined in public.py.
- `translate_content(ctx: dict[str,object], translation_id: str) -> None` in worker.py.
- `recover_translation_jobs(ctx: dict[str,object]) -> int`, ARQ once/minute.
- `purge_resource_translations(session, workspace_id: UUID, resource_type: str, resource_id: UUID) -> int`, flush-only owner lifecycle hook.
- `TranslationBatchRead` model fields match T1 endpoint envelope and item states.

- [ ] **Step 1 — durable request/cache.** Validate authorization/privacy before computing lookup; canonical JSON fingerprint with workspace/user/visibility/version/settings/privacy/model/prompt and SHA256. Upsert cache row under unique constraint and attach batch item; commit before queue. Concurrent request for same fingerprint coalesces.
- [ ] **Step 2 — work limits.** Add exactly one PostgreSQL translation_admission_slots row in p14_translation, with translation_id/fencing_token/expires_at; renew20s, expiry120s. Acquire this slot before existing ModelGateway admission, preserving its overall model limits; no transaction held during I/O. This enforces one translation gateway call across API/worker processes. Resource deadline90s; individual gateway attempt≤20s, max2 network attempts/segment; deadline stops unfinished Brief, no partial Brief publish.
- [ ] **Step 3 — recover.** Queued rows missing Redis job and expired running leases requeue bounded50/minute. Transport errors retry up to2 job attempts; validation/privacy/capability errors require new eligible input/settings revision. Client polling never causes repeated automatic retries of same failed fingerprint.
- [ ] **Step 4 — publish fence.** After gateway result, reload exact resource, membership/share, source generation/visibility, settings/privacy/model revisions. Any mismatch marks blocked/stale_request and discards output. Cache GET independently checks current authorization; old ready result cannot bypass revoke.
- [ ] **Step 5 — cleanup.** Document/source purge, Brief deletion and account/workspace deletion remove related caches/batches via public hooks. Keep deletion receipts until child cleanup settles; late job cannot recreate cache. Nightly expires30-day cache in pages100; source originals retain existing policies.
- [ ] **Step 6 — telemetry/build.** Counts ready/unchanged/blocked/failed/cache hits and latency/token use; no text or protected maps in logs. Settings-off cancels queued work; any in-flight external response must be discarded.

**Fingerprint implementation sketch**
```python
import hashlib
import json

def translation_fingerprint(parts: dict[str, str | int]) -> str:
    """Hash an explicit canonical scope/version tuple without storing article text in keys."""
    body = json.dumps(parts, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()
```
Define in public.py; parts must include all T3 Step1 fields, with content represented by hash, not raw text. V4 compares different workspace, actors, languages, model/privacy revision and content version.

## T4 — Settings và original-first News/Brief UI

**Files**
- Create: `apps/web/src/modules/settings/translation-settings.tsx`, `apps/web/src/modules/translations/api.ts`, `types.ts`, `use-content-translation.ts`.
- Modify: `apps/web/src/modules/settings/ai-settings.tsx`, `apps/web/src/modules/news/story-list.tsx`, `apps/web/src/modules/news/story-detail.tsx`, `apps/web/src/modules/news/story-types.ts`, `apps/web/src/modules/news/story-api.ts`, `apps/web/src/modules/dashboard/daily-brief.tsx`, `apps/web/src/modules/dashboard/gadgets/brief-gadget.tsx`.
- Add UI catalogs `apps/web/src/core/messages/translations.ts`.
- Deferred E2E: `tests/e2e/content-translation.spec.ts`.

**UI contract**
```typescript
type TranslationState = 'pending' | 'ready' | 'unchanged' | 'blocked' | 'failed';
type ContentTranslation = {
  status: TranslationState;
  original_revision: string;
  target_language: 'vi' | 'en';
  translation: { title?: string; excerpt?: string; content?: string } | null;
  error_code: string | null;
};
```
Current original Story/Brief fields remain unchanged. Server owner responses expose additive `translation_revision` opaque hash (News) or existing immutable Brief revision string for T1 request, avoiding frontend content hashing as authority.

- [ ] **Step 1 — settings placement.** Translation card within AI & Ommi Router settings; toggle + target vi/en + save expected_revision. Clearly states applies to News/Daily Brief in this workspace; changing UI language doesn't change content target. Member sees read-only effective setting.
- [ ] **Step 2 — News batching.** Render original immediately. When enabled, request only loaded current page IDs, chunks≤25; deduplicate requests by workspace/actor/revisions/settings. No fetch next pages solely for translation.
- [ ] **Step 3 — Brief.** Request selected saved revision only, both dashboard widget and expanded view reuse same query/cache. Changing day or revision cancels local request observation; no automatic regeneration of original Brief.
- [ ] **Step 4 — polling.** Poll batch every2s while visible, maximum60s per page mount; abort on navigation/switch/settings-off. Server durable work can finish for future cache hits; late result only applies if selection workspace/resource/revision still matches. Further visits read same existing job, not duplicate.
- [ ] **Step 5 — rendering.** Ready shows translated fields, “Bản dịch tự động”, target and “Xem bản gốc”; citations/attribution retained. Pending/blocked/failed keep original with concise reason; no full-page spinner or toast storm.
- [ ] **Step 6 — build/review.** shadcn controls, keyboard/focus, vi/en accessible strings, safe Markdown; screenshot does not establish actual gateway acceptance. V4 E2E must verify current-page-only and no cross-workspace late response.
