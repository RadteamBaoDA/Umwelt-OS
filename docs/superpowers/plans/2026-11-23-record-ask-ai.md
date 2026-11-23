# Per-record Ask AI (anchored) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One Ask AI button on every document-backed dashboard row that opens chat anchored on that record while normal workspace retrieval stays on.

**Architecture:** The existing `selection` chat context gains `anchor: true` (one item). The server resolves it with the same Documents/fence/local-only checks, then returns `selected_only: False`, an empty `source_scope` and a server-derived `anchored: True`. Retrieval collects anchor refs first, caps them (20 chunks, half the byte budget) and keeps them ahead of reranked search results. The frontend gets one shared `AskRecordButton` used by every row.

**Tech Stack:** FastAPI/Pydantic, pytest; Next.js/React/TypeScript, next-intl, Vitest.

**Spec:** `docs/superpowers/specs/2026-11-23-record-ask-ai-design.md`

## Global Constraints

- Anchor accepted only with exactly one item; otherwise 422 with `_selection_detail` code `anchor_requires_single_item`.
- Anchor cap: at most **20 chunks** (leading chunks) and at most **half the byte budget** (`budget_bytes // 2`), always keeping at least one anchor chunk.
- Exact selection (no `anchor`) behaviour, including 422 `selection_too_large`, is unchanged.
- `local_only` record still 409 `selection_local_only`; stale/out-of-scope still 409 `selection_unavailable`.
- `anchored` is server-derived: stripped from any client context like `selected_only`, and dropped for scheduled-task threads.
- Gating: feed keeps its Telegram exception (`ctx.isTelegram || ctx.isOwner`); every other row button is owner-only (`useWorkspace().isOwner`).
- Label reuses `dashboard` namespace key `askAbout.message`; no new i18n strings.
- Out of scope: `intelligence-panel.tsx`, `news/story-detail.tsx`, bulk "Ask about N", gadget-header ask.
- Repo rules: GitNexus `impact` before editing each symbol, `detect_changes` before each commit; JSDoc/docstring on every new or changed named function; semantic tokens only.

## Review Focus

1. A long article whose projection is truncated → anchor answers from its leading 20 chunks, no 422. (Task 1 test)
2. One anchor chunk larger than half the budget → that chunk is still kept, search fills the rest. (Task 2 test)
3. Search yields 100+ refs → anchor refs are still in the evidence. (Task 2 test)
4. Owner attaches a file after Ask AI → context becomes plain exact selection, no `anchor`, no 422. (Task 3 test)
5. Client sends `anchored: true` or `selected_only` inside a non-selection or selection context → server ignores it. (Task 1 test)

---

### Task 1: Server resolves anchored selections

**Files:**
- Modify: `modules/chat/_public/conversations.py` (`_resolve_selected_chunk_ids`, `resolve_gadget_context`)
- Modify: `modules/chat/_worker/base.py` (`_TASK_THREAD_DROPPED_KEYS`)
- Test: `tests/unit/modules/chat/test_anchor_selection.py` (follow fixtures/mocks of `test_selection_error_codes.py` and `test_gadget_context_keys.py`)

**Interfaces:**
- Produces: `ANCHOR_MAX_CHUNKS = 20` in `conversations.py`; resolved dict for anchor = `{"source_scope": [], "selected_refs": [...], "selection_fences": [...], "selected_only": False, "anchored": True}`.
- `_resolve_selected_chunk_ids(session, item, projection, fence, scope_kw, *, anchor: bool = False) -> list[UUID]`

- [ ] **Step 1: Write failing tests**
  - `test_anchor_single_item_resolves_unfenced`: anchor context with one valid item → `selected_only is False`, `anchored is True`, `source_scope == []`, `selected_refs` non-empty, `selection_fences` has one fence.
  - `test_anchor_rejects_multiple_items`: two items + `anchor: True` → `ValidationFailedError`, detail code `anchor_requires_single_item`.
  - `test_anchor_truncated_document_uses_leading_chunks`: projection `chunks_truncated=True` with 30 chunks → 20 refs, equal to the first 20 chunk ids in order.
  - `test_exact_selection_truncated_still_422`: same projection without anchor → `selection_too_large`.
  - `test_client_anchored_key_stripped`: `{"kind": "document", "anchored": True, "selected_only": True}` → neither key in result; `{"kind": "selection", "items": [...], "anchored": True}` (no `anchor`) → `anchored` absent, `selected_only is True`.
  - `test_anchor_local_only_still_rejected`: anchor result passed to `reject_unsendable_selection` with a `local_only` fence → `ConflictError` code `selection_local_only`.
  - `test_task_thread_drops_anchored`: `_pin_task_thread_scope` on context with `anchored: True` and `scheduled_task` metadata → key removed.

- [ ] **Step 2: Run** `pytest tests/unit/modules/chat/test_anchor_selection.py -q` → FAIL.

- [ ] **Step 3: Implement.** `anchor = context.get("anchor") is True`; check item count right after `_parse_selection_items`. In `_resolve_selected_chunk_ids`, when `anchor` and no `chunk_id`: skip the truncation 422 and take `projection.chunks[:ANCHOR_MAX_CHUNKS]`. Non-selection branch strips `"anchored"` along with `"selected_only"`. Add `"anchored"` to `_TASK_THREAD_DROPPED_KEYS`. Update docstrings.

- [ ] **Step 4: Run** the new file plus `pytest tests/unit/modules/chat/test_selection_error_codes.py tests/unit/modules/chat/test_gadget_context_keys.py tests/unit/modules/chat/test_task_thread_scope.py -q` → PASS.

- [ ] **Step 5: Commit** `feat(chat): resolve anchored single-record selections`

### Task 2: Retrieval keeps the anchor first and capped

**Files:**
- Modify: `modules/chat/schemas.py` (`AnswerContextRequest`)
- Modify: `modules/chat/_worker/setup.py` (the `AnswerContextRequest(...)` build, ~l.200)
- Modify: `modules/chat/retrieval.py` (`build_context` steps 1–7, `_fit_context_budget`)
- Test: `tests/unit/modules/chat/test_anchor_retrieval.py` (follow `test_rest_refactor_characterization.py` for `_fit_context_budget`/`_evidence` helpers and `test_retrieval.py` for `build_context` mocks)

**Interfaces:**
- Consumes: Task 1's `anchored` key in `req_params`.
- Produces: `AnswerContextRequest.anchored: bool = False`.

- [ ] **Step 1: Write failing tests**
  - `test_anchor_refs_collected_before_search`: `build_context` with `anchored=True`, 2 selected refs, search mocked to return 120 refs → refs passed to `read_chat_evidence_chunks` start with the 2 anchor refs; search still called.
  - `test_budget_caps_anchor_at_half`: budget 10 000 bytes, 5 anchor items of 3 000 bytes, 5 search items of 1 000 → result keeps 1 anchor item (3 000 ≤ 5 000, second would make 6 000) then search items until 10 000; anchor items first.
  - `test_budget_keeps_oversized_first_anchor`: single anchor item of 8 000 bytes, budget 10 000 → kept, followed by search items that fit the remaining 2 000.
  - `test_rerank_only_reorders_search_items`: reranker mock reverses its input → anchor items stay first, reranker receives only non-anchor items.
  - `test_exact_selection_budget_unchanged`: `selected_only=True` overflow still raises `ValueError`.
  - `test_setup_passes_anchored`: `req_params={"anchored": True, ...}` → built request has `anchored is True`.

- [ ] **Step 2: Run** `pytest tests/unit/modules/chat/test_anchor_retrieval.py -q` → FAIL.

- [ ] **Step 3: Implement.** Anchor ref set = `{(r.document_version_id, r.chunk_id) for r in request.selected_refs}`. In `build_context`, when `request.anchored`, add selected refs to the collector before `_collect_search_refs`. In `_fit_context_budget`, when `anchored`: fill anchor items up to `budget_bytes // 2` (at least one), then non-anchor items up to the full budget; output anchor items first. Rerank step: when `anchored`, call `_apply_configured_reranking` with non-anchor items only and prepend the anchor items. Docstrings updated.

- [ ] **Step 4: Run** the new file plus `pytest tests/unit/modules/chat/test_retrieval.py tests/unit/modules/chat/test_rest_refactor_characterization.py tests/unit/modules/chat/test_worker_decomposition.py -q` → PASS; `ruff check modules/chat` and `mypy modules/chat` clean.

- [ ] **Step 5: Commit** `feat(chat): keep anchored record evidence first within a capped budget`

### Task 3: Shared AskRecordButton; feed and video use it

**Files:**
- Modify: `apps/web/src/modules/chat/api.ts` (`ChatContext`: add `anchor?: boolean` with JSDoc "one-item selection used as an anchor; server derives retrieval mode")
- Create: `apps/web/src/modules/dashboard/gadgets/ask-record-button.tsx`
- Modify: `apps/web/src/modules/dashboard/gadgets/feed-gadget.tsx`, `feed-parts.tsx` (`FeedItemActions`), `video-panel.tsx`
- Test: `apps/web/src/modules/dashboard/gadgets/ask-record-button.test.tsx`; extend `feed-gadget.test.tsx`, `video-panel.test.tsx`; add a case to the chat-attachments test file

**Interfaces:**
- Produces: `export interface RecordIdentity { sourceId: string; documentId: string; documentVersionId: string }` and `export function AskRecordButton({ record, allowNonOwner = false, className }: { record: RecordIdentity; allowNonOwner?: boolean; className?: string }): JSX.Element | null`. Renders a ghost `Button` (`size="sm"`, `h-6 px-2 text-xs`, as in `FeedItemActions`) labelled `t('askAbout.message')`; returns null unless `allowNonOwner || useWorkspace().isOwner`; click → `openDrawer({ context: { kind: 'selection', anchor: true, items: [record] } })`.

- [ ] **Step 1: Write failing tests**
  - `ask-record-button.test.tsx`: owner click calls `openDrawer` with exactly `{ context: { kind: 'selection', anchor: true, items: [record] } }`; non-owner renders nothing; non-owner with `allowNonOwner` renders.
  - feed test: row Ask AI sends `anchor: true` and one item; Telegram row visible for non-owner; bulk "Ask about N" sends no `anchor`.
  - video test: row Ask AI sends `anchor: true`.
  - attachments test: `withAttachments({ kind: 'selection', anchor: true, items: [a] }, [b])` → result has no `anchor` key and two items.

- [ ] **Step 2: Run** `npx vitest run src/modules/dashboard/gadgets/ask-record-button.test.tsx src/modules/dashboard/gadgets/feed-gadget.test.tsx src/modules/dashboard/gadgets/video-panel.test.tsx` (from `apps/web`) → FAIL.

- [ ] **Step 3: Implement.** Feed: replace the inline Ask button with `<AskRecordButton record={...} allowNonOwner={ctx.isTelegram} />`, keep the outer `(ctx.isTelegram || ctx.isOwner)` semantics; single-row path no longer goes through `ctx.onAskAbout` (bulk keeps it). Video: `ask` prop builds the same anchor context (or renders `AskRecordButton` where the row renders its button).

- [ ] **Step 4: Run** the tests → PASS; `npx tsc --noEmit` and `npx eslint src/modules/dashboard src/modules/chat` clean.

- [ ] **Step 5: Commit** `feat(web): anchored Ask AI button for feed and video rows`

### Task 4: Ask AI on media, table, job-list, text-panel and watchlist rows

**Files:**
- Modify: `apps/web/src/modules/dashboard/gadgets/media-parts.tsx` (per-item actions near the translate button, ~l.492)
- Modify: `table-panel.tsx` (row actions, ~l.200)
- Modify: `job-posting.ts` (`JobPosting`: add `sourceId?: string`, read from projection `source_id`), `job-list.tsx` (~l.88)
- Modify: `text-panel.tsx` (~l.69–75)
- Modify: `watchlist-gadget.tsx` (`WatchlistItem`: add optional `sourceId`, `documentId`, `documentVersionId`, mapped from the highlight match `source_id`/`document_id`/`document_version_id`; render button on rows that have all three)
- Test: extend `media-feed.test.tsx`, `table-panel.test.tsx`, `job-list.test.tsx`, `job-posting.test.ts`, `watchlist-gadget.test.tsx`; add `text-panel` case to an existing dashboard panels test or a new `text-panel.test.tsx`

**Interfaces:**
- Consumes: `AskRecordButton`, `RecordIdentity` from Task 3.

- [ ] **Step 1: Write failing tests** — per gadget, as owner: one row with full identity renders the Ask AI button and clicking it calls `openDrawer` with `anchor: true` and that row's three ids; as non-owner: no button. Job-posting: `parseJobPosting` maps `source_id` → `sourceId`. Watchlist: a match row without ids renders no button.

- [ ] **Step 2: Run** the touched test files with `npx vitest run <files>` → FAIL.

- [ ] **Step 3: Implement** — place `AskRecordButton` inside each gadget's existing per-row action area (same hover/visibility pattern as that row's translate button); render only when the row has all three ids.

- [ ] **Step 4: Run** the tests → PASS; full `npx vitest run src/modules/dashboard`, `npx tsc --noEmit`, `npx eslint src/modules/dashboard` clean.

- [ ] **Step 5: Commit** `feat(web): anchored Ask AI on media, table, job, text and watchlist rows`

### Task 5: Status note and final gates

**Files:**
- Modify: `docs/IMPLEMENTATION_STATUS.md` (one line under the dashboard/chat area: per-record anchored Ask AI, spec link)

- [ ] **Step 1:** `pytest tests/unit/modules/chat -q`, `ruff check .`, `mypy modules/chat`; in `apps/web`: `npx vitest run`, `npx tsc --noEmit`, `npx eslint .` → all pass (report any pre-existing failure separately).
- [ ] **Step 2:** `node .gitnexus/run.cjs detect-changes --scope compare --base-ref main --repo .` → only expected symbols; not partial/truncated.
- [ ] **Step 3: Commit** `docs: note per-record anchored Ask AI status`
