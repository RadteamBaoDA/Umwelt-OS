---
name: implementing-umwelt-os
description: Use when preparing, implementing, resuming, or reviewing Umwelt-OS Personal Intelligence OS tasks from personal-intelligence-os-spec-v2.md or docs/superpowers/plans, including phase dependencies, acceptance gates, and execution handoffs.
---

# Implementing Umwelt-OS

Repository-local reference for the approved specification and every implementation plan. The source documents own requirements and progress; this skill supplies navigation, not a second specification or completion ledger.

## Start here

Paths below are relative to the repository root unless linked. Locate the checkout containing `specs/personal-intelligence-os-spec-v2.md`; run commands there.

1. Read `AGENTS.md` and run `git status --short`. Preserve existing edits.
2. Read `docs/ARCHITECTURE_DECISIONS.md` and only the newest sections at the top of `docs/IMPLEMENTATION_STATUS.md` and `docs/superpowers/plans/EXECUTION.md` (both are newest-first and hundreds of KB; grep them for older history only when the task needs it).
3. Read [R01–R16](../../../docs/superpowers/plans/2026-09-30-umwelt-os-spec-reconciliation.md) and preserve existing Phase 4 work before reading the [master plan](../../../docs/superpowers/plans/2026-09-25-umwelt-os-master-plan.md), then use [plan-map.md](plan-map.md) to select the phase and specification sections. Read the selected plan completely, including its entry gate, interfaces, fixtures and failure cases; inspect current code before editing.
4. Resolve requirements against the canonical [specification](../../../specs/personal-intelligence-os-spec-v2.md), especially revisions 156–167. Later explicit clarifications govern earlier examples. Report unresolved material contradictions rather than silently changing architecture.

When implementation is requested, use **superpowers:subagent-driven-development** with the approved scope. This skill does not start background execution.

## Quick reference

| Need | Read |
| --- | --- |
| Current/next task, blocked evidence | `docs/superpowers/plans/EXECUTION.md` |
| Phase dependency and shared contracts | Master plan |
| Exact task files, tests and acceptance | Selected phase plan |
| Product behavior and architectural constraints | Specification + architecture decisions |
| All phases and feature routing | [plan-map.md](plan-map.md) |

## Implementation handoff

For frontend work, also load [umwelt-os-ui-system](../umwelt-os-ui-system/SKILL.md) and follow [DESIGN_SYSTEM.md](../../../docs/DESIGN_SYSTEM.md). These owner-selected shadcn/Recharts, theme, locale, dialog and chat-surface decisions govern older UI examples in phase plans.

Record task ID, prerequisite evidence, affected contracts/files, build and test commands/results, review findings, unresolved gates, and next task at the top of `EXECUTION.md`. Keep each implementer on one finite task; independent tasks may run concurrently in isolated worktrees under the controller, with shared builds serialized. Validate as described in the Testing Guidelines of `AGENTS.md`. Follow the master plan and continue authorized ready work without repeated scope approval.

## Common mistakes

- Treating plan examples as existing code: verify imports, packaging, fixtures and runner flags.
- Freezing the next task in this skill: always reread the ledger and reconcile with current code.
- Equating mocks or larger-host checks with acceptance: keep live-provider, graph, browser, n8n, target-hardware and restore gates explicit; continue independent work.
- Rebuilding completed Phase 0 or re-planning approved scope: use current evidence and phase dependencies.
- Respect the current owner authorization: commit accepted large task/phase milestones and merge their isolated worktrees into develop, batching checkpoint files with accepted source. Do not push, deploy or merge main without the applicable current direction.

Document code as required by the Mandatory Code Documentation section of `AGENTS.md`.
