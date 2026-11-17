# Umwelt-OS Phase 0 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver a Docker Compose application that starts from an empty database, safely creates exactly one owner, supports login/logout, and exposes truthful infrastructure status on the 2-core/8 GB deployment target.

**Architecture:** Keep FastAPI and ARQ entry points thin, with shared infrastructure and authentication in `core/`. Next.js serves a same-origin frontend/API path; PostgreSQL stores identity/session state and Redis supports worker health and authentication throttling. OmniRoute remains a configurable external or local gateway, without inference required for setup/login.

**Tech Stack:** Python, FastAPI, Pydantic v2, SQLAlchemy 2, Alembic, asyncpg, Argon2id, Redis/ARQ, PostgreSQL/pgvector, Next.js/React/TypeScript, Tailwind/shadcn, TanStack Query, React Hook Form/Zod, pytest, Playwright, Docker Compose, GitHub Actions.

**Spec:** `specs/personal-intelligence-os-spec-v2.md`, especially sections 72–94, Phase 0 of section 95, 118–120, 134–135, 139–143, and 156–163. Architecture approved by the owner on 2026-09-25. Native execution was authorized on 2026-09-25; implementation evidence is recorded in `.superpowers/sdd/2026-09-25-bbd-os-phase-0/progress.md`.

## Global Constraints

- "Support exactly one owner profile."
- "Never hardcode secrets."
- "Every schema change must use Alembic."
- "Agents must use defined tools and APIs."
- "Do not introduce a Go backend or a second application language for backend services without measured need and a separate decision."
- "Create directories only when used."
- "Public APIs use /api/v1; earlier unversioned resource examples are shorthand, except /health."
- Target: 2 CPU cores, 8 GB RAM, SSD; no local AI inference required.
- Four-space Python indentation; two-space TypeScript indentation. Ruff/mypy, ESLint/TypeScript, pytest and Playwright remain required gates.
- Preserve existing untracked documentation and AGENTS.md. Do not commit, push, reset, delete data, or change branches without explicit user direction. Task completion means reviewable changes, not an automatic commit.
- At execution, verify compatible maintained dependency releases using official sources, lock exact resolved versions, and pin container images. This plan does not claim unverified version compatibility.

## Scope and Completion Boundary

This is the first implementation plan, not a reduction of the full product scope. No ingestion/agent/graph implementation, n8n workflows, crawler, full settings editor, or fake Today dashboard belongs in Phase 0. Add those in their owning phases. Install a dependency when the phase actually uses it, rather than starting every future service immediately.

Phase 0 delivers production/dev Compose, real authentication, migrations, worker readiness, accessible setup/login/system UI, commands, CI, and operational documentation. Source/model onboarding shows honest unconfigured status; it does not pretend to index or call a provider.

`make seed`, `make reset`, `make backup`, and `make restore` belong to later data/operations deliverables and must be tracked as incomplete, not implemented as successful no-ops. The Phase 0 README must distinguish available commands from the full specification's future command interface.

## Review Focus

1. Simultaneous first-run requests must create exactly one owner and cannot allow an unauthenticated LAN visitor to claim the instance: Task 2 tests setup token and database uniqueness.
2. Cross-origin login/logout requests and stolen pre-login cookies must not bypass CSRF or reuse a session: Task 3 tests origin/token/session rotation.
3. Container restart, Redis outage, or expired sessions must not silently grant access or report dependencies healthy: Tasks 3–4 test failures and expiry.
4. Windows development, container DNS, and reverse-proxy cookies must work without Unix-only assumptions or browser access to internal service names: Tasks 1 and 5–6 test documented commands and real browser flows.
5. Missing OmniRoute credentials and unavailable future graph services must not break setup or masquerade as validated integrations: Tasks 4–5 test explicit unconfigured/not-installed states.

## Planned Files and Ownership

| Files | Responsibility |
| --- | --- |
| `pyproject.toml`, `uv.lock`, `package.json`, `package-lock.json` | Python and npm workspaces with reproducible dependency resolution |
| `.gitignore`, `.dockerignore`, `.env.example`, `Makefile`, `scripts/dev.ps1` | Ignore secrets/artifacts, configuration examples, equivalent development commands |
| `core/config.py`, `core/database.py`, `core/errors.py` | Validated settings, async DB sessions, safe errors and request IDs |
| `core/auth/models.py`, `schemas.py`, `service.py`, `routes.py`, `dependencies.py` | Single-owner setup, sessions, CSRF, throttling and public auth contracts |
| `alembic.ini`, `infrastructure/postgres/migrations/env.py`, `versions/0001_auth.py` | Empty-database migration and identity/session schema |
| `apps/api/main.py`, `core/system/routes.py`, `core/system/health.py` | App composition, liveness and authenticated dependency status |
| `apps/worker/main.py` | ARQ settings and health integration; one concurrent job initially |
| `apps/web/src/app/layout.tsx`, `page.tsx`, `setup/page.tsx`, `login/page.tsx`, `globals.css` | Real setup/login and authenticated system landing screen |
| `apps/web/src/core/api.ts`, `query-provider.tsx`, `auth.tsx` | Same-origin client, CSRF handling, server-state provider and auth UI |
| `apps/web/src/components/ui/button.tsx`, `input.tsx`, `label.tsx` | Only shadcn primitives actually used |
| `apps/web/package.json`, `tsconfig.json`, `next.config.ts`, `eslint.config.mjs`, `postcss.config.mjs`, `next-env.d.ts`, `components.json` | Framework/tool configuration |
| `infrastructure/docker/api.Dockerfile`, `web.Dockerfile`, `docker-compose.yml`, `docker-compose.dev.yml`, `docker-compose.test.yml` | Deploy, local development and disposable integration stack |
| `tests/conftest.py`, `tests/integration/test_auth_race.py`, `tests/test_system_health.py`, `tests/test_config.py` | Backend checks, including a real PostgreSQL owner-setup race |
| `playwright.config.ts`, `tests/e2e/auth.spec.ts`, `tests/e2e/system.spec.ts` | Browser acceptance and keyboard checks |
| `.github/workflows/ci.yml`, `README.md`, `docs/development.md`, `docs/deployment.md`, `docs/privacy.md` | Quality gates and runnable documentation |
| Existing status/decision documents and `AGENTS.md` | Accurate phase progress, decisions, and available commands |

Add Python package markers only where required. Do not create empty feature directories or shared packages without a consumer. `apps/api` composes core routers; no parallel domain hierarchy under `services/`.

## Task 1: Runnable API, Configuration, and Reproducible Tooling

**Files:** Create root manifests/lockfiles, ignore files, `.env.example`, `core/config.py`, `core/errors.py`, `apps/api/main.py`, `tests/test_config.py`, initial `apps/web` framework configuration. Build Make/PowerShell command targets as their underlying tasks become available.

**Interfaces:**
- `Settings` contains database/Redis URLs, data directory, public origin, secure-cookie toggle, setup token, CSRF signing secret, session lifetime, and optional OmniRoute base URL/key. Use server-only environment settings; secret types must mask repr/log output.
- `create_app(settings: Settings | None = None) -> FastAPI`; unauthenticated `GET /health` returns `{"status":"ok"}` for process liveness only.
- All errors use `{error:{code,message,details,requestId}}`; validation failures return 422 with sanitized details and never echo secret values.

- [ ] Select and lock compatible versions, including supported Python/Node runtimes. Check Docker daemon availability and target Linux image architecture; do not install dependencies globally or infer successful runtime availability from command discovery.
- [ ] Add configuration tests and run `uv run pytest tests/test_config.py -q`; observe the missing implementation failure first.

```python
def test_invalid_public_origin_is_rejected():
    import pytest
    from pydantic import ValidationError
    from core.config import Settings

    with pytest.raises(ValidationError):
        Settings(public_origin="not-a-url")

def test_gateway_key_is_redacted():
    from core.config import Settings

    settings = Settings(omniroute_api_key="test-secret-only")
    assert "test-secret-only" not in repr(settings)
```

- [ ] Implement settings with Pydantic Settings, development defaults for service addresses and no usable default secrets. `.env.example` uses explicit replace-me placeholders; `make setup`/PowerShell setup generate local secrets only when absent and never overwrite an existing `.env`.
- [ ] Create the API factory and request-ID/error handling. Use `logging`/structlog without request-body or credential logging. Keep `/health` independent of network dependencies.

```python
from fastapi import FastAPI
from core.config import Settings

def create_app(settings: Settings | None = None) -> FastAPI:
    app = FastAPI(title="Umwelt-OS")
    app.state.settings = settings or Settings()

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app
```

- [ ] Configure Ruff/mypy, ESLint/tsc, workspace build scripts, and Python packaging so API/worker imports work from installed packages in containers. Verify the config tests and liveness endpoint; do not add stub feature routes.

## Task 2: Database Migration and Exclusive Owner Setup

**Files:** Create `core/database.py`, auth models/schemas/service/routes, migration configuration/revision, `tests/conftest.py`, `tests/integration/test_auth.py`; extend `apps/api/main.py`.

**Interfaces:**
- `GET /api/v1/auth/setup-status -> {setupRequired:boolean}` reveals no owner metadata.
- `POST /api/v1/auth/setup` accepts `{password:string}` and `X-Setup-Token`; success 201 `{created:true}`, wrong/missing token 403, existing owner 409. Setup does not implicitly log in.
- Owner table: singleton integer PK constrained to 1, Argon2id password hash, UTC creation time. Session table: hashed random token PK, owner FK, hashed CSRF token, creation/expiry UTC timestamps. No raw password/session token persistence.
- Password length 12–128 characters, no truncation; setup token compared in constant time. Require matching configured Origin for browser mutations.

- [ ] Build disposable test fixtures: `client` is an httpx AsyncClient using `create_app(test_settings)`; `test_settings` points only to the test PostgreSQL/Redis databases; migrate once and reset identity tables between tests. `test_settings.setup_token` is generated test-only input. Set the AnyIO backend fixture to `asyncio` for asyncpg/Redis. Never truncate a non-test database.
- [ ] Add the failing race test, then run `uv run pytest tests/integration/test_auth.py -q` against test services.

```python
import asyncio
import pytest

@pytest.mark.anyio
async def test_setup_has_one_winner(client, test_settings):
    headers = {"X-Setup-Token": test_settings.setup_token.get_secret_value(),
               "Origin": str(test_settings.public_origin).rstrip("/")}
    async def setup():
        return await client.post("/api/v1/auth/setup", headers=headers,
                                 json={"password": "test-owner-password-42"})
    responses = await asyncio.gather(setup(), setup())
    assert sorted(r.status_code for r in responses) == [201, 409]
```

- [ ] Implement the schema with database uniqueness, not check-then-insert alone. The essential constraint is:

```sql
CREATE TABLE owner (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    password_hash TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

- [ ] Use Argon2id from a maintained password library; bound simultaneous hash work and benchmark its parameters on the constrained target. Convert only the singleton conflict to 409; propagate unexpected DB failures through a safe 503/500 response.
- [ ] Add invalid-token/password and second-setup checks to the same integration file. Verify hashed storage, migration from empty DB, and repeated `alembic upgrade head`. Extend auth migration with session fields consumed by Task 3, never runtime `create_all`.

## Task 3: Sessions, CSRF, Logout, and Authentication Throttling

**Files:** Extend auth schemas/service/routes; create `core/auth/dependencies.py`; extend integration fixtures/tests.

**Interfaces:**
- `GET /api/v1/auth/csrf -> {csrfToken:string}` issues a bounded pre-auth token tied to an HTTPOnly SameSite cookie; use a maintained signing primitive and a setup-generated server secret, with a short expiry.
- `POST /api/v1/auth/login {password}` requires matching Origin and `X-CSRF-Token`; returns 200 `{authenticated:true,csrfToken:string}` and a new opaque HTTPOnly SameSite=Lax session cookie. Rotate away from pre-auth state on login.
- `GET /api/v1/auth/session -> {authenticated:true,csrfToken:string}` for a valid session; otherwise 401. No-store for auth responses.
- `POST /api/v1/auth/logout` requires session/CSRF/origin, deletes the server-side session and expires its cookie; 204.
- `require_owner` validates the hashed opaque token and expiry from PostgreSQL; no caller controls an owner ID. Mutating protected endpoints also require CSRF and matching Origin.

- [ ] Define fixture `authenticated_client`: first create the owner through the setup endpoint using the test token/password, then obtain pre-auth CSRF, login, keep cookies, and expose the returned `csrfToken` to tests. Add failure/expiry cases before implementation. Each test gets reset identity state; do not depend on a previous test creating the owner.

```python
@pytest.mark.anyio
async def test_logout_rejects_cross_origin(authenticated_client):
    client, csrf = authenticated_client
    response = await client.post("/api/v1/auth/logout", headers={
        "Origin": "https://untrusted.example", "X-CSRF-Token": csrf,
    })
    assert response.status_code == 403
    assert (await client.get("/api/v1/auth/session")).status_code == 200
```

- [ ] Generate tokens with `secrets.token_urlsafe(32)`, store SHA-256 digests, validate CSRF with constant-time comparison. Default session absolute expiry to 24 hours with configurable shorter duration. Set Path=/, HTTPOnly, SameSite=Lax, and configurable Secure; use Secure in HTTPS deployment.
- [ ] Rate-limit setup/login by a bounded Redis counter with expiry: initial policy five password attempts per minute per trusted client address plus a bounded instance-wide limit. Do not trust arbitrary forwarded headers. Return 429 with Retry-After; return 503 for login/setup if the limiter is unavailable. Existing valid sessions do not depend on Redis. Use generic invalid-login errors.
- [ ] Run tests for wrong password, missing/wrong CSRF, pre-auth expiry, old-token reuse, session expiry/restart persistence, missing Origin, and successful logout. Test cookie flags and confirm password/API key values never appear in error bodies.

## Task 4: Worker and Truthful System Status

**Files:** Create `apps/worker/main.py`, `core/system/health.py`, `core/system/routes.py`, `tests/integration/test_system.py`; compose routes in `apps/api/main.py`.

**Interfaces:**
- `GET /api/v1/system/health`, owner-only, reports `overall: healthy|degraded` and components with `status: healthy|unavailable|unconfigured|not_installed`.
- Check PostgreSQL, Redis, and ARQ worker heartbeat with bounded timeouts. OmniRoute reports configuration presence only (`connectivity: not_tested`); a configured URL is not evidence of healthy inference. Graph/n8n/browser components report not_installed until implemented/enabled.
- API readiness depends on PostgreSQL; UI status can be degraded with a missing worker or gateway. Public liveness reveals no dependency topology or secrets.

- [ ] Add tests before routes, including no-credential startup:

```python
@pytest.mark.anyio
async def test_gateway_absence_is_explicit(authenticated_client):
    client, _ = authenticated_client
    response = await client.get("/api/v1/system/health")
    assert response.status_code == 200
    assert response.json()["components"]["model_gateway"]["status"] == "unconfigured"
    assert (await client.get("/health")).json() == {"status": "ok"}
```

- [ ] Configure ARQ using its supported WorkerSettings/health-check mechanism with `max_jobs = 1`. Register one real hourly maintenance job, `purge_expired_sessions(ctx) -> int`, which deletes at most 1000 expired sessions per execution using the database clock. This gives the initial worker useful bounded work without adding speculative ingestion/agent functions. Run it through ARQ's cron facility; emit no log for zero deleted rows.

```sql
DELETE FROM auth_session
WHERE token_hash IN (
    SELECT token_hash FROM auth_session
    WHERE expires_at <= now()
    ORDER BY expires_at
    LIMIT 1000
);
```

Name the Task 2 session table `auth_session`, with `token_hash` as its hashed-token primary key and an index on `expires_at`. Add a test that inserts expired and valid sessions, invokes the job, and asserts only expired rows disappear; repeat invocation must return zero.
- [ ] Test heartbeat missing/stale, Redis down, PostgreSQL timeout, and unauthenticated system access. Start/stop only disposable test services for failure tests, then restore them. Never equate a running container with a healthy worker.

## Task 5: Setup, Login, and Authenticated Frontend

**Files:** Create the planned Next.js pages, shared UI/client files, shadcn primitives, and `tests/e2e/auth.spec.ts`, `tests/e2e/system.spec.ts`, `playwright.config.ts`.

**Interfaces:** Consume Tasks 2–4 exactly. Browser requests use `/api/v1/...` on the same origin; server-only Next configuration proxies to the internal API URL. Forward cookies/Origin safely and do not export container hostnames or secrets through NEXT_PUBLIC variables.

- [ ] Add the failing Playwright setup/login test against a fresh disposable stack. In the test environment, `E2E_SETUP_TOKEN` matches only the disposable instance token.

```typescript
import { test, expect } from '@playwright/test';

test('owner can set up, log in, and log out', async ({ page }) => {
  await page.goto('/');
  await page.getByLabel('Setup token').fill(process.env.E2E_SETUP_TOKEN!);
  await page.getByLabel('Password', { exact: true }).fill('test-owner-password-42');
  await page.getByLabel('Confirm password').fill('test-owner-password-42');
  await page.getByRole('button', { name: 'Create owner' }).click();
  await expect(page).toHaveURL(/\/login$/);
  await page.getByLabel('Password', { exact: true }).fill('test-owner-password-42');
  await page.getByRole('button', { name: 'Sign in' }).click();
  await expect(page.getByRole('heading', { name: 'System status' })).toBeVisible();
  await page.getByRole('button', { name: 'Sign out' }).click();
  await expect(page).toHaveURL(/\/login$/);
});
```

- [ ] Implement forms with React Hook Form/Zod, accessible labels, password autocomplete values, visible focus, pending/error states, and neutral responsive styling. Obtain pre-auth CSRF before login; retain auth state through TanStack Query, never localStorage session tokens. Clear cached private responses on logout/401.
- [ ] Root route checks setup/session status and displays real system state after login. Do not expose navigation links to unimplemented features. State clearly that OmniRoute is unconfigured/not tested; do not display fictional tasks or graph counts.
- [ ] Use the browser to verify keyboard submission, validation focus, mobile-width layout, dark/light contrast, expired-session redirect, and backend-offline errors. Add Playwright checks for accessible error text and unavailable gateway; capture actual UI screenshots for documentation.

## Task 6: Compose, Developer Commands, and CI

**Files:** Create Dockerfiles/Compose files, Makefile, `scripts/dev.ps1`, `.github/workflows/ci.yml`; update manifests/scripts and README.

**Interfaces:**
- Production Compose services: `postgres`, `redis`, `migrate` (one-shot), `api`, `worker`, `web`. Use pgvector PostgreSQL image. Only web binds a host port, initially loopback; documented HTTPS reverse proxy enables remote access.
- API/worker start after successful migration; migration failure prevents readiness. Store DB/Redis data in named volumes. Run app containers as non-root. Secrets come from environment/config, not baked images.
- `docker-compose.dev.yml` adds source mounts/hot reload; production never runs dev servers. `docker-compose.test.yml` isolates credentials, volumes, ports and database from personal data.

- [ ] Build multi-stage production images and Compose dependency/health checks; use exec-form commands, e.g. `CMD ["uvicorn", "apps.api.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]`. Allow `create_app` to load settings when no argument is supplied while retaining test injection.
- [ ] Implement command parity with the following actual commands, delegated from Make and PowerShell rather than duplicating logic:

```text
setup      uv sync --frozen; npm ci; generate missing local config securely
dev        docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d --build
stop       docker compose -f docker-compose.yml -f docker-compose.dev.yml stop
migrate    docker compose run --rm migrate
lint       uv run ruff check .; npm run lint --workspace apps/web
typecheck  uv run mypy core apps/api apps/worker; npm run typecheck --workspace apps/web
test       uv run pytest; npm run test:e2e (against disposable services)
build      npm run build --workspace apps/web; docker compose build
```

The command list describes sequences, not literal shell concatenation. Each runner stops on the first failure and returns nonzero. `test` owns disposable service startup/migrations/readiness and cleanup; pytest fixtures use only its test URLs. Reset the disposable DB before Playwright. Configure a single-worker `bootstrap` Playwright project for `auth.spec.ts`; make the `system` project depend on `bootstrap` and log in afresh using that disposable owner. This is an explicit project dependency, not test-file ordering. Retries of bootstrap must reset its disposable instance before setup.

- [ ] On Windows run `./scripts/dev.ps1 setup`, `lint`, `typecheck`, `test`, and `build`. On CI/Linux run the matching Make commands. Test that a failed subcommand is not hidden by a later success. No automatic deletion of user volumes.
- [ ] CI checks frozen installs, Ruff/mypy, ESLint/tsc, PostgreSQL/Redis integration, production frontend build, Docker builds, and Playwright flows against production containers. Save failure screenshots/logs with secret redaction.
- [ ] Validate fresh install, repeat migrations, API/worker restart, login persistence and worker recovery on an isolated project. Test production proxy/cookie handling at the real browser origin. Record actual image versions and measurements, not assumed RAM estimates.

## Task 7: Documentation, Acceptance, and Handoff

**Files:** Create/update README, development/deployment/privacy docs, AGENTS.md, architecture decisions, and implementation status. No unrelated edits.

- [x] Document prerequisites, Windows and Make commands, initial setup token retrieval without putting it in logs, HTTP loopback versus HTTPS deployment, cookie/origin configuration, named volumes, and troubleshooting migration/Redis/worker failure.
- [x] Explain that Phase 0 has no ingestion/model calls yet. Document OmniRoute config placeholders and data-egress policy without suggesting provider credentials are already validated. Keep graph/n8n/browser installation in their owning phases.
- [x] Run the complete acceptance commands once after final changes; capture results:

```text
make lint
make typecheck
make test
make build
docker compose -p bbd-os-phase0-acceptance -f docker-compose.yml -f docker-compose.test.yml up -d
```

Use PowerShell command parity when Make is absent. Verify this disposable project's volume/port/config isolation before starting it. Preserve logs and clean up only its known resources. Never run `down -v` against the owner deployment.

- [!] Record CPU/RAM and latency on the target 2-core/8-GB mini PC. The available Docker host has 14 CPUs and 31 GiB RAM, so target capacity and latency are unverified.
- [x] Self-review interfaces and auth failure cases; inspect the browser acceptance results. Update status with evidence and remaining phases. No commit was created.

## Coverage and Deferred Phase Map

| Specification requirement | This plan / owning phase |
| --- | --- |
| Phase 0 monorepo, tooling, Docker, auth, migrations, health, CI | Tasks 1–7 |
| Single owner, Argon2id, session/CSRF and safe errors | Tasks 2–3 |
| Responsive, keyboard-friendly loading/error UI | Task 5 |
| OmniRoute replaces LiteLLM, no configured AI required for setup | Tasks 1, 4–5; live inference in search/Ask phases |
| Module isolation | Phase 0 infrastructure in core; real domain descriptors with Phase 1+ consumers, no unused registry scaffolding |
| Canonical domain models, seed data | Phase 1 |
| n8n connectors, cursor safety, durable ingestion/ARQ recovery, Crawlee | Phase 2; GitHub completeness in Phase 9 |
| Embedding privacy, pinned indexes, model capability probes | Phase 3 and Phase 6 |
| Entities, Graphiti/backend compatibility and resource validation | Phases 4–5 |
| LangGraph checkpoints, permissions, approval/resume and browser-use tools | Phases 6–7 |
| Today, tasks/goals, daily brief | Phase 8 |
| Automation, observability, retention, backup/restore, full resource acceptance | Phases 10–12 |

Phase 0 does not change the remaining acceptance requirements. Each subsequent subsystem gets a reviewable implementation plan based on the approved master design and the code that exists at that time.

## Execution Record

Phase 0 implementation is complete for the defined scope. Final local evidence is recorded in `docs/IMPLEMENTATION_STATUS.md` and `.superpowers/sdd/2026-09-25-bbd-os-phase-0/progress.md`. An independent review found no remaining actionable issues. The target mini PC measurement remains open for Phase 12 capacity validation; phases 1–12 remain unimplemented. No commit or staging was performed.
