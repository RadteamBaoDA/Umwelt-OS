# Production Readiness, Free Collectors và Translation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cung cấp pilot nhiều private workspace, thu thập nguồn miễn phí hợp lệ không bắt buộc n8n, dịch News/Daily Brief và evidence production acceptance.
**Architecture:** Giữ FastAPI/PostgreSQL/ARQ/OmniRoute. Tách identity/scope, collection execution và translation derived views theo owner modules; n8n chỉ là backend tùy chọn. Không tạo orchestrator hoặc model gateway mới.
**Tech Stack:** Python 3.12–3.13, SQLAlchemy/Alembic, PostgreSQL/pgvector, Redis/ARQ, httpx, Next.js/React/TypeScript, shadcn, n8n 2.5.2 tùy chọn.
**Spec:** [Design baseline](../specs/2026-10-07-production-collectors-translation-design.md), bổ sung canonical `specs/personal-intelligence-os-spec-v2.md`.

## Global Constraints

- “OSS-first Python deployment for a 2-core / 8 GB host, with OmniRoute as the model gateway.”
- “Use OmniRoute as the model gateway through its OpenAI-compatible API. Do not deploy a second LiteLLM gateway.”
- Một default private workspace/user; invite-only; member chỉ thấy nội dung được share; không shared Chat/Memory.
- Chỉ data sources miễn phí đúng điều kiện; không paid fallback, không khẳng định provider hay model là free khi chưa có evidence.
- Giữ source/version/citation, privacy, idempotency, grants, revision/generation fences, encrypted credentials.
- UI giữ Dashboard / Chat / Settings và shadcn; UI locale en-us/vi-vi độc lập target ngôn ngữ dịch.
- Trong đợt triển khai này áp dụng code/build trước, validation sau theo AGENTS và local implementing skill. Các đoạn test trong plan là cho validation stage, không tạo/run trong production-code stage.
- Không push/deploy/migrate database thật hoặc reset dirty tree theo plan tự động. Commit/merge chỉ khi có chỉ đạo còn hiệu lực; checkpoint ghi local trong EXECUTION.md khi bắt đầu thực thi.
- Mọi production symbol sửa cần GitNexus impact; high/critical phải báo. Public module seams không import foreign ORM. Named functions/components có docstring/JSDoc.
- Baseline SHA 512a3fe68895a54f418a85a0ad5e4df3ccad8144. Recheck HEAD trước thực thi; không coi current snapshot tests là evidence của code mới.

## Review Focus

1. User có membership nhưng chưa có share: list/detail/search/citation/cache/SSE đều không được lộ nội dung — W2/W3/W4 và V1.
2. Worker chết giữa fetch và acknowledgement hoặc Redis mất queue: durable request được retry, không double ingest — C2/C3 và V2.
3. n8n deactivate timeout: không chạy backend native song song — C4 và V2.
4. Feed/market data hợp lệ HTTP nhưng schema/time/unit sai hoặc bị quota: giữ last good, không biến thiếu dữ liệu thành 0 — P1–P4 và V3.
5. Model trả nội dung sai số/citation hoặc quyền bị revoke giữa lúc dịch: original còn nguyên, output không publish — T2/T3 và V4.

## 1. Plan map và dependency

| Thứ tự | Plan | Task | Sản phẩm độc lập để review |
| --- | --- | --- | --- |
| 1 | [Workspace & access](2026-10-07-workspaces-access.md) | W1–W5 | Multiuser migration, scope enforcement, explicit sharing, UI context |
| 2 | [Native collectors & optional n8n](2026-10-07-native-collectors.md) | C1–C5 | Durable polling/manual collection, backend transition, setup |
| 3 | [Free data providers](2026-10-07-free-data-providers.md) | P1–P4 | Catalog, RSS/news, typed macro/market/disaster adapters |
| 4 | [News & Brief translation](2026-10-07-news-brief-translation.md) | T1–T4 | Workspace settings, bounded jobs/cache, original-first UI |
| 5 | [Production validation & rollout](2026-10-07-production-validation.md) | V1–V5 | Security/regression, real-provider, restore/capacity evidence |

Dependency graph:
```text
W1 → W2 → W3 → W4 → W5
W2 → C1 → C2 → C3 → C4 → C5
C2 → P1; P1 + C3 → P2 → P3 → P4
W3 + C2 → T1 → T2 → T3 → T4
W5 + C5 + P4 + T4 → V1 → V2 → V3 → V4 → V5
```
C1 fixes có thể phát triển sớm trên baseline nhưng tích hợp sau W2 để không bypass mới scope. T1–T4 độc lập adapter mới, có thể phát triển khi W3 contract và C2 migration ổn định. P1 quota-ledger additions tích hợp cùng C2 trước khi migration được áp dụng; nếu revision đã áp dụng thì tạo revision nối tiếp, không sửa migration lịch sử. Shared schema/migration/API client owner chỉ một implementer tại một thời điểm; build serialize.

## 2. File structure và trách nhiệm

| Owner | Giữ/reuse | Tạo mới |
| --- | --- | --- |
| Identity | core/auth/{models,dependencies,service,routes,schemas,google}.py | core/workspaces/{__init__,models,schemas,public,dependencies,routes,access}.py |
| Scope | domain public.py, queries/models, core/realtime.py, core/events.py | docs/workspace-access-matrix.md; scope migrations |
| Collection | modules/connectors/{public,models,registry,activation,provisioning,provisioning_routes,routes,worker,n8n}.py | collection_schemas.py, collection.py, scheduler.py, backends.py, providers/rest.py |
| Providers | provider catalog, knowledge/documents schemas, observations public | provider_specs.py; providers/{macro,crypto,disasters}.py; docs/connectors/free-sources.md |
| Translation | settings/news/dashboard public contracts, ModelGateway | modules/translations/{__init__,models,schemas,public,worker,routes}.py |
| UI | core/api.ts, realtime-provider, source editor, story list, daily-brief | workspace-switcher, workspace-members, translation-settings, use-content-translation |
| Validation | scripts/dev.ps1 test isolated harness; existing fixtures | focused unit/integration/E2E suites; docs/release/free-data-pilot-acceptance.md |

Đường dẫn đầy đủ từng task trong plan con. “Create” nghĩa là artifact mới; không giả định nó đã tồn tại.

## 3. Stage, migration và rollout

- [ ] **M0 — baseline/checkpoint:** đọc AGENTS, spec baseline, EXECUTION, status; git status/HEAD/worktrees; ghi task hiện tại + deferred/live gates vào ledger, không sửa report cũ thành completed.
- [ ] **M1 — code/build:** W/C/P/T, từng task build và scoped review; không tests/lint/typecheck trong stage này.
- [ ] **M2 — validation:** V1–V4 mới tạo fixtures/tests và chạy focused validation; cuối stage chạy regression harness. Fix failure rồi chỉ rerun vùng bị ảnh hưởng + integration gates phù hợp.
- [ ] **M3 — operational acceptance:** V5 với disposable restore và target host; không claim production nếu còn scope leak, dữ liệu mất, capacity hoặc restore chưa kiểm.
- [ ] **M4 — rollout có kiểm soát:** sau acceptance và chỉ đạo deploy riêng; một workspace/source native trước, rồi workload pilot; không public signup/billing.

Migration chain dự kiến:
```text
p12_evidence_version_index
→ p14_identity
→ p14_workspace_scope
→ p14_collection
→ p14_translation
```
Một người tích hợp migrations; nếu baseline Alembic thay đổi thì nối head đã xác minh, không sửa revision cũ. W1 tạo default workspace trước, W2 backfill/quarantine dữ liệu mồ côi rồi mới NOT NULL/constraints. Legacy source mặc định n8n, source mới chọn native theo capability. Schema downgrade chặn nếu có nhiều account/workspace hoặc payload không biểu diễn được; recovery bằng restore đã kiểm chứng, không xóa tenant để ép downgrade.

Thời điểm cutover identity: chặn writes/scheduler theo maintenance admission hiện có, drain/fence jobs, migrate, invalidate auth sessions, restart đúng phiên bản mới. Không dùng mixed old/new workers với schema tenant mới.

## 4. Task completion và câu lệnh chuẩn

Mỗi task code kết thúc khi: public interface khớp plan; build pass; reviewer không còn P0/P1; limitations ghi rõ. Điều này chưa phải runtime acceptance.
```powershell
git status --short
git rev-parse HEAD
./scripts/dev.ps1 build
```
Không tự điền env secrets để build hoặc đọc .env vào log. Build cần cấu hình thì dùng cách đã được operator cho phép và ghi đúng giới hạn.

Validation examples, chỉ ở M2:
```powershell
./scripts/dev.ps1 test -PytestTarget tests/integration/test_workspace_isolation.py
./scripts/dev.ps1 test -PytestTarget tests/integration/test_native_collection.py
./scripts/dev.ps1 test -PytestTarget tests/integration/test_content_translation.py
./scripts/dev.ps1 test
```

Ghi từng task: SHA, code/review/build, validation chưa chạy/đã chạy, live status, remaining findings, next ID. Không commit từng dòng checkpoint; batch với milestone khi được yêu cầu. Plan writing hiện không thay đổi EXECUTION hay source code.

## 5. Định nghĩa hoàn tất

Tất cả task có checklist rõ trong plan con: W5 + C5 + P4 + T4 = 18 code tasks, V5 = 5 validation tasks, tổng 23. Cho phép bỏ qua live source bị điều kiện license không đáp ứng nhưng phải disabled rõ và không tuyên bố coverage. Không bỏ qua tenant isolation, retry/idempotency, original/citation, restore hay capacity bằng disclaimer.

Khi triển khai, ưu tiên subagent-driven theo local implementing skill, chia mỗi task finite scope và reviewer riêng; không spawn agent chỉ để viết plan này. Người dùng hiện chỉ yêu cầu tài liệu, chưa chọn bắt đầu thực thi.

## 6. Self-review của tài liệu — 2026-10-07

- [x] Coverage: private workspace/invite/share W1–W5; n8n hiện trạng/fixes/native/transition C1–C5; GitHub/free endpoints/terms/setup P1–P4; News + Brief/settings/privacy T1–T4; production review/tests/live/restore/capacity V1–V5.
- [x] 23 task IDs không trùng, 18 code tasks và 5 validation tasks; mọi task có files, deliverable và acceptance scenarios.
- [x] Đã đối chiếu context/revision/job/backend interfaces và migration dependency; sửa WorkspaceRead.configuration_revision, TranslationSettingsUpdate và thứ tự C2→P1/T1.
- [x] Internal Markdown links tồn tại; code fences cân bằng; Python snippets parse được AST và JSON examples parse được. Đây là kiểm tra tài liệu, không phải build/runtime/test acceptance.
- [x] Không có placeholder triển khai; reference-only providers và blocked live gates ghi rõ thay vì để executor tự chọn provider trả phí.
- [x] Chỉ tạo spec snapshot và sáu plan files; không sửa production code hoặc ghi đè dirty changes có sẵn. Không commit, test, build, migrate hoặc deploy trong lần viết plan.
