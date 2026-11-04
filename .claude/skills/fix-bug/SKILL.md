---
name: fix-bug
description: Diagnose and fix Umwelt-OS bugs starting from pasted logs (Python tracebacks, FastAPI error JSON, X-Request-ID, docker compose logs, browser console, Playwright/pytest/vitest output) or from the owner's plain-language description, in English or Vietnamese ("bấm lưu không thấy gì", "gadget trống", "lỗi 400 khi tạo nguồn"). Finds the root cause with evidence, reports it with a proposed fix, waits for approval, then fixes with a regression test. Use this whenever the user pastes an error or log, says something is broken, failing, wrong, slow, blank or stuck, reports a failing test, or asks "tại sao", "sửa lỗi", "fix bug", "bị lỗi", even if they do not say "debug".
---

# Fix bug (Umwelt-OS)

Turn a symptom into a proven root cause, get the owner's go-ahead, then make the smallest correct fix.

Two phases with a hard stop between them:

1. **Diagnose** (read-only on the repo): understand, gather evidence, reproduce, locate, explain.
2. **Fix** (only after the owner approves the diagnosis): regression test, fix, verify.

The stop exists because the owner wants to judge the cause before code changes, and because a confident wrong fix in this codebase (workspace scoping, generation fences, deletion invariants) is expensive. In phase 1 you may run read-only commands, existing tests and throwaway scripts in the session scratchpad. Do not edit repository files.

Reply in the owner's language (Vietnamese if they wrote Vietnamese). Keep code identifiers, paths and error text verbatim.

## Phase 1: Diagnose

### 1. Read the input into a symptom card

Write down, briefly, before searching code:

- **Symptom**: what happened, quoted exactly (error text, status code, what the screen showed).
- **Where**: surface (Dashboard / Chat / Settings / a datasource / worker job / test name), route or URL, browser vs API vs worker.
- **Trigger**: the action or input that leads to it; always or sometimes.
- **Expected**: what should have happened.
- **Anchors**: grep-able strings — exception class, message, `request_id`, endpoint path, error `code`, i18n text the user saw, test id.

Reading logs:

- Python traceback: the *last frame in our code* (`core/`, `modules/`, `apps/`) is the throw site, not necessarily the cause. Note the frames above it — they show which caller supplied the bad state.
- FastAPI/Pydantic 400/422: the `loc` and `type` fields (e.g. `string_too_short` at `body.news_query`) name the exact field and validator. Find who sends that value, not only who validates it.
- `X-Request-ID` / `request_id`: correlate across services with `docker compose logs --since 30m api worker chat-worker model-worker 2>&1 | grep <id>`.
- Browser console / Next.js: map the component name or i18n string back with grep in `apps/web/src`.
- Test failure: read the assertion diff and the first error, not the last; rerun that single test to see if it is deterministic.

Plain-language reports are often vague ("không thấy gì", "bị đơ"). Translate them into a concrete candidate: which screen, which request, what state. Use the UI text the user mentions to grep for the component. If the description fits several flows and you cannot narrow it with code or logs, ask **one** focused question (which screen? what did you click? any error toast?) — but try the code first; most of the time it narrows itself.

### 2. Gather evidence before theorizing

Pick the cheapest source that can confirm or kill a hypothesis:

| Need | Command (read-only) |
|------|---------------------|
| Service logs | `docker compose logs --since 30m <api\|worker\|chat-worker\|model-worker\|web>` |
| Recent changes (regressions) | `git log --oneline -15`, `git diff` (this repo often has large uncommitted work), `git log -S'<symbol>' --oneline` |
| Find code by concept | `gitnexus_query({query: "<symptom>"})`, then `gitnexus_context({name})` — if GitNexus MCP is unavailable, fall back to Grep and say so |
| Call the API | `curl -s -i http://localhost:3000/api/v1/...` (dev stack; needs session cookie for authed routes) |
| Upstream provider | `curl -s -o /dev/null -w '%{http_code}' <provider URL>` |
| Existing tests | `uv run pytest -q <path>::<test>`, `cd apps/web && npx vitest run <file>` |

Docker-harness e2e (`./scripts/dev.ps1 test`), seed, reset and live keyed-provider calls need owner approval — don't run them in this phase.

Separate **app bug** from **upstream/environment** early. Datasources here hit dozens of free public APIs (Google RSS, GDACS, CoinGecko, arXiv, VietnamWorks…). A 404/429/timeout from the provider, a Windows network error (`ERR_NO_BUFFER_SPACE`), or a missing env var is not a code bug; prove it with a direct probe and say so. The fix may still be in our code (better error, retry, skip), but name the real cause.

### 3. Locate the root cause

Trace from symptom to cause end to end; do not stop at the first suspicious line.

- Follow data backwards: where was the bad value produced, and why did nothing catch it?
- Respect the module layout: frontend in `apps/web/src/{modules,core,components}`, backend modules in `modules/<capability>/` (public contract in `public.py`), shared infra in `core/`. Cross-module calls go through `public.py`.
- Before settling on a function, list all its callers (`gitnexus_context` or grep). If siblings share the flaw, the root cause is in the shared function, and the fix belongs there — patching only the path the report names leaves the siblings broken.
- Check what changed: if the behavior used to work, find the commit or uncommitted diff that broke it.

Confirm with a reproduction when it is cheap: an existing test that fails, a scratchpad script, a curl call, or a short pytest/vitest file in the scratchpad. A cause you reproduced beats a cause you inferred. If you cannot reproduce, say so and say what evidence the conclusion rests on.

For a hard multi-module trace, you may delegate the investigation to the `analyst` agent (read-only, Opus) with the symptom card and what you've ruled out.

### 4. Report and stop

Use this structure (headings in the owner's language):

```
## Triệu chứng
<one or two lines, exact error/behavior>

## Root cause
<file:line> — <what is wrong, in one or two sentences>

## Bằng chứng
1. <log line / test output / code fact> → <what it shows>
2. ...
(the chain from symptom to cause; each step verifiable)

## Đề xuất sửa
<where and what to change; why there and not at the symptom site>
Ảnh hưởng: <callers/flows touched; gitnexus_impact risk if available>
Test hồi quy: <test file and what it asserts>

## Độ tin cậy
<Cao / Trung bình / Thấp> — <reproduced? what is still unverified?>
```

If the cause is upstream or environmental, say that in **Root cause** and make **Đề xuất sửa** about handling it (or "no code change").

Then stop and ask the owner to approve, adjust or reject. Do not start editing.

## Phase 2: Fix (after approval)

1. **Impact first.** Run `gitnexus_impact({target, direction: "upstream"})` for each symbol you will edit and report the blast radius. If risk is HIGH or CRITICAL, warn and confirm before continuing. (Project rule in `AGENTS.md`.)
2. **Regression test that fails first.** pytest (`tests/` beside the module or `tests/integration/`, `test_*.py`) for backend, Vitest (`*.test.ts(x)` next to the component) for frontend. Run it and see it fail for the reason in the diagnosis. If the bug is only reproducible through Playwright, write the spec but ask before running the Docker harness.
3. **Fix at the root cause** identified in phase 1. Smallest correct change; no unrelated refactors. Every changed named function/component needs its JSDoc or docstring per `AGENTS.md` (explain the rule, not the statement). UI changes follow the `umwelt-os-ui-system` skill (semantic tokens, i18n for both `en-us` and `vi-vi`).
4. **Verify.** Run the new test, then the smallest relevant set: the module's tests, `npm run typecheck` / `npx vitest run <dir>` for web, `uv run pytest -q <dir>` and `uv run ruff check <files>` for Python. Report real results, including failures.
5. **Scope check.** `gitnexus_detect_changes()` (or `git diff --stat` if unavailable) to confirm only expected files changed.
6. **Don't commit.** Summarize: cause, change (file:line), test added, checks run with results, anything unverified (e.g. "not checked in the browser", "e2e not run — needs approval"). The owner decides on commit.

If the fix turns out different from the approved proposal (the cause was wrong, or the fix grows beyond the plan), stop and report again rather than pushing through.
