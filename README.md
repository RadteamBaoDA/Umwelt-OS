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


