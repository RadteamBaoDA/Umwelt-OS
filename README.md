# Umwelt-OS

Umwelt-OS is a self-hosted Personal Intelligence OS. Phase 0 provides the private owner account, service status, database migrations, a small background worker, and the local deployment foundation. It does not collect sources, call AI models, or run graph, n8n, or browser services yet.

## Start locally

Requirements: Docker Compose v2, Python 3.12, uv, Node.js 24, and npm. On Windows, run commands from PowerShell:

```powershell
./scripts/dev.ps1 setup
./scripts/dev.ps1 dev
```

On macOS/Linux, use `make setup` and `make dev`. Setup creates `.env` once with random setup, CSRF, and database secrets; it does not print their values or replace an existing file. Open `.env` locally and copy `SETUP_TOKEN` into the first-run setup form at [http://localhost:3000](http://localhost:3000). Keep `.env` private.

The web service binds to `127.0.0.1` only. For remote use, place it behind an HTTPS reverse proxy, set `PUBLIC_ORIGIN` to the public origin and `SECURE_COOKIES=true`, then restart the stack. Do not expose the API, Redis, or PostgreSQL ports directly.

## Commands

Both `scripts/dev.ps1` and Make provide `setup`, `dev`, `stop`, `migrate`, `seed`, `lint`, `typecheck`, `test`, and `build`. Run `./scripts/dev.ps1 seed` (or `make seed`) to explicitly add three fictional Phase 1 demo records: a manual source and two notes. The command reports created/existing counts and fails visibly if migration or database access fails. It never runs at startup. Repeating it preserves edits and deletions within the demo source; an archived demo source remains archived. Source deletion with data archives the source and deletes its notes, and later seed commands leave that archived source alone. `test` starts a uniquely named disposable Compose project on temporary loopback ports, runs backend, real-PostgreSQL race, and browser checks, then removes only that project's containers and volumes. It never touches the normal `bbd-os` data volumes. `reset`, `backup`, and `restore` are planned for later phases and are not implemented.

During production implementation, CI runs dependency installation and `make build` only. After all original and reconciliation production tasks are complete, manually dispatch the CI workflow with **Run deferred validation** enabled to run lint, typecheck, and tests.

PostgreSQL migrations run before API and worker startup. Their persistent data uses named volumes. `docker compose logs api migrate worker` shows service diagnostics; do not share logs with `.env` values. If startup is blocked, inspect `docker compose ps` and the migration service first.

## OmniRoute and privacy

`OMNIROUTE_BASE_URL` and `OMNIROUTE_API_KEY` are optional placeholders. Phase 0 reports whether the gateway is configured but does not verify connectivity or send prompts. Provider/model calls and source ingestion are deferred to later phases. Review [privacy](docs/privacy.md) and [deployment](docs/deployment.md) before enabling remote access or adding connectors.

See [development](docs/development.md), [deployment](docs/deployment.md), [privacy](docs/privacy.md), and [implementation status](docs/IMPLEMENTATION_STATUS.md).

Optional Google sign-in uses server-side OAuth credentials. The account menu and its Google-link dialog use locally owned Radix-based shadcn source under `apps/web/src/components/ui`; run `npm ci` and `uv sync` to install the locked frontend and backend dependencies. See the [Google OAuth setup](docs/deployment.md#google-sign-in) and [open-source inventory](OSS_USED.md) for configuration and license details.

User display preferences (theme, interface language, and IANA time zone) are stored in the owner preferences row. The shell provides English (US) and Vietnamese UI catalogs; source and user-authored content remain unchanged.

The knowledge entity page uses React Flow for a bounded interactive graph and keeps a relationship-list fallback. See the [open-source inventory](OSS_USED.md) and [third-party license notices](THIRD_PARTY_NOTICES.md) for its locked dependencies and attribution.
