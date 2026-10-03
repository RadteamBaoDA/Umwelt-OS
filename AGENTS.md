# Repository Guidelines

## Project Structure & Module Organization

This repository implements Phase 0 of the single-user, self-hosted Personal Intelligence OS. The approved product and engineering specification remains `specs/personal-intelligence-os-spec-v2.md`; delivery and later-phase status are tracked in `docs/IMPLEMENTATION_STATUS.md`.

Follow the specification when adding code:
- `apps/web/`: Next.js, React, and TypeScript frontend.
- `apps/api/` and `apps/worker/`: Python FastAPI backend and background processing.
- `packages/`: shared UI, contracts, SDK, and configuration.
- `core/` and `modules/<capability>/`: shared infrastructure and domain modules, following the modular-monolith requirements in sections 139–143.
- `tests/integration/`, `tests/e2e/`, and `tests/fixtures/`: cross-module validation.
- `infrastructure/` and `docs/`: deployment configuration and operational documentation.

Keep module internals private; integrate through public contracts or events. Create directories only when used.

## Build, Test, and Development Commands

Use `make setup`, `make dev`, `make stop`, `make migrate`, `make seed`, `make lint`, `make typecheck`, `make test`, and `make build` on macOS/Linux. Windows PowerShell equivalents are `./scripts/dev.ps1 <task>`. `test` creates a uniquely named disposable Compose project and removes only its own volumes. `seed` explicitly creates fictional Phase 1 demo data; `reset`, `backup`, and `restore` are not implemented yet.

## Coding Style & Naming Conventions

Use four-space Python indentation and two-space TypeScript indentation. Use `snake_case` for Python functions/modules, `camelCase` for TypeScript functions, and `PascalCase` for React components and types.

The specification requires ESLint and TypeScript checks for frontend code, and Ruff and mypy for Python. Keep changes focused and avoid unused scaffolding.

## UI Design System

For BBD-OS frontend design, implementation, or review, load `.agents/skills/bbd-os-ui-system/SKILL.md` and follow `docs/DESIGN_SYSTEM.md`. Use shadcn/ui components and semantic tokens, Recharts for financial charts, light/dark/system themes, and the two locale preferences `en-us` / `vi-vi` (normalized to `en-US` / `vi-VN` for formatting). Main navigation remains Dashboard / Chat / Settings. These explicit UI decisions govern older UI examples; they do not authorize unrelated backend changes or override the code/build-only stage.

## Testing Guidelines

Use pytest for backend tests and Playwright for end-to-end flows; the frontend unit-test framework is not selected. For Phases 1-12 and reconciliation tasks, do not create, modify or run tests, lint, or typecheck during implementation; run builds only. Begin the deferred validation stage after all original and reconciliation production code is complete. Name Python tests `test_*.py` and Playwright tests `*.spec.ts`. Cover module contracts, ingestion, permissions, search, and citation flows. No numeric coverage threshold is specified. Report checks run and any unavailable validation.

Cross-module reads use owner `public.py` DTO/query contracts. A narrowly scoped read projection may be approved when its owner, fields, permission and deletion checks are documented; callers must not use that projection for writes. Module persistence models remain private.

## Commit & Pull Request Guidelines

There is no commit history or established message convention. Use concise, imperative subjects, such as `docs: clarify ingestion contracts`. PRs should describe scope, reference relevant specification sections or issues, list validation results, and include screenshots for UI changes.

## Security & Configuration

Never commit secrets or personal data. Provide placeholder configuration in `.env.example` when configuration is introduced. Preserve source provenance and enforce agent access through supported APIs and permission checks.

<!-- gitnexus:start -->
# GitNexus — Code Intelligence

This project is indexed by GitNexus as **BBD-OS** (3144 symbols, 7969 relationships, 260 execution flows). Use the GitNexus MCP tools to understand code, assess impact, and navigate safely.

> If any GitNexus tool warns the index is stale, run `npx gitnexus analyze` in terminal first.

## Always Do

- **MUST run impact analysis before editing any symbol.** Before modifying a function, class, or method, run `gitnexus_impact({target: "symbolName", direction: "upstream"})` and report the blast radius (direct callers, affected processes, risk level) to the user.
- **MUST run `gitnexus_detect_changes()` before committing** to verify your changes only affect expected symbols and execution flows.
- **MUST warn the user** if impact analysis returns HIGH or CRITICAL risk before proceeding with edits.
- When exploring unfamiliar code, use `gitnexus_query({query: "concept"})` to find execution flows instead of grepping. It returns process-grouped results ranked by relevance.
- When you need full context on a specific symbol — callers, callees, which execution flows it participates in — use `gitnexus_context({name: "symbolName"})`.

## When Debugging

1. `gitnexus_query({query: "<error or symptom>"})` — find execution flows related to the issue
2. `gitnexus_context({name: "<suspect function>"})` — see all callers, callees, and process participation
3. `READ gitnexus://repo/BBD-OS/process/{processName}` — trace the full execution flow step by step
4. For regressions: `gitnexus_detect_changes({scope: "compare", base_ref: "main"})` — see what your branch changed

## When Refactoring

- **Renaming**: MUST use `gitnexus_rename({symbol_name: "old", new_name: "new", dry_run: true})` first. Review the preview — graph edits are safe, text_search edits need manual review. Then run with `dry_run: false`.
- **Extracting/Splitting**: MUST run `gitnexus_context({name: "target"})` to see all incoming/outgoing refs, then `gitnexus_impact({target: "target", direction: "upstream"})` to find all external callers before moving code.
- After any refactor: run `gitnexus_detect_changes({scope: "all"})` to verify only expected files changed.

## Never Do

- NEVER edit a function, class, or method without first running `gitnexus_impact` on it.
- NEVER ignore HIGH or CRITICAL risk warnings from impact analysis.
- NEVER rename symbols with find-and-replace — use `gitnexus_rename` which understands the call graph.
- NEVER commit changes without running `gitnexus_detect_changes()` to check affected scope.

## Tools Quick Reference

| Tool | When to use | Command |
|------|-------------|---------|
| `query` | Find code by concept | `gitnexus_query({query: "auth validation"})` |
| `context` | 360-degree view of one symbol | `gitnexus_context({name: "validateUser"})` |
| `impact` | Blast radius before editing | `gitnexus_impact({target: "X", direction: "upstream"})` |
| `detect_changes` | Pre-commit scope check | `gitnexus_detect_changes({scope: "staged"})` |
| `rename` | Safe multi-file rename | `gitnexus_rename({symbol_name: "old", new_name: "new", dry_run: true})` |
| `cypher` | Custom graph queries | `gitnexus_cypher({query: "MATCH ..."})` |

## Impact Risk Levels

| Depth | Meaning | Action |
|-------|---------|--------|
| d=1 | WILL BREAK — direct callers/importers | MUST update these |
| d=2 | LIKELY AFFECTED — indirect deps | Should test |
| d=3 | MAY NEED TESTING — transitive | Test if critical path |

## Resources

| Resource | Use for |
|----------|---------|
| `gitnexus://repo/BBD-OS/context` | Codebase overview, check index freshness |
| `gitnexus://repo/BBD-OS/clusters` | All functional areas |
| `gitnexus://repo/BBD-OS/processes` | All execution flows |
| `gitnexus://repo/BBD-OS/process/{name}` | Step-by-step execution trace |

## Self-Check Before Finishing

Before completing any code modification task, verify:
1. `gitnexus_impact` was run for all modified symbols
2. No HIGH/CRITICAL risk warnings were ignored
3. `gitnexus_detect_changes()` confirms changes match expected scope
4. All d=1 (WILL BREAK) dependents were updated

## Keeping the Index Fresh

After committing code changes, the GitNexus index becomes stale. Re-run analyze to update it:

```bash
npx gitnexus analyze
```

If the index previously included embeddings, preserve them by adding `--embeddings`:

```bash
npx gitnexus analyze --embeddings
```

To check whether embeddings exist, inspect `.gitnexus/meta.json` — the `stats.embeddings` field shows the count (0 means no embeddings). **Running analyze without `--embeddings` will delete any previously generated embeddings.**

> Claude Code users: A PostToolUse hook handles this automatically after `git commit` and `git merge`.

## CLI

| Task | Read this skill file |
|------|---------------------|
| Understand architecture / "How does X work?" | `.claude/skills/gitnexus/gitnexus-exploring/SKILL.md` |
| Blast radius / "What breaks if I change X?" | `.claude/skills/gitnexus/gitnexus-impact-analysis/SKILL.md` |
| Trace bugs / "Why is X failing?" | `.claude/skills/gitnexus/gitnexus-debugging/SKILL.md` |
| Rename / extract / split / refactor | `.claude/skills/gitnexus/gitnexus-refactoring/SKILL.md` |
| Tools, resources, schema reference | `.claude/skills/gitnexus/gitnexus-guide/SKILL.md` |
| Index, status, clean, wiki CLI commands | `.claude/skills/gitnexus/gitnexus-cli/SKILL.md` |

<!-- gitnexus:end -->

## Mandatory Code Documentation

- Every generated or changed named production function/method/component/hook must have an adjacent JSDoc block in JavaScript/TypeScript or a Python docstring immediately inside its body. Document classes and module/public contracts where they own behavior.
- Explain the function's purpose and relevant inputs, output, side effects, errors, authorization, ordering, concurrency, bounds and deletion/provenance invariants. Use @param/@returns/@throws when useful; keep TypeScript types in the signature instead of repeating them in prose. Python docstrings must identify the function's actual contract, not merely restate its name.
- For other authored production languages, use their native documentation format for named functions (for example PowerShell comment-based help) and focused inline rationale. Keep test-only helpers and scripts deferred with the test stage.
- Add focused inline comments at non-obvious branches, transactions, locking/retry/idempotency paths, parsing/normalization and UI state transitions. Explain why the rule exists. Do not add noise that narrates each obvious statement or invent guarantees the implementation does not provide.
- Cover existing generated production code as it is brought into compliance. Preserve existing correct comments and OSS notices. Do not edit vendored dependencies, generated framework artifacts or lockfiles solely for comments. Keep test edits deferred under the implementation-stage rule; future generated tests follow this documentation convention.
- Comment-only work must preserve application logic, public signatures and schema operations. Historical migration documentation may describe operations; it must not change revision IDs, ancestry or database behavior.
- Include documentation compliance in each agent handoff and source review. GitNexus impact/change-scope rules and code/build-only validation still apply.
- Update status/checkpoint files as work proceeds, but commit status documentation only with a large completed task or phase; do not make a standalone docs commit for every progress update.
