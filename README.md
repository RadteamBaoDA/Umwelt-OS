# Umwelt-OS

Umwelt-OS is a self-hosted Personal Intelligence OS for collecting world and personal data, exploring retained knowledge, and arranging a live gadget dashboard with an AI chat drawer and full Chat page. The current integrated production source includes owner/Google authentication, native connector and MCP settings, ingestion and search, knowledge/timeline views, remote AI through OmniRoute, agent/action safeguards, dashboard editing, finance/weather observations, and privacy-aware Chat history. These are code/build/source-review deliveries; live providers, recovery, mobile/accessibility and target-host acceptance remain deferred. Backup/admission/recovery, portable saved gadget/eligible retained brief/schedule exports, and bounded Chat copied-evidence cleanup are integrated. Document deletion receipt UI, Source canonical/linked-child cleanup, and mobile/full Dashboard code, build and review are also integrated; mobile runtime and accessibility acceptance remain deferred. Historical Source receipts and Source-local Memory coverage are also integrated as code/build/review, while their runtime and physical-erasure acceptance remain pending. Final P12/R16 release acceptance (deferred tests, runtime, providers, restore, capacity) remains pending. Read the [execution ledger](docs/superpowers/plans/EXECUTION.md) and [implementation status](docs/IMPLEMENTATION_STATUS.md) for exact task evidence.

## Start locally

Requirements: Docker Compose v2.24 or newer, Python 3.12, uv, Node.js 24, and npm. On Windows, run commands from PowerShell:

```powershell
./scripts/dev.ps1 setup
./scripts/dev.ps1 dev
```

On macOS/Linux, use `make setup` and `make dev`. Setup creates `.env` once with random setup, CSRF, and database secrets; it does not print their values or replace an existing file. Open `.env` locally and copy `SETUP_TOKEN` into the first-run setup form at [http://localhost:3000](http://localhost:3000). Keep `.env` private.

The web service binds to `127.0.0.1` only. For remote use, place it behind an HTTPS reverse proxy, set `PUBLIC_ORIGIN` to the public origin and `SECURE_COOKIES=true`, then restart the stack. Do not expose the API, Redis, or PostgreSQL ports directly.

## Commands

Both `scripts/dev.ps1` and Make provide `setup`, `dev`, `stop`, `migrate`, `seed`, `lint`, `typecheck`, `test`, and `build`. Run `./scripts/dev.ps1 seed` (or `make seed`) to explicitly add fictional Phase 1, 8, 10 and 12 demo fixtures. The command reports created/existing counts and privacy skips; it never runs at startup. Stable owner receipts preserve edits and deletions, and Chat fixtures are omitted when durable conversation history is disabled. `test` uses a uniquely named disposable Compose project and removes only its own containers and volumes.

For isolated local development, start a named workspace such as `bbd-os-ws-a1b2c3d4e5f6` with `./scripts/dev.ps1 dev -Workspace bbd-os-ws-a1b2c3d4e5f6` or `make workspace-up WORKSPACE=bbd-os-ws-a1b2c3d4e5f6`. Each workspace uses project-prefixed database, Redis, application and web dependency volumes, its own Compose network and loopback web port, and forced project-local database/Redis URLs even when `.env` contains different application URLs. Registration refuses existing unregistered project resources and records the Docker daemon identity. Seed it with `./scripts/dev.ps1 seed -Workspace <name>` or `make workspace-seed WORKSPACE=<name>`. To remove it, run `reset-preview` and inspect the name and fingerprint, then provide that exact fingerprint to `reset`. Reset refuses the default project, unregistered workspaces, changed configuration, unexpected volumes/networks, and mismatched container labels. It removes only Compose resources for that named local development workspace. Owner datasets in shared or external databases are outside this reset's scope and are not a supported reset target.

During production implementation, CI runs dependency installation and `make build` only. After all original and reconciliation production tasks are complete, manually dispatch the CI workflow with **Run deferred validation** enabled to run lint, typecheck, and tests.

PostgreSQL migrations run before API and worker startup. Their persistent data uses named volumes. `docker compose logs api migrate worker` shows service diagnostics; do not share logs with `.env` values. If startup is blocked, inspect `docker compose ps` and the migration service first.

## OmniRoute and privacy

`OMNIROUTE_BASE_URL` and `OMNIROUTE_API_KEY` are optional placeholders. The production model gateway uses the OpenAI-compatible OmniRoute endpoint and applies owner privacy and source policy before remote requests. Configured credentials do not establish provider connectivity, supported capabilities, or runtime acceptance. Collection adapters and Chat/agent calls require their own enabled configuration and consent. Review [privacy](docs/privacy.md) and [deployment](docs/deployment.md) before enabling remote access or adding connectors.

See [development](docs/development.md), [deployment](docs/deployment.md), [privacy](docs/privacy.md), and [implementation status](docs/IMPLEMENTATION_STATUS.md). See [backup and isolated restore](docs/backup-recovery.md) for the implemented encrypted backup, recovery and cleanup commands; source delivery does not prove runtime restoration.

Packaged source collection and the native GitHub App OAuth ownership model are documented in [connectors](docs/connectors.md); GitHub OAuth credentials remain API-owned and do not pass through n8n.

Optional Google sign-in uses server-side OAuth credentials. The account menu and its Google-link dialog use locally owned Radix-based shadcn source under `apps/web/src/components/ui`; run `npm ci` and `uv sync` to install the locked frontend and backend dependencies. See the [Google OAuth setup](docs/deployment.md#google-sign-in) and [open-source inventory](OSS_USED.md) for configuration and license details.

The shared Button is adapted from the official [New York shadcn Button registry source](https://ui.shadcn.com/r/styles/new-york/button.json), using the installed Radix Slot, `cn` class merger and semantic theme tokens. Its supported variants and sizes are documented in `apps/web/src/components/ui/button.tsx`; `class-variance-authority` is pinned in the npm lockfile. Owner topic interests are available below Data sources in Settings and receive the authenticated workspace CSRF token.

User display preferences (theme, interface language, and IANA time zone) are stored in the owner preferences row. The shell provides English (US) and Vietnamese UI catalogs; source and user-authored content remain unchanged.

The knowledge entity page uses React Flow for a bounded interactive graph and keeps a relationship-list fallback. See the [open-source inventory](OSS_USED.md) and [third-party license notices](THIRD_PARTY_NOTICES.md) for its locked dependencies and attribution.

## MCP endpoint deployment policy

`MCP_ALLOWED_ENDPOINT_CIDRS` accepts a JSON object mapping exact origins to private or loopback CIDRs. For example, `{"http://127.0.0.1:8080":["127.0.0.1/32"]}` authorizes that origin from the API runtime. Empty `{}` grants no private-network or plaintext HTTP exception. This configuration is independent of OmniRoute endpoint policy; selecting an endpoint in Settings does not change deployment authorization.

Public HTTPS retains the ordinary MCP destination checks. Private HTTPS requires an approved origin/CIDR mapping. Approved HTTP is credential-free: bearer authentication and credential-bearing request headers are rejected. Endpoint resolution checks every returned address and pins the selected address; redirects and automatic protocol downgrade are disabled.

MCP sources remain subject to remote data eligibility even when the recipient is named `local`. SDK compatibility, deployed network isolation, and runtime/provider acceptance are deferred validation gates; a source review or production build does not establish them.

## MCP stdio profile deployment

Stdio is unavailable by default. To enable administrator-reviewed profiles, set `MCP_STDIO_PROFILE_MANIFEST=/opt/bbd-mcp/profiles.json` and add a deployment Compose override that mounts the administrator-owned host directory read-only:

```yaml
services:
  api:
    volumes:
      - type: bind
        source: /srv/bbd-mcp
        target: /opt/bbd-mcp
        read_only: true
```

The host directory, manifest, and every launched artifact must be root-owned, canonical, and non-writable by the API service. The runtime keeps UID/GID 10001; `/app` and `/data` are not valid profile roots. The image contains only an empty root-owned `/opt/bbd-mcp` directory: it does not install, copy, or enable an MCP server or its runtime. Administrators must supply a real reviewed artifact and any required Python/Node interpreter and dependencies under that immutable root. A Python/Node entry script and interpreter must both be manifested there; the service-owned `/app/.venv` is not permitted. No command text comes from a connection, UI, or model.

The UTF-8 manifest is bounded to 1 MiB and has this exact top-level shape: `{"version":1,"profiles":{"profile-id":{"profile_hash":"<64 lowercase hex>","enabled":true,"platform":"posix","executable":"/opt/bbd-mcp/bin/server","argv":["/opt/bbd-mcp/bin/server"],"cwd":"/opt/bbd-mcp","environment":{},"immutable_root":"/opt/bbd-mcp","artifact_sha256":{"/opt/bbd-mcp/bin/server":"<64 lowercase hex>"},"runtime_kind":"native","entry_script":null}}}`. It supports at most 32 exact profile IDs. The environment transport accepts only `LANG`, `LC_ALL`, and `TZ`; shell/package-manager launchers and interpreter code-evaluation options are rejected. Do not include tokens or credentials.

`profile_hash` is the lowercase hexadecimal SHA-256 of the canonical transport policy JSON. Serialize as UTF-8 JSON with recursively sorted keys (`sort_keys=true`), compact separators `,` and `:`, and `ensure_ascii=false`. The following is the complete sorted-key JSON shape for a native profile; replace example identity/path/argument/environment/artifact values with exact deployment values. Keep all keys shown, preserve JSON types, sort the `environment` and `artifact_sha256` object keys, and use the fixed `limits` keys and values shown: `{"argv":["/opt/bbd-mcp/bin/server"],"artifact_sha256":{"/opt/bbd-mcp/bin/server":"<64 lowercase hex>"},"cwd":"/opt/bbd-mcp","enabled":true,"entry_script":null,"environment":{},"executable":"/opt/bbd-mcp/bin/server","immutable_root":"/opt/bbd-mcp","limits":{"discovery_frame":1048576,"discovery_frames":64,"discovery_total":1048576,"ordinary_frame":262144,"ordinary_frames":16,"ordinary_total":524288,"outbound_frame":131072},"platform":"posix","policy":"bbd-os-mcp-stdio-v1","profile_id":"profile-id","runtime_kind":"native"}`. Compute `profile_hash = lowercase_hex(SHA256(UTF8(canonical_json(policy_object))))`. For Python or Node, set the exact `runtime_kind` and canonical `entry_script` accepted by the transport; the profile hash covers those values too. `operation_kind` is deliberately excluded. The transport recomputes this policy hash and verifies each artifact's bytes, ownership and path ancestry before launch; the catalog does not implement a second hash policy. Replacing the manifest requires an API restart and fresh connection check, discovery, and capability grants for the new identity.

The API selects Uvicorn's asyncio loop explicitly, and API/worker Compose services use an init process. This source configuration does not prove host ACLs, read-only root filesystem policy, process cancellation behavior, provider compatibility, egress isolation, or target-capacity acceptance; those remain deployment validation gates.




