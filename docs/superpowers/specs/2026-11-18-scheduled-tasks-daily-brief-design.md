# Design: Scheduled tasks — Daily brief and Topic watch

Status: approved UI (artifact https://claude.ai/artifact/QBvVoZdi7tsc2THAD1ndXf, group "Scheduled tasks (proposal)",
version `1794973160-6fbd`); this document awaits owner approval before planning.

| Phase | Scope | Gate |
|---|---|---|
| 1 | Scheduled-tasks page on top of automations; Daily brief task posts into its own chat thread; brief gets market / weather / hazard / source-health facts; task threads visible under "tasks" in Chat with server-side retrieval scope | pytest automations + chat + dashboard brief; vitest page + both locales; tsc; eslint; one brief run posts exactly one message on retry |
| 2 | Topic watch: prompt → web search → bounded fetch → dedupe → relevance filter → 2-sentence summaries → Documents of a task-owned Source; `ai task` gadget | collector tests with mocked transport (SSRF refusal, byte cap, dedupe, injected page ignored, quota exhaustion); vitest gadget |
| 3 | Create a task from chat ("describe it in chat"): chat proposes a task card, owner confirms | chat tool test: draft never saves without explicit confirm; vitest card |

## Intent / why
Owner wants an AI datasource driven by a prompt: they write what to follow, a schedule runs it, results show on a
dashboard gadget and can be queried in chat, with summaries and links. Model: ChatGPT Tasks / Claude app
"Scheduled tasks" (page with Yours / Templates, new-task menu, one thread per task). First use is a **daily brief**;
second is a **topic watch** where the AI finds sources on its own for a user-given topic.

## Goals
1. Settings › Data & AI › **Scheduled tasks** page: Yours (list with state, cron + plain-language schedule, last run
   counts, next run, actions run now / pause / edit / history) and Templates (8 cards; Daily brief and Topic watch are
   live, others are pre-filled templates of those two kinds or hidden until their kind exists).
2. Each task owns one chat thread. Every run posts its result there as an assistant message with citations.
   Follow-up questions in that thread retrieve only from the task's own sources (server-enforced).
3. **Daily brief** (Phase 1): sections top stories, markets, weather, hazards near you, needs-attention
   (failing sources); topic-watch news added in Phase 2; optional extra instruction; schedule; reads only already-collected data.
   One per workspace.
4. **Topic watch** (Phase 2): topic instruction, plain-language keep rule, languages, include/exclude sites,
   max new items per run, schedule. Each item stored as a Document with its URL, title, 2-sentence summary,
   fetched-at; searchable in chat and shown on an `ai task` gadget.
5. Run log per run: queries, results, fetched / blocked / timeout, duplicates, off-topic, items, tokens.

## Non-goals
- Telegram delivery (no outbound Telegram exists; deferred). The checkbox is hidden.
- Multiple daily briefs per workspace (brief history is per day per spec §1449).
- Free-roaming research agent. Topic watch is a fixed pipeline, not `run_agent`.
- New HTML-extraction or crawling dependencies.

## Anti-goals (failure even if it "works")
- Fetched page text able to trigger tools, read owner data or change what is stored beyond its own item.
- Citations pointing to URLs the run did not fetch.
- A retried run posting the brief twice, or spending tokens twice for the same slot.
- Task threads leaking into other chats' retrieval, or other sources leaking into a task thread.
- Web egress or spend that the owner did not consent to.

## Constraints (from code and spec)
- Owner module for schedules and runs: `modules/automations` (cron parser, `AutomationSchedule`, durable runs,
  retries, `MAX_RUNS_PER_HOUR`, 5-minute floor for model-spending rules, missed slots coalesce, one conversation per
  rule with `context_kind="automation"`). `modules/tasks` already means to-dos: "scheduled task" is **UI wording
  only**; no `modules/scheduled_tasks`, no `task_*` backend names.
- Brief generator exists: `modules/dashboard/briefs.py:generate_brief` (fingerprinted, immutable revisions,
  citations required) and automations `generate_brief` action; `BriefSchedule` slot can be owned by `automation`.
- Chat has no public command to append an assistant message; `chat/public.py` re-exports ORM classes (existing leak,
  do not widen it).
- Documents require an active Source; connector ingestion goes through `ingestion.accept_native_collection` with a
  collector lease; collectors register via `connectors/native_fetch.register_collector`.
- SSRF-safe fetch: `connectors/providers/rest.fetch_bounded`; HTML → text: `connectors/_collection/web.py`
  parser; web search: `chat/web_search.search` gated by `web_search_permitted` and `consume_daily_quota`.
- Spec: agent/model runs bounded by steps, time and tokens (§4492, §4505); budgets live in AI settings (§4643).
- Every changed production function gets JSDoc / docstring (AGENTS.md). GitNexus impact before each symbol edit.

## Approaches considered
| | Approach | Trade-off | Verdict |
|---|---|---|---|
| A | Extend `modules/automations` (task = rule with schedule trigger + task facade fields in the revisioned definition); topic watch = connector collector on a task-owned Source | Reuses cron, runs, retries, approval, erasure, indexing, citations. Cost: new chat append command, a collector | **Chosen** |
| B | New scheduled-tasks module with its own tables and runner | Duplicates automations' scheduling, retries, idempotency; two cron systems | Rejected |
| C | Embed an OSS researcher (GPT Researcher / Khoj) as sidecar | Outside the Python module boundary, egress / privacy policy not enforceable, heavy | Rejected |
| D | Topic watch as RSS-only + AI filter | Cheapest, safest, but cannot find sources beyond chosen feeds — fails "AI finds sources itself" | Rejected |

## Decisions (owner, 2026-11-18)
- Topic watch consent: **one-time consent at creation** (web search + estimated cost). Editing instruction,
  schedule, sites or max items re-requires consent. Runs then proceed unattended.
- **One Daily brief per workspace**; the Daily brief template is hidden/disabled when one exists.
- **Telegram deferred.**
- Task threads appear under a **"tasks" group in the Chat history**; other automation conversations stay hidden.
- Deleting a topic watch **asks**, with "also delete the N collected items" **off by default**. Kept items stay
  under the Source, which becomes an ordinary archived source (still searchable). The task's chat thread is always
  deleted with the task.

- 2026-11-18 (later): **per-task model choice in Phase 1**, as a choice among the configured model roles
  (brief / chat / background).

## Builder-check calls (made by Claude; owner may override)
1. **When a run posts.** A run posts only when it yields a brief revision that has not been posted to the thread yet;
   the idempotency key is the brief revision id. A reused revision (unchanged inputs) posts nothing, and the run log
   says "no new data since HH:MM". "Run now" is a normal run under the same rule. The Phase 1 gate tests one message
   per revision across retries.
2. **Existing brief setups.** Creating a Daily brief task takes over the `BriefSchedule` slot
   (`schedule_owner='automation'`) and pre-fills its time from the current internal schedule. An existing non-task
   `generate_brief` rule counts toward the one-per-workspace limit: the template is disabled with "convert existing
   brief rule" (adds the task facade to that rule). There is no data migration. Deleting the Daily brief task returns
   the slot to `internal_brief` with the internal schedule disabled.
3. **Task-thread capabilities.** Retrieval is pinned to the task scope across the whole thread. Per-message web
   search keeps the normal opt-in toggle, which is explicit and visible, so it does not count as a leak. `db_query`
   (query_collected_data) is limited to the same source scope. Other chat features are unchanged.
4. **Market / weather / hazard facts.** Each fact is mapped from the connector provider of its source (for example
   coingecko / gold / FX feeds → markets, open_meteo → weather, emsc / nasa_firms / disasters → hazards). The exact
   provider lists are fixed in the plan from `provider_specs`. A section with no data is omitted, never shown empty.
   "Near you" uses the workspace location if one is set; otherwise hazards are global top-N by severity.
   The topic-watch section ships in Phase 2.
5. **Pause / delete.** Pause = rule disabled; thread and data kept. Delete Daily brief = rule and thread deleted;
   saved brief revisions are kept (spec §1449).
6. **Topic-watch budget.** The estimate is queries × (plan + per-page summary tokens) × max_items, shown at consent.
   The per-run hard cap is 1.5 × the estimate in tokens, plus the web-search quota. When either runs out, the run
   stops, keeps the items already saved, is marked `partial`, and the log shows the reason. There is no
   workspace-wide $ budget (no AI-settings budget field exists yet).

## Design

### Task model (no new table)
A scheduled task is an `Automation` whose revision has a `schedule` trigger and a `task` facade in its definition:
`{kind: "daily_brief" | "topic_watch", name, instruction?, delivery: {chat: bool, dashboard: bool}, consent?: {
hash, granted_at, est_tokens}}`. `GET /automations?view=tasks` filters to rules with a `task` facade.
Templates are a static frontend list; picking one pre-fills the editor.

### Delivery to chat
New public command in `modules/chat/public.py`: append an assistant message to an existing conversation inside the
caller's unit of work, with citations and `metadata_json={"origin": "automation_run", "run_id"}`, idempotent on a caller-supplied
key (for the brief: `brief:<revision_id>`; for topic watch: `run:<run_id>`). Automations calls it after a successful
action. A retry finds the existing message and does nothing.

### Daily brief (Phase 1)
- `GenerateBriefAction` gains `deliver_chat` (default true) and `sections` (ordered subset) and optional
  `extra_instruction` (bounded length, passed as owner instruction, not system prompt).
- `dashboard.context.build_daily_widgets` gains facts from observations (market prices, weather, hazards within a
  radius of followed places) via `knowledge.observations.public` and source health via `sources.public`
  (signal for "failing" to be confirmed during planning). Every fact carries `source_ids` so citation validation holds.
- Brief stays per workspace per day; the task owns the `BriefSchedule` slot (`schedule_owner='automation'`).

### Task thread retrieval
Chat history lists automation conversations whose rule has a `task` facade under "tasks". For turns in such a
conversation the server derives `source_scope` (enforced) from the rule: topic watch → its Source; daily brief →
sources cited by its briefs anywhere in the thread. Never from the client.

### Topic watch (Phase 2)
- On create: `sources` creates a Source `type="web", provider="topic_watch"`, configuration
  `{topic, keep_rule, languages, include_sites, exclude_sites, max_items}`; the rule stores `source_id`.
- New action `collect_source(source_id)` requests a collection through `connectors.public.request_collection`;
  allowed unattended only when the rule's consent hash matches the current definition.
- `TopicWatchCollector` pipeline, each step bounded:
  1. plan ≤ 4 queries (LLM, structured JSON);
  2. `web_search.search` per query under quota and `web_search_permitted`;
  3. canonicalise URLs (lowercase host, drop fragment, strip `utm_*`/`fbclid`/`gclid`, sort query) → skip already
     collected `external_id`s; drop near-duplicate titles within the run;
  4. `fetch_bounded` each page (public addresses only, redirect re-check, byte/time caps); parse to text;
  5. relevance filter + 2-sentence summary per page (LLM, JSON output; page text delimited as untrusted data; no tool
     calling; URL taken from fetch, never from model output);
  6. emit native records → Documents (title, summary, url, site, published/fetched time).
- Run log stored on the run action result (counts and queries), shown in run detail.

### Web UI
- `src/app/(workspace)/settings/scheduled-tasks/page.tsx` + workspace component in `modules/automations`
  (Yours / Templates tabs, empty state, new-task menu, list, editor dialogs per kind, run detail). Link registered in
  `module-registry.ts` `settingsExtraLinks` and `dataAiIds`. Existing AI-settings automations section stays.
- Chat history: "tasks" group. Thread header: schedule badge, edit task, pause.
- Gadgets: `daily_brief` gadget gains "open full brief" → task thread; Phase 2 adds `ai_task` renderer (list of items
  with summary, site, time, "+N also reported"; states full / compact / partial / paused).
- i18n keys in `en-us` and `vi-vi` (`automations.json`, `chat.json`, `dashboard.json`).

## Left to the builder
- Exact names of new schema fields and the chat command, within the conventions above.
- How "failing source" is detected (confirm `last_error_code` or status field).
- Radius / place source for "hazards near you" (use existing followed places if present; otherwise omit the section).
- Whether the brief sections are a new prompt key or appended to the existing `brief` system prompt.
- Gadget layout details consistent with the mockup.
