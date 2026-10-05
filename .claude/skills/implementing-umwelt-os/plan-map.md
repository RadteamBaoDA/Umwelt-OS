# Umwelt-OS source map

All links resolve from this file. This is a navigation reference; task status lives in the [execution ledger](../../../docs/superpowers/plans/EXECUTION.md). Re-enumerate `docs/superpowers/plans/` if plans are added or renamed.

## Required source documents

- [Canonical specification](../../../specs/personal-intelligence-os-spec-v2.md): product contracts; sections 156–164 clarify the implementation strategy and approved drawer.
- [Master plan](../../../docs/superpowers/plans/2026-09-25-umwelt-os-master-plan.md): dependencies, shared contracts, verification protocol and external gates.
- [Implementation status](../../../docs/IMPLEMENTATION_STATUS.md): delivered versus planned capabilities.
- [Architecture decisions](../../../docs/ARCHITECTURE_DECISIONS.md): selected stack and owner decisions.
- [Execution ledger](../../../docs/superpowers/plans/EXECUTION.md): active/next task, evidence, blockers and handoff.

## Every phase

The master owns exact dependencies. Default order is 1 through 12 after Phase 0 acceptance; an unmet live gate allows independent work but not dependent acceptance.

| Phase / plan | Task coverage | Specification sections |
| --- | --- | --- |
| [0: Foundation](../../../docs/superpowers/plans/2026-09-25-umwelt-os-phase-0.md) | Tasks 1–7: API/tooling, exclusive owner setup, sessions/CSRF, worker health, frontend, Compose/CI, acceptance | 72–94, 95, 118–120, 134–135, 139–143, 156–163 |
| [1: Core data](../../../docs/superpowers/plans/2026-09-25-umwelt-os-phase-1-core-data-platform.md) | P01-T1–T4: public auth and packaging; Source/Document revisions; library UI; seed/contracts | 2.3, 13, 73–85, 95, 110–112, 139–156 |
| [2: Ingestion](../../../docs/superpowers/plans/2026-09-25-umwelt-os-phase-2-ingestion-connectors.md) | P02-T1–T4: durable receipt; file parsing/chunks; n8n/Crawlee; collection UI/deletion | 30–31, 52–55, 67–68, 89, 95, 138.3–6, 158–161 |
| [3: Search/models](../../../docs/superpowers/plans/2026-09-25-umwelt-os-phase-3-search-model-gateway.md) | P03-T1–T4: gateway/privacy; lexical/embedding indexes; search/palette; live model acceptance | 8, 33–35, 63–65, 76–77, 95, 113–114, 121–123, 157 |
| [4: Entities](../../../docs/superpowers/plans/2026-09-25-umwelt-os-phase-4-entity-knowledge.md) | P04-T1–T4: entities/evidence; extraction; corrections/merge/split; knowledge UI | 13, 32, 34–35, 49–51, 95, 128–129, 138.11–14 |
| [5: Temporal knowledge](../../../docs/superpowers/plans/2026-09-25-umwelt-os-phase-5-temporal-knowledge.md) | P05-T1–T4: Graphiti/FalkorDB gate; events/time; graph sync; Timeline | 7, 13, 34, 48, 95, 138.12–14, 158, 161–162 |
| [6: Ask/drawer/memory](../../../docs/superpowers/plans/2026-09-25-umwelt-os-phase-6-ask-chat-drawer-memory.md) | P06-T1–T4: retrieval/citations; conversations/stream replay; drawer/Ask; selective memory | 34–43, 65, 95, 111–114, 138.2/15/24, 157, 164 |
| [7: Agent tools](../../../docs/superpowers/plans/2026-09-25-umwelt-os-phase-7-agent-harness-tools.md) | P07-T1–T4: registry/MCP; LangGraph; approvals/effect reconciliation; specialists/browser-use | 9, 39–40, 62, 87–88, 95, 138.16–17, 140–150, 158, 160 |
| [8: Today/tasks](../../../docs/superpowers/plans/2026-09-25-umwelt-os-phase-8-today-daily-chat-tasks.md) | P08-T1–T4: tasks/goals/topics; stories/trends; brief/day context; Today drawer UX | 38, 44–47, 56–59, 66, 95, 115, 138.1/7–9/19–21, 144, 164 |
| [9: GitHub](../../../docs/superpowers/plans/2026-09-25-umwelt-os-phase-9-github-integration.md) | P09-T1–T4: workflows/setup; pagination/webhooks; canonical mapping; UI/E2E | 53, 95, 115, 138.5, 159 |
| [10: Automation](../../../docs/superpowers/plans/2026-09-25-umwelt-os-phase-10-automation-workflows.md) | P10-T1–T4: rules/preview; durable dispatch; agent/UI; rule-pack acceptance | 10, 60–61, 95, 138.18, 158–159 |
| [11: Operations](../../../docs/superpowers/plans/2026-09-25-umwelt-os-phase-11-observability-operations.md) | P11-T1–T4: traces/metrics; operations/quality UI; retention/lifecycle; optional Langfuse | 69–70, 95, 110, 126–127, 130, 138.21/23/28, 161 |
| [12: Release](../../../docs/superpowers/plans/2026-09-25-umwelt-os-phase-12-hardening-release-acceptance.md) | P12-T1–T5: backup/restore; deletion/security; onboarding/accessibility; target capacity; clean-install/upgrade | 71, 78–94, 108–135, 155, 161–162 |

## Cross-phase lookups

| Question | Authoritative route |
| --- | --- |
| Public auth helper or new module missing from image/wheel/migrations? | P01-T1; master Public Contracts and Ownership. Includes runner/CI discovery, not just moving route helpers. |
| Receipt acknowledged before queue survives restart? | P02-T1; spec 158–159: durable PostgreSQL batch/pending work before acknowledgement; idempotent dispatch. |
| Remote reasoning/embeddings or fallback permitted? | P03-T1/T2, P06-T1, P07; spec 157: separate owner grants, source-local policy, destination guarantees, fixed embedding identity per generation. |
| Drawer close versus Stop, reconnect or expanded Ask? | P06-T2/T3; spec 42/164: close hides UI, Stop cancels, same conversation on `/ask`, no duplicate sends. |
| Historical Today and selected date? | P08-T3/T4; spec 46/164: date + timezone bound conversations, saved brief revisions, currently retained records with update time; no implied historical task snapshot. |
| Approval or external side-effect retry? | P07-T3; spec 158: immutable action/arguments, expiry, execution-time revalidation, reconciliation of uncertain outcomes. |
| Collection schedules versus automation authoring? | P02-T3 owns initial n8n collection; P09 specializes GitHub; P10 adds rule authoring. |
| Corrections/deletion through derived stores? | P01 → P02 → P04/P05/P06, then P12-T2 audits the full lifecycle. |
| Release on the mini host? | P12-T4/T5, spec 108/132–135/155/161–162, ledger External acceptance gates. Mocks and a larger workstation do not satisfy live or 2-core/8GiB acceptance. |

## Example: resume safely

Request: “Continue implementation from the saved checkpoint.”

Read current status, ledger and code first. If the ledger still selects P01-T1, load Phase 1 and verify Phase 0 evidence. Trace public auth extraction and every packaging/test-runner consumer. Use the existing disposable full test runner until targeted flags are implemented. Record real failing/passing evidence and advance to P01-T2 only after P01-T1 acceptance. If the checkpoint has advanced, use its current task instead.
