---
name: implementing-umwelt-os
description: Use when preparing, implementing, resuming, or reviewing Umwelt-OS Personal Intelligence OS tasks from personal-intelligence-os-spec-v2.md or docs/superpowers/plans, including phase dependencies, acceptance gates, and execution handoffs.
---

# Implementing Umwelt-OS

Repository-local reference for the approved specification and every implementation plan. The source documents own requirements and progress; this skill supplies navigation, not a second specification or completion ledger.

## Start here

Paths below are relative to the repository root unless linked. Locate the checkout containing `specs/personal-intelligence-os-spec-v2.md`; run commands there.

1. Read `AGENTS.md` and run `git status --short`. Preserve existing edits.
2. Read `docs/IMPLEMENTATION_STATUS.md`, `docs/ARCHITECTURE_DECISIONS.md`, and `docs/superpowers/plans/EXECUTION.md`.
3. Read [R01–R16](../../../docs/superpowers/plans/2026-09-30-umwelt-os-spec-reconciliation.md) and preserve existing Phase 4 work before reading the [master plan](../../../docs/superpowers/plans/2026-09-25-umwelt-os-master-plan.md), then use [plan-map.md](plan-map.md) to select the phase and specification sections. Read the selected plan completely, including its entry gate, interfaces, fixtures and failure cases; inspect current code before editing.
4. Resolve requirements against the canonical [specification](../../../specs/personal-intelligence-os-spec-v2.md), especially revisions 156–166. Later explicit clarifications govern earlier examples. Report unresolved material contradictions rather than silently changing architecture.

When implementation is requested, use **superpowers:subagent-driven-development** with the approved scope. The owner requires a code/build stage across Phases 1-12 followed by a separate test stage after all production code is complete. This skill does not start background execution.

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

Record task ID, prerequisite evidence, affected contracts/files, build commands/results, review findings, unresolved gates, and next task in `EXECUTION.md`. Keep one task active. During the Phase 1–12 implementation stage, implement production code and run builds only: do not create, modify or run tests, lint, or typecheck. Start the test stage after all phase code is complete. Follow the master plan and continue authorized ready work without repeated scope approval.

During implementation, run `./scripts/dev.ps1 build` on Windows or `make build` on Linux/macOS. Do not create, modify or run tests, lint or typecheck until all original Phase 1-12 and reconciliation production code is complete; then follow the deferred validation stage in the master plan. CI push/pull-request runs install dependencies and build only; manually dispatch its deferred-validation stage only after that gate.


## Common mistakes

- Treating plan examples as existing code: verify imports, packaging, fixtures and runner flags.
- Freezing the next task in this skill: always reread the ledger and reconcile with current code.
- Equating mocks or larger-host checks with acceptance: keep live-provider, graph, browser, n8n, target-hardware and restore gates explicit; continue independent work.
- Rebuilding completed Phase 0 or re-planning approved scope: use current evidence and phase dependencies.
- Respect the current owner authorization: commit completed phases and merge Phase 1 into main after implementation and review; do not push or deploy.


## Code documentation required by the owner

Before handing off generated or changed production code, ensure every named function/method/component/hook has JSDoc (JavaScript/TypeScript) or a Python docstring describing its real purpose and contract. Add inline comments for non-obvious invariants, authorization, transaction/lock ordering, retry/idempotency/deletion behavior and UI state transitions. Preserve accurate existing comments and OSS notices; never fabricate safety guarantees or narrate obvious lines. Follow AGENTS.md's Mandatory Code Documentation section for details and existing-code coverage.

Review documentation alongside source behavior. Keep changes limited to comments/docstrings during a documentation task; preserve signatures and application logic. Tests remain deferred under the owner's code/build-only stage. Update checkpoint files continuously and batch their commits with a completed large task or phase instead of committing every status update.

For other authored production languages, use their native named-function documentation format; keep test-only helpers deferred with the test stage.
