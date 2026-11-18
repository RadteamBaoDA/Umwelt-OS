# Scheduled Tasks — Phase 1 (framework + Daily brief) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a Settings › Scheduled tasks page whose Daily brief task generates the existing daily brief on a cron schedule. Each brief is posted into the task's own chat thread, the thread shows under "tasks" in Chat, and the thread's retrieval is pinned server-side to the brief's sources.

**Architecture:** A scheduled task is an `Automation` rule with a `schedule` trigger and a single `generate_brief` action. "Task kind" is derived from the definition, so no new table or migration is needed. `modules/dashboard` keeps brief generation and gains sections, an extra instruction, a pinned `as_of` time and four new fact widgets. `modules/chat` gains public commands that create the task conversation, append the run's message idempotently and scrub copies. `modules/automations` wires the two together.

**Tech Stack:** Python 3 / FastAPI / SQLAlchemy async / ARQ worker, pytest; Next.js App Router / React / TypeScript / shadcn/ui / next-intl, Vitest.

**Spec:** `docs/superpowers/specs/2026-11-18-scheduled-tasks-daily-brief-design.md` (mockups: artifact `QBvVoZdi7tsc2THAD1ndXf`, views 0, 0b, 2, 5, 7, 8)

## Global Constraints

- "Scheduled task" is UI wording only. Do not create `modules/scheduled_tasks`, and do not add backend names starting `task_` for this feature (`modules/tasks` already means to-dos). Exception: the chat metadata keys defined below (`scheduled_task`, `task_source_scope`).
- Use one daily brief per workspace. The existing `dashboard.claim_brief_slot` / `BriefSlotOwned` enforces it; reuse it.
- Telegram delivery is out of scope. Do not render the checkbox.
- Per-task model choice is a configured model role (brief/chat/background), added as Task 6b by owner decision 2026-11-18.
- No migration and no new dependency.
- Every new or changed production function, method, component or hook gets a JSDoc block or Python docstring stating its contract (AGENTS.md "Mandatory Code Documentation").
- Run GitNexus impact (`impact({target, direction:"upstream"})`) before editing any existing symbol, and report HIGH/CRITICAL. Run `detect_changes` before every commit. The worktree has no `.gitnexus/run.cjs`, so use the MCP tools or run the CLI from `d:/Project/Umwelt-OS`.
- Locales: every UI string goes in both `apps/web/locales/en-us/*.json` and `vi-vi/*.json`; the parity tests must pass.
- Use semantic tokens and `src/components/ui` primitives only, no literal colors (DESIGN_SYSTEM.md).
- Run Python tests with `uv run pytest <path> -q`, web tests with `npm --prefix apps/web run test -- <path>`, the typecheck with `make typecheck` and lint with `make lint`.

## Review Focus

1. **Market price ticks while the brief is generating.** Generation re-checks facts in three phases, and a 5-minute price feed would change the fingerprint and silently drop the brief. Expected: the brief publishes. Pinned by `test_observation_widgets_are_pinned_to_as_of` (Task 2) and `test_generate_brief_passes_one_as_of_to_all_phases` (Task 3).
2. **Retry after the message was posted but before the action was marked succeeded.** Expected: exactly one chat message per brief revision. Pinned by `test_retry_posts_brief_once` (Task 5).
3. **Inputs unchanged, so the brief is reused.** This happens with "Run now" twice, or a second slot on the same day. Expected: no new message, and the run's result is `brief:<same id>`. Pinned by `test_reused_brief_posts_nothing_new` (Task 5).
4. **Client sends a forged `source_scope` in a task thread.** Expected: it is ignored and the server-derived scope applies, enforced. Pinned by `test_task_thread_scope_ignores_client_scope` (Task 4).
5. **A source cited by a posted brief is purged, and brief cleanup scrubs the brief.** Expected: the chat copy of that brief is scrubbed in the same transaction, so the deleted content does not survive in chat. Pinned by `test_brief_cleanup_scrubs_chat_copies` (Task 3), which uses `scrub_task_messages` (Task 1).

---

### Task 1: Chat public commands for task conversations

**Files:**
- Create: `modules/chat/_public/task_threads.py`
- Modify: `modules/chat/public.py` (export the three functions and add them to `__all__`)
- Test: `tests/unit/modules/chat/test_task_threads.py`

**Interfaces:**
- Produces (all flush-only; they never commit, and the caller owns the transaction):
  - `async def ensure_automation_conversation(session: AsyncSession, automation_id: UUID, title: str, *, task_kind: Literal["daily_brief"] | None, scope: Scope) -> UUID`
    - Returns the existing non-archived conversation with `context_kind="automation"`, `context_resource_id=automation_id`, or creates it with the same fields that `automations._execution.actions._automation_conversation` uses today.
    - When `task_kind` is set, ensures `metadata_json["scheduled_task"] == {"kind": task_kind}` and `title == title[:255]`.
  - `async def append_task_message(session: AsyncSession, conversation_id: UUID, *, content: str, citations: list[dict[str, Any]], metadata: dict[str, Any], source_ids: Sequence[UUID], dedupe_key: str, scope: Scope) -> tuple[UUID, bool]`
    - Inserts a `role="assistant"` message with `client_request_id = f"task:{dedupe_key}"` (≤128 chars) and `metadata_json = {"origin": "scheduled_task", **metadata}`.
    - If a message with that `client_request_id` already exists in the conversation, returns `(existing_id, False)` and changes nothing. Otherwise returns `(new_id, True)`.
    - On insert, merges `source_ids` into `conversation.metadata_json["task_source_scope"]`: string UUIDs, newest first, de-duplicated, capped at `MAX_SOURCE_SCOPE` (100) by dropping the oldest. It also bumps `conversation.updated_at`.
    - Takes `SELECT … FOR UPDATE` on the conversation row before the existence check, so concurrent retries serialize.
  - `async def scrub_task_messages(session: AsyncSession, brief_ids: Sequence[UUID], *, scope: Scope) -> int`
    - For messages whose `metadata_json["brief_id"]` is in `brief_ids`: sets `content` to the scrubbed placeholder that chat already uses for erased content (reuse the existing constant; do not invent text), sets `citations=[]`, removes `brief_citations` from the metadata, and returns the count.

- [ ] **Step 1: Write failing tests** in `tests/unit/modules/chat/test_task_threads.py`, using the existing chat unit-test session fixtures/fakes from `tests/unit/modules/chat`:
  - `test_ensure_conversation_creates_once_and_marks_task`: two calls return the same id, and `metadata_json["scheduled_task"] == {"kind": "daily_brief"}`.
  - `test_ensure_conversation_without_kind_keeps_metadata_empty`: `task_kind=None` gives `metadata_json == {}`, which is today's `run_agent` behaviour.
  - `test_append_is_idempotent_on_dedupe_key`: the second call with `dedupe_key="brief:X"` returns `(same_id, False)`, and the message count is 1.
  - `test_append_merges_source_scope_newest_first_capped`: 101 distinct ids → `len == 100`, the most recent call's ids come first, and no duplicates.
  - `test_scrub_task_messages_clears_content_and_citations`: returns 1, content equals the placeholder, and `citations == []`.
- [ ] **Step 2: Run** `uv run pytest tests/unit/modules/chat/test_task_threads.py -q`. Expected: FAIL (import error).
- [ ] **Step 3: Implement** the three functions in `modules/chat/_public/task_threads.py` and export them from `modules/chat/public.py`.
- [ ] **Step 4: Run** the same command. Expected: PASS.
- [ ] **Step 5: Run** `detect_changes`, then commit: `feat(chat): public commands for scheduled-task threads`.

### Task 2: Dashboard fact widgets: markets, weather, hazards, source health

**Files:**
- Modify: `modules/dashboard/daily_schemas.py`
  - add `BriefSection = Literal["stories", "markets", "weather", "hazards", "agenda", "source_health"]`
  - add `ALL_BRIEF_SECTIONS: tuple[BriefSection, ...]` in that order
- Modify: `modules/dashboard/context.py`
  - `build_daily_widgets` gains `as_of: datetime | None = None` and `sections: Sequence[BriefSection] | None = None`
  - add the four new widget builders
- Modify: `modules/dashboard/briefs.py:_fact_candidates`: the detail chain becomes `item.get("detail") or item.get("status") or item.get("progress") or item.get("type") or ""`
- Modify: `modules/dashboard/public.py` (export `BriefSection` and `ALL_BRIEF_SECTIONS`)
- Test: `tests/unit/modules/dashboard/test_brief_fact_widgets.py`

**Interfaces:**
- Consumes:
  - `knowledge.observations.public.list_observations(session, ObservationQuery, None, scope=, multi_workspace_enabled=)`; `ObservationQuery` allows at most 32 `source_ids` and `limit ≤ 100`.
  - `sources.public.list_sources(session, limit, cursor, scope=, multi_workspace_enabled=)`.
  - The connector catalog accessor exported by `modules/connectors/public.py`. If `get_catalog_entry` is not exported there, export it; do not import `connectors.catalog` directly.
- Produces:
  - `build_daily_widgets(session, day, timezone, *, scope, multi_workspace_enabled, relation=None, as_of=None, sections=None) -> list[DailyWidget]`
  - `sections=None` means all of `ALL_BRIEF_SECTIONS`. The widgets come out in section order: `agenda` = the existing `tasks`, `goals` and `events` widgets, `stories` = the existing stories widget.
  - `as_of=None` means `datetime.now(UTC)` truncated to the minute.
  - New widget ids: `markets`, `weather`, `hazards`, `source_health`. Each item is `{"id": str, "title": str, "detail": str, "source_id": str}`. The id is stable: `f"{source_id}:{metric}:{symbol or ''}"` for observations and the source id for health. Each widget has at most `MAX_ITEMS` items.
  - A widget with no items gets `status="empty"`, so `_facts` produces no facts and the section is omitted from the prompt.

**Rules (values the tests pin):**
- Source classification by catalog `category` / provider id:
  - `economy` → markets
  - provider in `WEATHER_PROVIDERS = frozenset({"open_meteo"})` → weather (extend the set only with providers you confirm in the catalog)
  - `hazards` → hazards
- Only sources with `status == "active"` and `not local_only` are read.
- Observation window:
  - markets and hazards: `from_at = as_of - 24h`, `to_at = as_of`
  - weather: `from_at = as_of`, `to_at = as_of + 24h` (forecast points), with a fallback to the last 3 h when the window is empty
- Markets: per `(source_id, metric, symbol)`, the latest value plus the change against the earliest value in the window. The detail is `"{value:,} {unit} ({pct:+.1f}% 24h)"`, with the percentage omitted when there is only one point.
- Weather: per source, min/max of the `temperature*` metrics and the sum of `precipitation*` over the window. The detail is `"{min}–{max}{unit}, rain {sum} mm"`.
- Hazards: the top 5 by `value` descending, ties broken by the latest `observed_at`. The detail is `"{metric} {value} {region or ''}"`.
- Source health: active sources with `last_error_at` set and (`last_success_at` is None or `last_error_at > last_success_at`). The detail is `last_error_code or collection_error_code or "error"`. At most 5.

- [ ] **Step 1: Write failing tests** with fake `list_observations` / `list_sources` / catalog (monkeypatch, following the patterns in `tests/unit/modules/dashboard/test_briefs_three_phase.py`):
  - `test_sections_filter_and_order`: with `sections=["markets", "stories"]`, the widget ids are `["markets", "stories"]`.
  - `test_default_sections_include_agenda_widgets`: with `sections=None`, the ids contain `tasks`, `goals`, `events`, `stories`, `markets`, `weather`, `hazards` and `source_health`.
  - `test_markets_latest_and_change`: points 100 → 103 give the detail `"103 USD (+3.0% 24h)"`.
  - `test_observation_widgets_are_pinned_to_as_of`: a point with `observed_at > as_of` is excluded, so two calls with the same `as_of` return equal widgets even after a newer point is added.
  - `test_source_health_lists_only_failing_active`: an error newer than the last success is listed, a success newer than the last error is not, and a paused source is not.
  - `test_fact_candidates_prefers_detail`: `_fact_candidates` on a `markets` item uses `detail`.
- [ ] **Step 2: Run** `uv run pytest tests/unit/modules/dashboard/test_brief_fact_widgets.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** the schema literals, the `build_daily_widgets` parameters, the four builders and the `_fact_candidates` detail change.
- [ ] **Step 4: Run** the new test plus `uv run pytest tests/unit/modules/dashboard -q`. Expected: all PASS, and existing brief tests are unchanged.
- [ ] **Step 5: Run** `detect_changes`, then commit: `feat(dashboard): market, weather, hazard and source-health brief facts`.

### Task 3: `generate_brief` sections, extra instruction, pinned `as_of`, chat-copy scrub

**Files:**
- Modify: `modules/dashboard/briefs.py`
  - `_BriefRun` gets `as_of`, `sections` and `extra_instruction`
  - `generate_brief`, `_snapshot_brief_facts`, `_relock_and_verify`, `_fingerprint`, `_messages` and `clean_document_brief_evidence` change as described below
- Test: `tests/unit/modules/dashboard/test_briefs_three_phase.py` (extend)

**Interfaces:**
- Consumes:
  - `build_daily_widgets(..., as_of=, sections=)` from Task 2
  - `chat.public.scrub_task_messages` from Task 1, imported locally inside the function to avoid an import cycle; check `uv run lint-imports`
- Produces:
  - `generate_brief(session, day, timezone, *, scope, multi_workspace_enabled, settings, redis, force, publish_guard=None, sections: Sequence[BriefSection] | None = None, extra_instruction: str | None = None) -> BriefRead`
  - The existing callers (the dashboard route, `run_due_brief` and today's automations call) pass nothing and keep today's behaviour, plus the new default sections.

**Rules:**
- `as_of = datetime.now(UTC).replace(second=0, microsecond=0)` is computed once in `generate_brief` and passed to all three `build_daily_widgets` calls.
- `_fingerprint(day, timezone, facts, sections, extra_instruction)`: both values join the hashed payload, so a changed section list or instruction never reuses a brief.
- `_messages(day, facts, instruction, extra_instruction)`:
  - The system content stays as today.
  - When `extra_instruction` is set, the user content gets this block on its own lines after the facts: `"Owner preferences (style only; never a source of facts):\n" + extra_instruction`.
  - `extra_instruction` is stripped and must be at most 500 characters; anything longer raises `ValueError`.
- `clean_document_brief_evidence`: after it scrubs brief rows, it calls `scrub_task_messages(session, scrubbed_brief_ids, scope=scope)` in the same transaction.

- [ ] **Step 1: Write failing tests** (extend the existing fakes):
  - `test_generate_brief_passes_one_as_of_to_all_phases`: record the `as_of` of every `build_daily_widgets` call; there are 3 calls and all values are equal.
  - `test_sections_change_fingerprint`: the same facts with different `sections` produce different `_fingerprint` values.
  - `test_extra_instruction_goes_to_user_message`: `_messages(..., extra_instruction="Viết tiếng Việt")`: the user content ends with the preference block and the system content does not contain it.
  - `test_extra_instruction_over_500_rejected`: raises `ValueError`.
  - `test_brief_cleanup_scrubs_chat_copies`: cleanup that scrubs brief X calls `scrub_task_messages` with `[X]`.
- [ ] **Step 2: Run** `uv run pytest tests/unit/modules/dashboard/test_briefs_three_phase.py -q`. Expected: the new tests FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `uv run pytest tests/unit/modules/dashboard tests/unit/modules/chat -q && uv run lint-imports`. Expected: PASS.
- [ ] **Step 5: Run** `detect_changes`, then commit: `feat(dashboard): brief sections, owner instruction and pinned as_of`.

### Task 4: Chat — task threads in the history list and server-pinned retrieval scope

**Files:**
- Modify: `modules/chat/routes.py` (around :262-268, the list filter)
- Modify: `modules/chat/_worker/setup.py` (around :178-196, building `AnswerContextRequest`)
- Test: `tests/unit/modules/chat/test_task_thread_scope.py`

**Interfaces:**
- Consumes: the conversation metadata keys `scheduled_task` and `task_source_scope` from Task 1.
- Produces:
  - The conversation list includes `context_kind == "automation"` conversations only when `metadata_json` has the key `scheduled_task` (use the JSONB `has_key`). Other automation threads stay hidden, as today. Export (`_public/export.py:375`) is unchanged.
  - In a turn on a conversation whose metadata has `scheduled_task`:
    - `answer_request.source_scope = [UUID(x) for x in metadata.get("task_source_scope", [])]`
    - `source_scope_enforced = True`
    - the client `req_params["source_scope"]` is ignored
    - an empty list means "no sources", which matches the existing semantics
  - `query_collected_data` in such a turn honours the same scope.

- [ ] **Step 1: Write failing tests:**
  - `test_list_includes_task_threads_only`: three conversations (plain, automation, automation with `scheduled_task`); the list returns the plain one and the task one.
  - `test_task_thread_scope_ignores_client_scope`: the client sends `source_scope=[A]` and the metadata scope is `[B]`, so the request has `source_scope == [B]` and `source_scope_enforced is True`.
  - `test_task_thread_without_briefs_has_empty_enforced_scope`.
  - `test_query_collected_data_respects_task_scope`: a query naming a source outside the scope is refused or returns no rows. If this already passes, keep the test as the regression pin.
- [ ] **Step 2: Run** `uv run pytest tests/unit/modules/chat/test_task_thread_scope.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** the list filter and the scope override. Read `metadata_json` once; `setup.py` already selects it for `_day_scope`.
- [ ] **Step 4: Run** `uv run pytest tests/unit/modules/chat -q`. Expected: PASS.
- [ ] **Step 5: Run** `detect_changes`, then commit: `feat(chat): show scheduled-task threads and pin their retrieval scope`.

### Task 5: Automations — Daily brief task posts to its thread

**Files:**
- Modify: `modules/automations/schemas.py:169-174` (`GenerateBriefAction`), the `AutomationRead` model (add `next_run_at`), `RunActionRead` (no change)
- Modify: `modules/automations/_execution/actions.py`
  - `_generate_brief` passes `sections` and `extra_instruction`, then posts the brief
  - `_automation_conversation` delegates to `chat.ensure_automation_conversation`
- Modify: `modules/automations/public.py`
  - include `next_run_at` in the reads
  - on delete, delete the rule's chat conversation through the existing chat conversation-delete public path (find it; do not hard-delete rows directly)
- Test: `tests/unit/modules/automations/test_action_step.py` (extend), `tests/unit/modules/automations/test_schemas.py` (extend)

**Interfaces:**
- Consumes: Task 1 `ensure_automation_conversation` and `append_task_message`; Task 3 `generate_brief(..., sections=, extra_instruction=)`; `dashboard.public.BriefSection`.
- Produces:
  - `GenerateBriefAction`:
    - `scope: Literal["daily"] = "daily"`
    - `sections: list[BriefSection] = Field(default_factory=lambda: list(ALL_BRIEF_SECTIONS), min_length=1, max_length=6)`, with duplicates rejected
    - `extra_instruction: str | None = Field(default=None, max_length=500)`
    - `deliver_chat: bool = False`
    - The default stays `False` so existing rules do not change behaviour. The web editor always sends `True`.
  - `AutomationRead.next_run_at: datetime | None`, taken from `AutomationSchedule.next_slot` (None for non-schedule triggers or disabled rules).
  - `def scheduled_task_kind(trigger: dict, actions: list[dict]) -> Literal["daily_brief"] | None`, in `modules/automations/schemas.py`. It returns `"daily_brief"` iff `trigger["type"] == "schedule"` and `actions == [one generate_brief action with deliver_chat True]`. It is the only classifier; the web mirrors it.
- Behaviour after a successful `dashboard.generate_brief` when `deliver_chat` is true, in a new short transaction:
  1. `conv = ensure_automation_conversation(session, automation_id, rule_name, task_kind=scheduled_task_kind(...), scope=)`.
  2. `append_task_message(session, conv, content=brief.content, citations=<DocumentCitation dicts for cited facts that have document support, in ref order; may be empty>, metadata={"task_kind": "daily_brief", "run_id": str(run.id), "brief_id": str(brief.id), "brief_revision": brief.revision, "brief_citations": brief.citations}, source_ids=<union of source_ids in brief.citations>, dedupe_key=f"brief:{brief.id}", scope=)`.
  3. Commit, then mark the action `succeeded` with `result_reference=f"brief:{brief.id}"`.

  If step 1 or 2 raises, use the existing `_transient` retry path; the dedupe key makes the retry safe. `BriefEmpty` posts nothing, as today. A reused brief (same id) gets `False` from `append_task_message` and posts nothing.
- Read the action spec (`sections`, `extra_instruction`, `deliver_chat`) the same way `_start_agent` receives `spec` (from the run's revision `actions[ordinal]`).

- [ ] **Step 1: Write failing tests:**
  - `test_generate_brief_action_defaults_keep_old_behaviour`: `deliver_chat is False` and the sections equal `ALL_BRIEF_SECTIONS`.
  - `test_generate_brief_action_rejects_duplicate_sections`.
  - `test_scheduled_task_kind`: the cases are schedule+brief+deliver → `"daily_brief"`, webhook trigger → None, two actions → None, and `deliver_chat` False → None.
  - `test_brief_task_posts_to_thread`: a fake `generate_brief` returns brief B, so `append_task_message` is called once with `dedupe_key="brief:B"` and the action `result_reference == "brief:B"`.
  - `test_retry_posts_brief_once`: the first attempt raises after `append_task_message` committed, and the second attempt yields one message in total.
  - `test_reused_brief_posts_nothing_new`: two runs return the same brief id, giving one message.
  - `test_brief_empty_posts_nothing`.
  - `test_second_brief_task_refused`: creating a second `generate_brief` rule while the slot is owned raises the existing `BriefSlotOwned`-derived conflict (assert the HTTP code and error code the route maps today).
  - `test_next_run_at_in_read`.
  - `test_delete_brief_task_deletes_thread_releases_slot`.
- [ ] **Step 2: Run** `uv run pytest tests/unit/modules/automations -q`. Expected: the new tests FAIL.
- [ ] **Step 3: Implement.** Before editing, run impact on `_automation_conversation`, `_generate_brief` and `GenerateBriefAction`, and report the risk.
- [ ] **Step 4: Run** `uv run pytest tests/unit/modules/automations tests/unit/modules/chat tests/unit/modules/dashboard -q`. Expected: PASS. Then regenerate the API types with the repo's existing generator for `api-types.gen.ts` (see `apps/web/package.json` scripts) and commit the result.
- [ ] **Step 5: Run** `detect_changes`, then commit: `feat(automations): daily brief scheduled task posts into its thread`.

### Task 6: Web — Scheduled tasks page (Yours / Templates / new-task menu / Daily brief editor)

**Files:**
- Create: `apps/web/src/app/(workspace)/settings/scheduled-tasks/page.tsx`, which renders `<ScheduledTasksWorkspace />`
- Create in `apps/web/src/modules/automations/`:
  - `scheduled-tasks.tsx`: workspace component with the Yours/Templates tabs, toolbar and list
  - `scheduled-task-model.ts`: the pure helpers below
  - `brief-task-editor.tsx`: the dialog
  - `scheduled-tasks.test.tsx` and `scheduled-task-model.test.ts`
- Modify:
  - `apps/web/src/modules/automations/index.ts` (export)
  - `apps/web/src/core/module-registry.ts` (`settingsExtraLinks` += `{ id: 'scheduled-tasks', href: '/settings/scheduled-tasks', messageKey: 'scheduledTasks' }`)
  - `apps/web/src/core/app-shell/settings-nav.tsx` (`dataAiIds`: insert `'scheduled-tasks'` after `'data-sources'`)
  - `apps/web/locales/{en-us,vi-vi}/shell.json` (`scheduledTasks`) and `automations.json` (new `scheduledTasks.*` keys)

**Interfaces:**
- Consumes: `GET/POST/PATCH/DELETE /api/v1/automations`, `POST /{id}/run`, `GET /{id}/runs?limit=1` and `GET /{id}/conversation` through the existing `modules/automations/api.ts` client (add functions there if missing). Also `AutomationRead.next_run_at` from Task 5.
- Produces, in `scheduled-task-model.ts`:
  - `scheduledTaskKind(rule: AutomationRead): 'daily_brief' | null`, which mirrors the Python classifier exactly
  - `briefCron(time: string /* "HH:MM" */, days: 'daily' | 'weekdays'): string`, giving `"M H * * *"` or `"M H * * 1-5"`
  - `parseBriefCron(cron: string): { time: string; days: 'daily' | 'weekdays' } | null`
  - `describeSchedule(cron: string, locale: 'en-US' | 'vi-VN'): string`: a plain sentence for the two generated patterns, otherwise the raw cron
  - `TEMPLATES: readonly ScheduledTaskTemplate[]`. Phase 1 has exactly one entry, `{ id: 'daily-brief', kind: 'daily_brief', icon: 'sun', defaultTime: '07:00', defaultDays: 'daily' }`; the other mockup templates arrive with their kinds in later phases.

**UI behaviour (mockup views 0, 0b, 2, 7):**
- **Yours tab:**
  - Empty state: clock icon, "No scheduled tasks yet", a "New task" button, and the templates grid below a dashed rule.
  - Non-empty: a table with Task (name, the first line of the instruction or the section summary), State (active / paused), Schedule (cron in mono plus `describeSchedule`), Last run (time plus status from runs?limit=1), Next run (`next_run_at`, in the workspace locale), and Actions.
  - Actions: run now (`client_request_id = crypto.randomUUID()` with `expected_revision`), pause/resume (PATCH `enabled`), edit, history (the existing `run-detail`), and open thread (GET conversation, then route to `/chat/{id}`; disabled while null).
- **Templates tab:** the grid of `TEMPLATES`. The Daily brief card is disabled with a "convert existing brief rule" link when a non-task `generate_brief` rule exists, and with "already set up" when a task exists.
- **"New task ▾" menu:** "From a template" switches to Templates; "Blank task" opens the existing `rule-editor`. "Describe in chat" is hidden until Phase 3.
- **Brief editor dialog:**
  - Section checklist in `ALL_BRIEF_SECTIONS` order with localized labels.
  - Extra instruction textarea with a 500-character limit and a counter.
  - Time input and days select (every day / weekdays).
  - Read-only model line.
  - Deliver-to checkboxes: chat thread, which is forced on and disabled in Phase 1, and dashboard gadget, which is informational.
  - Timezone: the workspace brief timezone, read from the existing brief schedule API, falling back to `Intl.DateTimeFormat().resolvedOptions().timeZone`.
  - Save sends POST `{ name: 'Daily brief' (localized), enabled: true, trigger: { type: 'schedule', cron, timezone }, conditions: [], actions: [{ type: 'generate_brief', sections, extra_instruction, deliver_chat: true }] }`, or PATCH with `expected_revision` when editing.
  - A slot-owned conflict shows an inline error with the convert action. Convert sends a PATCH to the existing rule with `actions: [{ ...its generate_brief action, deliver_chat: true, sections, extra_instruction }]` and the schedule trigger.

- [ ] **Step 1: Write failing tests:**
  - `scheduled-task-model.test.ts`:
    - `briefCron('07:05','daily') === '5 7 * * *'`
    - `briefCron('18:00','weekdays') === '0 18 * * 1-5'`
    - `parseBriefCron` round-trips both and returns null for `'*/5 * * * *'`
    - `scheduledTaskKind` covers the same 4 cases as the Python test
  - `scheduled-tasks.test.tsx` (mocked api):
    - the empty state shows the template grid
    - a list row shows `describeSchedule` text and the next run
    - the Daily brief template is disabled when a task exists
    - saving the editor POSTs the exact payload above
    - a 409 slot conflict shows the convert action
    - both locales render (`en-us`, `vi-vi`)
- [ ] **Step 2: Run** `npm --prefix apps/web run test -- src/modules/automations`. Expected: FAIL.
- [ ] **Step 3: Implement** the components, routing, nav registration and locale keys, following the `umwelt-os-ui-system` skill.
- [ ] **Step 4: Run** `npm --prefix apps/web run test -- src/modules/automations locales` and `make typecheck`. Expected: PASS.
- [ ] **Step 5: Run** `detect_changes`, then commit: `feat(web): scheduled tasks settings page with daily brief editor`.

### Task 6b: Per-task model choice (owner decision 2026-11-18, added after Task 6 dispatch)

**Owner decision:** each scheduled task can choose which configured model runs it. This replaces the spec non-goal "no per-task model in Phase 1".

**Design call (controller):** the task chooses a configured **model role**, not a free-form model string. The options are `brief` (default), `chat` and `background`. Each role already has a provider/model mapping, privacy destinations and egress approval in Settings › Models, so no new validation surface is needed. `background` falls back to brief, then chat (`ModelDefaults.resolve`).

**Files:**
- Modify: `modules/dashboard/briefs.py` (`generate_brief` gains `model_role`; `_resolve_brief_egress` uses `settings_public.derive_for_role(config, model_role)` and `config.defaults.resolve(model_role)`; `_fingerprint` includes `model_role` only when it is not `"brief"`, so existing hashes stay stable)
- Modify: `modules/automations/schemas.py` (`GenerateBriefAction.model_role: Literal["brief", "chat", "background"] = "brief"`), `modules/automations/_execution/actions.py` (pass `spec.get("model_role", "brief")`)
- Modify web: `apps/web/src/modules/automations/brief-task-editor.tsx` (a model Select listing the three roles, each labelled with the resolved model name from the existing Models settings API; an unconfigured role is shown disabled), plus locales
- Regenerate `api-types.gen.ts`
- Tests: extend `tests/unit/modules/dashboard/test_briefs_three_phase.py`, `tests/unit/modules/automations/test_schemas.py` and `test_action_step.py`, and the editor test

**Interfaces:**
- `generate_brief(..., model_role: Literal["brief","chat","background"] = "brief")`
- `BriefRead.model_alias` stays the role name actually used (today it stores `egress.role`)

- [ ] **Step 1: Write failing tests.**
  - `test_model_role_selects_role_mapping`: `model_role="chat"` makes `derive_for_role` receive `"chat"` and the gateway get the chat mapping.
  - `test_default_model_role_keeps_fingerprint`: `model_role="brief"` gives the same fingerprint as before this task.
  - `test_model_role_changes_fingerprint`.
  - `test_generate_brief_action_rejects_unknown_model_role`.
  - Action passes `model_role` through.
  - Editor: the select shows the 3 roles, the save payload includes `model_role`, and an unconfigured role is disabled.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.** The privacy re-check in `_BriefEgress.verify_settings` must use the same role.
- [ ] **Step 4: Run** `uv run pytest tests/unit/modules/dashboard tests/unit/modules/automations -q` and `npm --prefix apps/web run test -- src/modules/automations`, plus typecheck. Expected: PASS.
- [ ] **Step 5: Run `detect_changes`, then commit** `feat: per-task model role for scheduled daily brief`.

### Task 7: Web — task threads in Chat and brief messages

**Files:**
- Modify:
  - `apps/web/src/modules/chat/chat-history.tsx`: a "tasks" group first, holding conversations with `context_kind === 'automation' && metadata.scheduled_task`
  - the chat message renderer (find the component that renders assistant message content and citations under `apps/web/src/modules/chat/`)
  - the conversation header
  - `apps/web/src/modules/dashboard/daily-brief.tsx`: an "open full brief" link
  - locales `chat.json` and `dashboard.json`
- Test: `chat-history.test.tsx` (extend), plus a new `task-brief-message.test.tsx` next to the renderer

**Interfaces:**
- Consumes: the message `metadata.task_kind === 'daily_brief'` with `brief_citations` (Task 5), and the conversation `metadata.scheduled_task.kind`.
- Produces:
  - An assistant message with `metadata.task_kind === 'daily_brief'` renders through the existing `brief-text.tsx`, passing `content` and `brief_citations` as the brief citations prop.
  - Above the message there is a run line: "Daily brief · {generated time} · run log", linking to `/settings/scheduled-tasks?run={run_id}`.
  - The conversation header for task threads shows a schedule badge and an "edit task" link to `/settings/scheduled-tasks?edit={context_resource_id}`.
  - The Daily brief gadget shows "open full brief" linking to the task thread when the brief slot is owned by an automation. Use the existing brief-schedule/slot read; if the slot's `automation_id` is not exposed to the web, link to `/settings/scheduled-tasks` instead.

- [ ] **Step 1: Write failing tests:**
  - the history renders a "tasks" group above "today" containing only the task thread
  - a daily-brief message renders `[1]` as a brief citation link
  - the header shows "edit task" for a task thread and not for a normal chat
  - both locales render
- [ ] **Step 2: Run** `npm --prefix apps/web run test -- src/modules/chat src/modules/dashboard`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the same command plus `make typecheck`. Expected: PASS.
- [ ] **Step 5: Run** `detect_changes`, then commit: `feat(web): scheduled-task threads and brief messages in chat`.

### Task 8: Phase gate

- [ ] **Step 1:** Run `make lint && make typecheck`. Expected: clean.
- [ ] **Step 2:** Run `uv run pytest tests/unit/modules/automations tests/unit/modules/chat tests/unit/modules/dashboard tests/unit/admission -q` and `npm --prefix apps/web run test`. Expected: PASS. Report the real counts.
- [ ] **Step 3:** Run `detect_changes({scope:"compare", base_ref:"main"})` and report the affected processes. Any HIGH/CRITICAL needs a written justification.
- [ ] **Step 4:** Update `docs/IMPLEMENTATION_STATUS.md` with a "Scheduled tasks — Phase 1" entry: scope, the validation results, and that Docker e2e has not been run (that needs owner approval).
- [ ] **Step 5:** Commit: `docs: record scheduled tasks phase 1 status`. Do not push.

---

Phase 2 (Topic watch) and Phase 3 (create from chat) each get their own plan after Phase 1 merges. They build on `scheduled_task_kind`, `append_task_message` and the Scheduled tasks page from this plan.
