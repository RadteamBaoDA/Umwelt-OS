# Generative UI parity matrix

Upstream: CopilotKit/OpenIntelligentUI @ `8391a94c1c3939c679e40ddd7bfc08a9ef690e70` (MIT).
Upstream paths below come from the plan inventory (`theo-review-c-a-th-ng-calm-piglet.md`) and are
approximate; they were not re-fetched in Wave 0. Umwelt-OS statuses were set by grepping the current code.

Statuses: `ported`, `equivalent` (same user-visible behaviour through a different design), `excluded-by-owner`
(owner decision, reason in the evidence column), `not-ported` (real gap, must reach zero).

Rules enforced by `scripts/check_genui_parity.py`: every row has all columns, status is one of the four values,
every backticked path in the evidence and proof columns (a trailing `:line` is stripped) exists in the repo, and no
row is `not-ported`. A `not-ported` or `excluded-by-owner` row may use plain text (`none`, a reason) in
evidence/proof; other rows need non-empty text. Pending real-model, Docker and Playwright runs are written as
`pending:` plain text in the proof column and need owner approval.

Gate wiring: `make lint` and `./scripts/dev.ps1 lint` currently pass `--allow-not-ported` so the gap stays visible
without blocking. The final gate (end of the port) drops that flag so any remaining `not-ported` row fails lint.

## Matrix

| id | upstream feature | upstream path | status | Umwelt-OS evidence | proof |
|---|---|---|---|---|---|
| T01 | generate_form tool | agent/tools/forms | not-ported | no functional render_form tool; only the time-picker slot `modules/chat/generative_ui/tools/form.py:24` exists | none (Wave C) |
| T02 | manage_todos tool | agent/tools/todos | not-ported | none | none (Wave D) |
| T03 | get_todos tool | agent/tools/todos | not-ported | none | none (Wave D) |
| T04 | todo state schema | agent/state | not-ported | only plan-embedded todos `modules/chat/generative_ui/tools/plan.py:36`; no conversation-scoped store | none (Wave D) |
| T05 | plan_visualization tool | agent/tools/plan | equivalent | `modules/chat/generative_ui/tool_specs.py:20`, `modules/chat/generative_ui/tools/plan.py:62` | `tests/unit/modules/chat/generative_ui/test_tool_specs.py` |
| T06 | query_data tool | agent/tools/query | equivalent | `modules/chat/generative_ui/tools/query.py:113`, `modules/chat/generative_ui/tool_specs.py:18` | `tests/unit/modules/chat/generative_ui/test_query.py` |
| T07 | get_trip_stop_images tool | agent/tools/images | not-ported | none | none (Wave F) |
| T08 | save_template tool | agent/tools/templates | not-ported | none (static seed templates only) | none (Wave G) |
| T09 | list_templates tool | agent/tools/templates | not-ported | none | none (Wave G) |
| T10 | apply_template tool | agent/tools/templates | not-ported | none | none (Wave G) |
| T11 | delete_template tool | agent/tools/templates | not-ported | none | none (Wave G) |
| T12 | clear_templates tool | agent/tools/templates | not-ported | none | none (Wave G) |
| I01 | agent server and health | agent/main | equivalent | `apps/api/main.py:249` | pending: no dedicated health test |
| I02 | middleware order | agent/main | equivalent | `modules/chat/worker.py:41` (tool-loop pipeline replaces LangGraph middleware) | `tests/integration/test_generative_ui_worker.py` |
| I03 | system prompt | agent/prompt | ported | `modules/chat/generative_ui/prompt.py:17`, `modules/chat/generative_ui/prompt.py:162` | `tests/unit/modules/chat/generative_ui/test_canonical_contract.py` |
| I04 | model selection | agent/models | equivalent | `modules/settings/routes.py:128`, `modules/settings/routes.py:184` | `tests/unit/modules/settings/test_settings.py` |
| I05 | Jev router as default | agent/router | excluded-by-owner | owner: Jev is not the default; internal router `modules/chat/generative_ui/visualization_router.py:157` is kept (external router option is a later opt-in) | `tests/unit/modules/chat/generative_ui/test_visualization_router.py` |
| I06 | render-tool filter | agent/tools | equivalent | `modules/chat/generative_ui/tool_specs.py:138` | `tests/unit/modules/chat/generative_ui/test_tool_specs.py` |
| I07 | map and animated-route guidance | agent/prompt | ported | `modules/chat/generative_ui/prompt.py:83`, `modules/chat/generative_ui/skills/agent-skills-vol2/SKILL.md:111` | `tests/unit/modules/chat/generative_ui/test_skills_contract.py` |
| I08 | BYOK credentials | agent/byok | equivalent | `modules/settings/routes.py:134` (owner AI settings store the provider credential) | `tests/unit/modules/settings/test_providers.py` |
| I09 | key validation endpoint | agent/byok | equivalent | `modules/settings/routes.py:145` | `tests/unit/modules/settings/test_providers.py` |
| I10 | request-scoped model | agent/models | equivalent | `modules/chat/worker.py:42` | `tests/unit/modules/chat/test_generative_ui_worker.py` |
| I11 | anthropic compat | agent/models | equivalent | `core/model_gateway/schemas.py` | `tests/unit/modules/settings/test_providers.py` |
| I12 | bounded checkpointer | agent/checkpoint | equivalent | `modules/chat/history.py:1` (bounded turns and chars, DB-backed, no LangGraph checkpointer) | `tests/unit/modules/chat/test_p14_generation_worker.py` |
| I13 | skills backend: master-playbook | agent/skills | ported | `modules/chat/generative_ui/skills/master-agent-playbook/SKILL.md`, `modules/chat/generative_ui/skill_backend.py:10` | `tests/unit/modules/chat/generative_ui/test_skill_backend.py` |
| I14 | skills backend: advanced-visualization | agent/skills | ported | `modules/chat/generative_ui/skills/agent-skills-vol2/SKILL.md` | `tests/unit/modules/chat/generative_ui/test_skills_contract.py` |
| I15 | skills backend: svg-diagrams | agent/skills | ported | `modules/chat/generative_ui/skills/svg-diagram-skill/SKILL.md` | `tests/unit/modules/chat/generative_ui/test_skills_contract.py` |
| F01 | chat page | app/page | equivalent | `apps/web/src/app/chat/page.tsx`, `apps/web/src/modules/chat/chat-session.tsx` | pending: `tests/e2e/generative-ui.spec.ts` needs Playwright and owner approval |
| F02 | providers | app/providers | equivalent | `apps/web/src/core/query-provider.tsx`, `apps/web/src/modules/chat/generative-ui/bridge/prompt-bridge.tsx:72` | `apps/web/src/modules/chat/generative-ui/__tests__/prompt-bridge.test.tsx` |
| F03 | runtime route and rate limit | app/api/copilotkit | equivalent | `modules/chat/routes.py:22` (`require_budget` from `core/rate_limit.py`), `core/rate_limit.py` | `tests/unit/modules/chat/test_p14_generation_worker.py` |
| F04 | runtime options | app/api/copilotkit | excluded-by-owner | owner: CopilotKit/LangGraph runtime is replaced by the native SSE chat API | none (owner decision) |
| F05 | frontend tool: toggleTheme | app/hooks | not-ported | none (theme is a Settings preference, not a model tool) | none |
| F06 | frontend tool: pieChart | app/hooks | ported | `apps/web/src/modules/chat/generative-ui/components/charts/pie-chart.tsx` | `apps/web/src/modules/chat/generative-ui/__tests__/charts.test.tsx` |
| F07 | frontend tool: barChart | app/hooks | ported | `apps/web/src/modules/chat/generative-ui/components/charts/bar-chart.tsx` | `apps/web/src/modules/chat/generative-ui/__tests__/charts.test.tsx` |
| F08 | frontend tool: plan renderer | app/hooks | ported | `apps/web/src/modules/chat/generative-ui/components/plan-card.tsx:139` | `apps/web/src/modules/chat/generative-ui/__tests__/plan-card.test.tsx` |
| F09 | frontend tool: default tool renderer | app/hooks | equivalent | `apps/web/src/modules/chat/generative-ui/components/tool-rendering.tsx:44` | `apps/web/src/modules/chat/generative-ui/__tests__/widget-copy-and-payloads.test.tsx` |
| F10 | frontend tool: scheduleTime HITL | app/hooks | not-ported | time picker `apps/web/src/modules/chat/generative-ui/components/meeting-time-picker.tsx:118` answers by a new message; no run pause | none (Wave E) |
| F11 | starter suggestions | app/suggestions | ported | `apps/web/src/modules/chat/generative-ui/templates/index.ts:48`, `apps/web/src/modules/chat/chat-transcript.tsx:185` | `apps/web/src/modules/chat/generative-ui/__tests__/template-library.test.tsx` |
| F12 | shared state: todos | app/state | not-ported | none | none (Wave D) |
| F13 | shared state: templates | app/state | not-ported | seed templates only `apps/web/src/modules/chat/generative-ui/templates/seed-templates.ts:5`; no persisted user templates | none (Wave G) |
| F14 | thread and session | app/thread | equivalent | `apps/web/src/modules/chat/chat-session.tsx`, `modules/chat/routes.py:332` | `apps/web/src/modules/chat/api-sse.test.ts` |
| F15 | reader scroll view | app/scroll | equivalent | `apps/web/src/modules/chat/use-chat-scroll.ts:9` | `apps/web/src/modules/chat/chat-side-rail.test.tsx` |
| F16 | answer markdown and reveal | app/markdown | equivalent | `apps/web/src/modules/chat/chat-markdown.tsx` | `apps/web/src/modules/chat/api-sse.test.ts` |
| F17 | error and retry | app/error | equivalent | `apps/web/src/modules/chat/chat-transcript.tsx:105`, `apps/web/src/modules/chat/chat-session.tsx:479` | `apps/web/src/modules/chat/api-sse.test.ts` |
| F18 | default tool renderer (ToolReasoning) | app/tool-reasoning | not-ported | no tool-step or reasoning view in the transcript | none |
| F19 | theme | app/theme | ported | `apps/web/src/core/preferences.ts:4`, `apps/web/src/modules/chat/generative-ui/sandbox/websandbox-loader.ts:128` | `apps/web/src/modules/chat/generative-ui/__tests__/design-system-parity.test.ts` |
| F20 | fonts | app/layout | equivalent | `apps/web/src/app/globals.css:60` | pending: visual check |
| F21 | metadata and OG | app/layout | equivalent | `apps/web/src/app/layout.tsx:8` (title and description only; no OG image for a private app) | pending: visual check |
| F22 | web health | app/api/health | equivalent | `apps/api/main.py:249` | pending: no dedicated health test |
| W01 | meeting time picker widget | components/meeting-time-picker | ported | `apps/web/src/modules/chat/generative-ui/components/meeting-time-picker.tsx:118` | `apps/web/src/modules/chat/generative-ui/__tests__/meeting-time-picker.test.tsx` |
| W02 | pie chart widget | components/charts | ported | `apps/web/src/modules/chat/generative-ui/components/charts/pie-chart.tsx` | `apps/web/src/modules/chat/generative-ui/__tests__/charts.test.tsx` |
| W03 | bar chart widget | components/charts | ported | `apps/web/src/modules/chat/generative-ui/components/charts/bar-chart.tsx` | `apps/web/src/modules/chat/generative-ui/__tests__/charts.test.tsx` |
| W04 | A2UI table catalog | a2ui/catalog | ported | `apps/web/src/modules/chat/generative-ui/components/table-catalog.tsx:232` | `apps/web/src/modules/chat/generative-ui/__tests__/table-catalog.test.tsx` |
| W05 | plan card widget | components/plan-card | ported | `apps/web/src/modules/chat/generative-ui/components/plan-card.tsx:139` | `apps/web/src/modules/chat/generative-ui/__tests__/plan-card.test.tsx` |
| S01 | generateSandboxedUi schema | sandbox/schema | ported | `apps/web/src/modules/chat/generative-ui/sandbox/schema.ts:131`, `modules/chat/generative_ui/prompt.py:118` | `tests/unit/modules/chat/generative_ui/test_canonical_contract.py` |
| S02 | streaming renderer: idiomorph | sandbox/renderer | ported | `apps/web/src/modules/chat/generative-ui/sandbox/idiomorph-inline.ts:10`, `apps/web/src/modules/chat/generative-ui/sandbox/websandbox-loader.ts:57` | `apps/web/src/modules/chat/generative-ui/__tests__/open-generative-ui-renderer.test.tsx` |
| S03 | streaming renderer: throttle 1000ms | sandbox/renderer | ported | `apps/web/src/modules/chat/generative-ui/sandbox/renderer.tsx:35` | `apps/web/src/modules/chat/generative-ui/__tests__/open-generative-ui-renderer.test.tsx` |
| S04 | frame content and CSP | sandbox/frame-content | ported | `apps/web/src/modules/chat/generative-ui/sandbox/frame-content.ts:37` | `apps/web/src/modules/chat/generative-ui/__tests__/frame-nonce.test.ts` |
| S05 | autosize | sandbox/frame-content | ported | `apps/web/src/modules/chat/generative-ui/sandbox/renderer.tsx:59`, `apps/web/src/modules/chat/generative-ui/bridge/sandbox-functions.ts:19` | `apps/web/src/modules/chat/generative-ui/__tests__/open-generative-ui-renderer.test.tsx` |
| S06 | websandbox loader | sandbox/websandbox | ported | `apps/web/src/modules/chat/generative-ui/sandbox/websandbox-loader.ts:159` | `apps/web/src/modules/chat/generative-ui/__tests__/open-generative-ui-renderer.test.tsx` |
| S07 | sendPrompt bridge | sandbox/functions | ported | `apps/web/src/modules/chat/generative-ui/bridge/sandbox-functions.ts:97`, `apps/web/src/modules/chat/generative-ui/bridge/prompt-bridge.tsx:72` | `apps/web/src/modules/chat/generative-ui/__tests__/prompt-bridge.test.tsx` |
| S08 | openLink bridge | sandbox/functions | ported | `apps/web/src/modules/chat/generative-ui/bridge/sandbox-functions.ts:66` | `apps/web/src/modules/chat/generative-ui/__tests__/prompt-bridge.test.tsx` |
| S09 | design system: THEME_CSS | sandbox/design-system | ported | `apps/web/src/modules/chat/generative-ui/design-system/index.ts:256` | `apps/web/src/modules/chat/generative-ui/__tests__/design-system-parity.test.ts` |
| S10 | design system: SVG classes | sandbox/design-system | ported | `apps/web/src/modules/chat/generative-ui/design-system/index.ts:277` | `apps/web/src/modules/chat/generative-ui/__tests__/design-system-parity.test.ts` |
| S11 | design system: form styles | sandbox/design-system | ported | `apps/web/src/modules/chat/generative-ui/design-system/index.ts:398` | `apps/web/src/modules/chat/generative-ui/__tests__/design-system-parity.test.ts` |
| S12 | design system: importmap | sandbox/design-system | ported | `apps/web/src/modules/chat/generative-ui/design-system/index.ts:528`, `apps/web/scripts/vendor-genui.mjs` | `apps/web/src/modules/chat/generative-ui/__tests__/design-system-parity.test.ts` |
| S13 | design system: design skill | sandbox/design-system | ported | `modules/chat/generative_ui/skills/agent-skills-vol2/SKILL.md` | `tests/unit/modules/chat/generative_ui/test_skills_contract.py` |
| S14 | trip animator | sandbox/trip-animator | ported | `apps/web/src/modules/chat/generative-ui/sandbox/trip-animator.ts:47` | `apps/web/src/modules/chat/generative-ui/__tests__/trip-animator.test.ts` |
| S15 | export overlay | sandbox/export-overlay | ported | `apps/web/src/modules/chat/generative-ui/sandbox/export-overlay.tsx:58` | `apps/web/src/modules/chat/generative-ui/__tests__/export-utils.test.ts` |
| S16 | export utils | sandbox/export-utils | ported | `apps/web/src/modules/chat/generative-ui/sandbox/export-utils.ts:93` | `apps/web/src/modules/chat/generative-ui/__tests__/export-utils.test.ts` |
| S17 | maps and itinerary guidance | sandbox/prompt | equivalent | `modules/chat/generative_ui/skills/agent-skills-vol2/SKILL.md:111` (no remote tiles; remote media is a later opt-in) | `tests/unit/modules/chat/generative_ui/test_skills_contract.py` |
| S18 | 7 example prompts | sandbox/examples | equivalent | `apps/web/src/modules/chat/generative-ui/templates/demo-data.ts:33` | `apps/web/src/modules/chat/generative-ui/__tests__/template-library.test.tsx` |
| U01 | demo gallery | app/demo | ported | `apps/web/src/modules/chat/generative-ui/templates/demo-card.tsx:35`, `apps/web/src/modules/chat/generative-ui/templates/category-filter.tsx:30` | `apps/web/src/modules/chat/generative-ui/__tests__/template-library.test.tsx` |
| U02 | template library | app/templates | ported | `apps/web/src/modules/chat/generative-ui/templates/seed-templates.ts:5`, `apps/web/src/modules/chat/generative-ui/templates/template-card.tsx` | `apps/web/src/modules/chat/generative-ui/__tests__/template-library.test.tsx` |
| U03 | QR modal | app/qr | not-ported | none (upstream-unwired; port or owner-exclude explicitly) | none |
| U04 | /pick page | app/pick | not-ported | none | none |
| U05 | /api/pick | app/api/pick | not-ported | none | none |
| U06 | /api/pick/ip | app/api/pick/ip | not-ported | none | none |
| U07 | desktop-tip modal | app/desktop-tip | not-ported | none | none |
| U08 | explainer cards | app/explainer | ported | `apps/web/src/modules/chat/generative-ui/templates/explainer-cards.tsx` | `apps/web/src/modules/chat/generative-ui/__tests__/template-library.test.tsx` |
| U09 | example layout | app/example | not-ported | none | none |
| B01 | BYOK key dialog | app/byok | equivalent | `modules/settings/routes.py:134` (Settings page replaces the dialog) | `tests/unit/modules/settings/test_settings.py` |
| B02 | key gate | app/byok | equivalent | `modules/settings/routes.py:128` | `tests/unit/modules/settings/test_settings.py` |
| B03 | provider-keys route | app/api/provider-keys | equivalent | `modules/settings/routes.py:145` | `tests/unit/modules/settings/test_providers.py` |
| X01 | launch telemetry | app/telemetry | excluded-by-owner | owner: no telemetry | none (owner decision) |
| X02 | PostHog | app/posthog | excluded-by-owner | owner: no PostHog | none (owner decision) |
| X03 | LangSmith | agent/langsmith | excluded-by-owner | owner: no LangSmith (local export in `core/langfuse_export.py` is separate) | none (owner decision) |
| M01 | MCP http server | mcp/server | equivalent | `modules/tools/mcp_server.py:1` (read-only inbound server; no assemble_document yet) | `tests/unit/modules/tools/test_mcp_auth.py` |
| M02 | MCP stdio | mcp/stdio | equivalent | `modules/tools/mcp_stdio.py` | `tests/unit/modules/tools/test_mcp_stdio.py` |
| M03 | MCP resources | mcp/resources | not-ported | skills are not exposed as MCP resources | none (Wave H) |
| M04 | MCP prompts | mcp/prompts | not-ported | none | none (Wave H) |
| M05 | assemble_document tool | mcp/assemble | not-ported | none | none (Wave H) |
| M06 | mcp integration | mcp/integration | equivalent | `modules/tools/mcp_client.py`, `modules/tools/mcp_dispatch.py` | `tests/unit/modules/tools/test_mcp_client.py` |
| D01 | monorepo scripts | package.json | equivalent | `package.json`, `scripts/dev.ps1` | pending: `npm run lint` |
| D02 | Makefile | Makefile | equivalent | `Makefile:36` | pending: `make lint` |
| D03 | render.yaml | render.yaml | excluded-by-owner | owner: self-hosted Docker Compose deploy, no Render | none (owner decision) |
| D04 | Docker | docker/ | equivalent | `docker/api.Dockerfile`, `docker/web.Dockerfile`, `docker-compose.yml` | pending: Docker needs owner approval |
| D05 | CI | .github | equivalent | `.github/workflows/ci.yml:33` | pending: CI run |
| D06 | docs pages | docs/ | equivalent | `docs/development.md`, `docs/IMPLEMENTATION_STATUS.md` | none (docs) |
| D07 | env vars | .env.example | equivalent | `docs/development.md` | none (docs) |

## Extras (Umwelt-OS only, not upstream rows)

| id | extra | status | Umwelt-OS evidence | proof |
|---|---|---|---|---|
| E01 | Fullscreen preview of widgets | ported | `apps/web/src/modules/chat/generative-ui/sandbox/export-overlay.tsx:58` | `apps/web/src/modules/chat/generative-ui/__tests__/export-utils.test.ts` |
