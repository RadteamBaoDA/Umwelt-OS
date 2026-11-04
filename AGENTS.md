# Repository Guidelines

## Project Structure & Module Organization

This repository implements the single-user, self-hosted Personal Intelligence OS through a modular production codebase. The approved product and engineering specification remains `specs/personal-intelligence-os-spec-v2.md`; delivery and later-phase status are tracked in `docs/IMPLEMENTATION_STATUS.md`.

Follow the specification when adding code:
- `apps/web/`: Next.js, React, and TypeScript frontend.
- `apps/api/` and `apps/worker/`: Python FastAPI backend and background processing.
- `packages/`: shared UI, contracts, SDK, and configuration.
- `core/` and `modules/<capability>/`: shared infrastructure and domain modules, following the modular-monolith requirements in sections 139–143.
- `tests/integration/`, `tests/e2e/`, and `tests/fixtures/`: cross-module validation.
- `infrastructure/` and `docs/`: deployment configuration and operational documentation.

Keep module internals private; integrate through public contracts or events. Create directories only when used.

## Build, Test, and Development Commands

Use `make setup`, `make dev`, `make stop`, `make migrate`, `make seed`, `make lint`, `make typecheck`, `make test`, and `make build` on macOS/Linux. Windows PowerShell equivalents are `./scripts/dev.ps1 <task>`. `test` creates a uniquely named disposable Compose project and removes only its own volumes. `seed` explicitly creates fictional Phase 1/8/10/12 demo data under privacy and stable-owner receipt controls. Named development workspace reset commands are implemented; their operational acceptance is separate. Backup and restore are delegated to the cloud provider and have no application commands. Follow `docs/development.md`; do not run state-changing commands (seed, reset) without owner approval.

## Coding Style & Naming Conventions

Use four-space Python indentation and two-space TypeScript indentation. Use `snake_case` for Python functions/modules, `camelCase` for TypeScript functions, and `PascalCase` for React components and types.

The specification requires ESLint and TypeScript checks for frontend code, and Ruff and mypy for Python. Keep changes focused and avoid unused scaffolding.

## UI Design System

All frontend work (`apps/web`) follows the Umwelt-OS design system. Use the `umwelt-os-ui-system` skill (`.agents/skills/umwelt-os-ui-system/SKILL.md`) for any UI component, dashboard gadget, chat surface or User settings work.

- **Rules and contracts:** `docs/DESIGN_SYSTEM.md` (component mapping, theme, i18n, charts, layout).
- **Tokens and brand book:** `docs/design-system/` — read `README.md` first (voice, color, type, spacing, states, iconography), then `tokens.json` (colors in light/dark, type styles, spacing, radii, shadows) and `components/<Name>/README.md` for each shadcn primitive.
- **Source of truth for values:** `apps/web/src/app/globals.css`. `docs/design-system/tokens.json` mirrors it; when you change a token in CSS, update `tokens.json` in the same change (and the reverse).
- **Published copy:** https://claude.ai/artifact/HsmKMkv9nMkoCrhnoZNM1X (re-publish after changing `docs/design-system/`).
- Use semantic tokens only (`bg`, `surface`, `text`, `muted`, `line`, `accent`, `on-accent`, `danger` or their Tailwind aliases); never literal colors in feature code. Compose `apps/web/src/components/ui` primitives; no parallel button/dialog/form systems.
- Use shadcn/ui components, Recharts for financial charts, light/dark/system themes, and the two locale preferences `en-us` / `vi-vi` (normalized to `en-US` / `vi-VN` for formatting). Main navigation remains Dashboard / Chat / Settings. These UI decisions govern older UI examples; they do not authorize unrelated backend changes.

## Testing Guidelines

Use pytest for backend tests, Vitest for frontend unit tests (`apps/web`), and Playwright for end-to-end flows. Run the smallest sufficient check for what changed (targeted tests, typecheck, lint, build) and report the real result, including failures and any unavailable validation. Name Python tests `test_*.py` and Playwright tests `*.spec.ts`. Cover module contracts, ingestion, permissions, search, and citation flows. No numeric coverage threshold is specified. The Docker-based integration/e2e harness and live provider calls need owner approval.

Cross-module reads use owner `public.py` DTO/query contracts. A narrowly scoped read projection may be approved when its owner, fields, permission and deletion checks are documented; callers must not use that projection for writes. Module persistence models remain private.

## Commit & Pull Request Guidelines

Use concise, imperative subjects, such as `docs: clarify ingestion contracts`. PRs should describe scope, reference relevant specification sections or issues, list validation results, and include screenshots for UI changes.

## Security & Configuration

Never commit secrets or personal data. Provide placeholder configuration in `.env.example` when configuration is introduced. Preserve source provenance and enforce agent access through supported APIs and permission checks.

<!-- gitnexus:start -->
# GitNexus — Code Intelligence

This project is indexed by GitNexus as **Umwelt-OS** (37085 symbols, 78299 relationships, 844 execution flows).

> Index stale? Run `node .gitnexus/run.cjs analyze --index-only` from the project root — it auto-selects an available runner. No `.gitnexus/run.cjs` yet? Bootstrap with `npx`, `bunx`, or `pnpm dlx` — e.g. `bunx gitnexus@latest analyze` (npm 11 npx crash; #1939).

## Always Do

- **MUST run impact before editing.** Use `impact({target: "symbolName", direction: "upstream"})` or `node .gitnexus/run.cjs impact "symbolName" --direction upstream --repo .`; report callers, processes, and risk. Never substitute grep for graph analysis.
- **MUST analyze graph changes before committing.** Use `detect_changes({scope: "all"})` (MCP) or `node .gitnexus/run.cjs detect-changes --scope all --repo .` (CLI fallback). `partial: true` or `truncated: true` is not a clean check — a zero means unseen, not unaffected; re-run it. For regression review: `detect_changes({scope: "compare", base_ref: "main"})` or `node .gitnexus/run.cjs detect-changes --scope compare --base-ref "main" --repo .`.
- MUST warn on HIGH/CRITICAL `risk` pre-edit; never use `riskSharedAxes` to waive a HIGH/CRITICAL `risk` warning. Compare File/symbol: MCP File omits axes; Graph-RAG expands File.
- **MUST treat `risk: UNKNOWN` as unresolved, not as low.** An empty caller set is not evidence the symbol is unused — it can also mean the callers are not resolvable by the index (plain-object property access, dynamic dispatch, cross-language calls). `impact` pairs `UNKNOWN` with a `riskNote` saying so. Confirm with a text search before treating the symbol as safe to change or delete; do not proceed on the strength of a zero.
- **MUST use `query({search_query: "concept"})` for concepts/flows, `context({name: "symbolName"})` for a named symbol, or `impact` for blast radius, on read-only callers, dependencies, imports, or execution flow.** Graph first; text search only for empty/`UNKNOWN`/literals.
- For security review, `explain({target: "fileOrSymbol"})` lists taint findings (source→sink flows; needs `analyze --pdg`).

## Never Do

- NEVER edit a function, class, or method before MCP/CLI impact analysis.
- NEVER ignore HIGH or CRITICAL risk warnings from impact analysis, and never read `UNKNOWN` as an all-clear — it means the walk could not answer, which is the one verdict that requires confirming by other means.
- NEVER rename symbols with find-and-replace — use `rename` which understands the call graph.
- NEVER commit before MCP/CLI graph change analysis.

## Resources

| Resource | Use for |
| --- | --- |
| `gitnexus://repo/Umwelt-OS/context` | Codebase overview, check index freshness |
| `gitnexus://repo/Umwelt-OS/clusters` | All functional areas |
| `gitnexus://repo/Umwelt-OS/processes` | All execution flows |
| `gitnexus://repo/Umwelt-OS/process/{name}` | Step-by-step execution trace |

## CLI

| Task | Read this skill file |
| --- | --- |
| Understand architecture / "How does X work?" | `.claude/skills/gitnexus-exploring/SKILL.md` |
| Blast radius / "What breaks if I change X?" | `.claude/skills/gitnexus-impact-analysis/SKILL.md` |
| Trace bugs / "Why is X failing?" | `.claude/skills/gitnexus-debugging/SKILL.md` |
| Rename / extract / split / refactor | `.claude/skills/gitnexus-refactoring/SKILL.md` |
| Tools, resources, schema reference | `.claude/skills/gitnexus-guide/SKILL.md` |
| Index, status, clean, wiki CLI commands | `.claude/skills/gitnexus-cli/SKILL.md` |

<!-- gitnexus:end -->

## Mandatory Code Documentation

- Every generated or changed named production function/method/component/hook must have an adjacent JSDoc block in JavaScript/TypeScript or a Python docstring immediately inside its body. Document classes and module/public contracts where they own behavior.
- Explain the function's purpose and relevant inputs, output, side effects, errors, authorization, ordering, concurrency, bounds and deletion/provenance invariants. Use @param/@returns/@throws when useful; keep TypeScript types in the signature instead of repeating them in prose. Python docstrings must identify the function's actual contract, not merely restate its name.
- For other authored production languages, use their native documentation format for named functions (for example PowerShell comment-based help) and focused inline rationale. Test-only helpers and scripts need no docs beyond a clear name.
- Add focused inline comments at non-obvious branches, transactions, locking/retry/idempotency paths, parsing/normalization and UI state transitions. Explain why the rule exists. Do not add noise that narrates each obvious statement or invent guarantees the implementation does not provide.
- Cover existing generated production code as it is brought into compliance. Preserve existing correct comments and OSS notices. Do not edit vendored dependencies, generated framework artifacts or lockfiles solely for comments. Generated tests follow this documentation convention.
- Comment-only work must preserve application logic, public signatures and schema operations. Historical migration documentation may describe operations; it must not change revision IDs, ancestry or database behavior.
- Include documentation compliance in each agent handoff and source review. GitNexus impact/change-scope rules still apply.
- Update status/checkpoint files as work proceeds, but commit status documentation only with a large completed task or phase; do not make a standalone docs commit for every progress update.

## Execution context after repository rename

The repository folder is `D:/Project/Umwelt-OS`. This chat can still inject the obsolete `D:/Project/BBD-OS` as its default cwd. On this host, pass the assigned checkout's actual absolute path in `workdir` on every shell tool call, using forward slashes; implementations in worktrees must use their assigned worktree path. Do not run against the injected/default BBD-OS path or create a shortcut/junction to make it work. A process-creation error267 at the old cwd does not prove the host or Docker is unavailable. Retry with the explicit assigned path before attempting unrelated worktree recovery or asking the user to reconnect.
