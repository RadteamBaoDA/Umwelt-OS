# Generative UI: verification that still needs owner approval

The parity matrix (`docs/GENUI_PARITY.md`) records what exists in code and unit tests. These checks
have **not** run. Each needs Docker, a real browser, a LAN device or a live provider, so each waits
for explicit owner approval (AGENTS.md: the Docker integration/e2e harness and live provider calls need
owner approval). Until they run, the matching rows keep a `pending:` proof.

## 1. Docker integration (Postgres, Redis)

Command (creates a disposable Compose project, removes only its own volumes):

    ./scripts/dev.ps1 test -PytestTarget tests/integration

What it must prove that mocks cannot:

| Area | Why mocks are not enough |
|---|---|
| Migrations r22 to r26 on real Postgres (`chat_todos`, `awaiting_input` CHECK + `input_request`, `viz_router`, `chat_templates` + `pending_template`, `source_response_id`) | only offline `--sql` renders were run |
| `purge_run_model_copies`, `neutralize_ui_events`, `delete_conversation` template delete | tested against a fake session, the real `UPDATE`/`DELETE` never ran |
| Template cap advisory lock (`pg_advisory_xact_lock`) | lock call asserted, concurrency not exercised |
| Park/resume of `awaiting_input` (lock release while parked, `GETDEL`-style claims, heartbeat, `recover_parked_input_runs`) | polling and row locks only simulated |
| `/api/v1/pick` single-use sessions (`GETDEL`, TTL) and `require_budget` | fake store only |
| Existing integration tests that may still assume one widget per turn (`tests/integration/test_generative_ui_worker.py`) | not run since multi-part and continuation rounds landed |

## 2. Playwright e2e (real browser)

Command:

    ./scripts/dev.ps1 test -E2eTarget tests/e2e/generative-ui.spec.ts

Specs to add or extend before running (none exist yet for these): streaming morph and 1000 ms throttle,
autosize, trip animator, theme sync into the sandbox, copy/download/fullscreen, chart export, template
library, demo gallery, form submit and retry, awaiting_input picker, `/pick` phone page, mobile viewport,
and **image display inside the sandbox** (`loadImage` returns a `data:` URL; whether the opaque-origin frame
shows it under `img-src data:` is the open question behind row T07).

## 3. Live provider run (real model)

Needs a configured provider (owner decides which; remote providers send prompts out). To prove:

1. Presentation tool calls across rounds (widget, text, widget) and the part/round caps.
2. External visualization router: classifier runs once per turn before the request, tools filtered, decision note
   honoured. Run once per adapter: generic LLM classifier (OpenRouter, a Cloudflare Workers AI model, OmniRoute)
   and TypeSafe System One. OmniRoute forwarding of `response_format` / `tool_choice` is unconfirmed until the
   Settings "Test routing" probe runs against a live gateway.
3. `request_user_input` end to end, `get_trip_stop_images` with the remote-media flag on, `set_theme`, template and
   todo tools.

## 4. Other

| Item | Needs |
|---|---|
| `/pick` QR reachable from a real phone (`lan_ip` inside Docker is probably the bridge IP) | a LAN device; may need a configurable public base URL |
| MCP client handshake against `/mcp` and stdio launch | a real MCP client |
| `make lint`, `npm run lint`, CI run with **Run deferred validation** | CI dispatch |
| `npx gitnexus analyze`, then `gitnexus_detect_changes` before commit | local index refresh (index is stale at 34e0fa8) |
| Automated security review plugin | re-login (it returned HTTP 401); until then the new attack surface was reviewed by reading code only |
