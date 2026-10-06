# Open-source and source-available inventory

Inventory reconciliation is in progress. The "Direct application runtime dependencies" table covers direct runtime packages only; its license labels come from lockfile-matched installed package metadata. The transitive audit is pending. Entries below document identified dependencies and copied source; runtime images, the optional browser runtime, build tooling and redistributed notices are being reconciled before the final release gate. This is not a complete transitive license audit.

| Package | Locked version | License | Use |
| --- | --- | --- | --- |
| [AnythingLLM frontend source slices](https://github.com/Mintplex-Labs/anything-llm/tree/128a01575a50f0284aeca75a93399b6fb1db0328) | `128a01575a50f0284aeca75a93399b6fb1db0328` | MIT | Bounded adaptations of upstream scroll, composer input, markdown/sanitizer, and copy-feedback regions in `apps/web/src/modules/chat`; exact originals, local changes, and hashes are recorded in `docs/anythingllm-port.md` and `.superpowers/sdd/r07/source-regions.md`. No upstream transport, backend, provider code, CSS, or assets are included. |
| [age / age-keygen host CLI](https://github.com/FiloSottile/age) | Operator-installed; not bundled or lockfile-pinned | BSD-3-Clause ([upstream license](https://github.com/FiloSottile/age/blob/main/LICENSE)) | Standard age recipient encryption and protected identity verification for backup/isolated restore in `modules/backup/host.py`, integrated at `5212def`. The binary version and operational compatibility must be recorded during deferred restore acceptance; no source or binary is redistributed by this repository. |
| [Authlib](https://github.com/authlib/authlib) | 1.8.0 | BSD-3-Clause | Server-side OpenID Connect discovery and Google sign-in validation, plus fixed-endpoint GitHub App OAuth code exchange and rotating-token refresh. |
| [class-variance-authority](https://github.com/joe-bell/cva) | 0.7.1 | Apache-2.0 | Variant and size class selection for the repository-owned shared Button adapted from the official New York shadcn registry source. |
| [DOMPurify](https://github.com/cure53/DOMPurify) | 3.4.16 | MPL-2.0 OR Apache-2.0 | Browser-side sanitization of rendered Chat markdown in `apps/web/src/modules/chat/chat-markdown.tsx`; Apache-2.0 notice from the locked npm package is reproduced in `THIRD_PARTY_NOTICES.md`. |
| [backoff](https://github.com/litl/backoff) | 2.2.1 | MIT | Required dependency metadata for pinned PostHog; Graphiti telemetry is disabled before imports. Older release, with advisory review deferred. |
| [FalkorDB Python client](https://github.com/FalkorDB/falkordb-py) | 1.2.0 | MIT | Graphiti's optional FalkorDB driver, isolated from ARQ Redis. `modules/knowledge/temporal/adapter.py` wraps its public driver path for bounds/redaction; no dependency source is modified. |
| [FalkorDB server image](https://github.com/FalkorDB/FalkorDB) | 4.22.0 (`sha256:4ac83f55062d364dcf151692635138b0bd2e12578019a56eb86ef53d067b116b`) | SSPL-1.0 | Optional disabled service in `infrastructure/graph/compose.yml` for derived temporal knowledge. The selected private single-owner self-hosted scope is recorded in the Phase 5 decision report; distribution or service use needs separate license review. |
| [Graphiti Core](https://github.com/getzep/graphiti) | 0.30.2 | Apache-2.0 | `modules/knowledge/temporal/adapter.py` injects controlled LLM/embed/rerank clients and wraps the Falkor driver; no Graphiti package source is modified. |
| [itsdangerous](https://github.com/pallets/itsdangerous) | 2.2.0 | BSD-3-Clause | Required by Starlette `SessionMiddleware` for signed session-cookie support. |
| [Radix UI](https://github.com/radix-ui/primitives) | 1.6.7 | MIT | Primitives used by the account DropdownMenu and Dialog. |
| [n8n community container](https://github.com/n8n-io/n8n/tree/n8n%402.5.2) | 2.5.2 (`n8nio/n8n:2.5.2`) | Source-available Sustainable Use License; enterprise-marked files have separate terms ([exact release license](https://github.com/n8n-io/n8n/blob/n8n%402.5.2/LICENSE.md)) | Optional packaged connector/schedule runtime. `infrastructure/docker/n8n-connectors.Dockerfile` adds the network firewall/entrypoint without modifying n8n application source; native Settings uses supported server APIs. Approved use is the personal single-owner self-hosted deployment. |
| [next-intl](https://github.com/amannn/next-intl) | 4.14.8 | MIT | English and Vietnamese interface catalogs and locale context. |
| [next-themes](https://github.com/pacocoursey/next-themes) | 0.4.6 | MIT | System-aware light/dark theme class bootstrap. |
| [markdown-it](https://github.com/markdown-it/markdown-it) | 15.0.2 | MIT | Markdown rendering for saved and streaming Chat message content in `apps/web/src/modules/chat/chat-markdown.tsx`; package notice is reproduced in `THIRD_PARTY_NOTICES.md`. |
| [Recharts](https://github.com/recharts/recharts) | 3.10.1 | MIT | Renders standard financial, activity, and market series time-series charts through the recharts-wrapper. |
| [globe.gl](https://github.com/vasturiano/globe.gl) | 2.45.0 | MIT | Client-side 3D globe renderer for local, owner-authorized observation points; no remote globe imagery or tiles are requested. |
| [Three.js](https://github.com/mrdoob/three.js) | 0.186.1 | MIT | WebGL material/rendering dependency used by globe.gl. |
| [deck.gl](https://github.com/visgl/deck.gl) (`@deck.gl/core`, `@deck.gl/layers`, `@deck.gl/react`) | 9.4.0 | MIT | Client-side flat map and point rendering using local geometry and owner-authorized observation points. |
| [world-atlas](https://github.com/topojson/world-atlas) | 2.0.2 | ISC; bundled Natural Earth data is public domain | Local 1:110m Natural Earth land geometry for the flat map. |
| [topojson-client](https://github.com/topojson/topojson-client) | 3.1.0 | ISC | Converts the bundled world-atlas TopoJSON land geometry to GeoJSON for deck.gl. |
| [HTTPX](https://github.com/encode/httpx) | 0.28.1 | BSD-3-Clause | OpenAI SDK HTTP transport with DNS result CIDR approval, numeric-IP pinning, and proxy/redirect denial at the OmniRoute boundary. |
| [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) | 2.3.0 | MIT | Maintained Model Context Protocol client/server SDK used for the remote MCP transport and typed protocol operations. |
| [MCP Types](https://github.com/modelcontextprotocol/python-sdk/tree/main/src/mcp-types) | 2.3.0 | MIT | Typed protocol models required by the MCP Python SDK. |
| [LangGraph](https://github.com/langchain-ai/langgraph/tree/1.2.12/libs/langgraph) | 1.2.12 | MIT | Runs bounded checkpointed Agent workflows, specialist handoffs and approved effects through the existing ModelGateway and ToolRegistry. |
| [LangGraph Checkpoint](https://github.com/langchain-ai/langgraph/tree/1.2.12/libs/checkpoint) | 4.1.0 | MIT | Defines the pinned checkpoint saver serialization contract. |
| [LangGraph Checkpoint Postgres](https://github.com/langchain-ai/langgraph/tree/1.2.12/libs/checkpoint-postgres) | 3.1.2 | MIT | Persists graph checkpoints through AsyncPostgresSaver; its exact schema is installed by Alembic, not startup setup. |
| [Psycopg](https://github.com/psycopg/psycopg) | 3.3.3 | LGPL-3.0-or-later | Async PostgreSQL checkpoint connection and bundled binary implementation. |
| [Psycopg Pool](https://github.com/psycopg/psycopg) | 3.3.0 | LGPL-3.0-or-later | Declared AsyncPostgresSaver pool dependency; application connections remain per-run and bounded. |
| [LangChain Core](https://github.com/langchain-ai/langchain/tree/master/libs/core) | 1.6.6 | MIT | LangGraph's required protocol and runnable contracts; application model I/O remains in ModelGateway. |
| [LangChain Protocol](https://github.com/langchain-ai/langchain/tree/master/libs/protocol) | 0.0.19 | MIT | LangChain Core's protocol dependency, locked transitively. |
| [LangGraph Prebuilt](https://github.com/langchain-ai/langgraph/tree/1.2.12/libs/prebuilt) | 1.1.0 | MIT | LangGraph package dependency; no prebuilt agent runtime is used. |
| [LangGraph SDK](https://github.com/langchain-ai/langgraph/tree/1.2.12/libs/sdk) | 0.4.5 | MIT | LangGraph package dependency; no hosted SDK service is used. |
| [LangSmith](https://github.com/langchain-ai/langsmith-sdk) | 0.14.4 | MIT | LangChain Core metadata dependency; telemetry remains outside this workflow. |
| [orjson](https://github.com/ijl/orjson) | 3.12.0 | Apache-2.0, MIT, MPL-2.0 | Locked checkpoint-postgres JSON dependency. |
| [ormsgpack](https://github.com/ormsgpack/ormsgpack) | 1.12.2 | Apache-2.0, MIT | LangGraph checkpoint package dependency; this workflow supplies its own strict JSON serializer. |
| [xxhash](https://github.com/ifduyue/python-xxhash) | 4.0.1 | BSD-2-Clause | LangGraph package hashing dependency. |
| [zstandard](https://github.com/indygreg/python-zstandard) | 0.25.0 | BSD-3-Clause | LangSmith SDK dependency, locked transitively. |
| [uuid-utils](https://github.com/aminalaee/uuid-utils) | 0.17.1 | MIT | LangSmith SDK dependency, locked transitively. |
| [requests-toolbelt](https://github.com/requests/toolbelt) | 1.0.0 | Apache-2.0 | LangSmith SDK dependency, locked transitively. |
| [Psycopg Binary](https://github.com/psycopg/psycopg) | 3.3.3 | LGPL-3.0-or-later | Wheel-provided binary build of the Psycopg PostgreSQL adapter. |
| [HTTPX2](https://github.com/pydantic/httpx2) | 2.13.1 | BSD-3-Clause | Maintained `httpx2` client used by the MCP SDK; distinct from the existing HTTPX 0.x dependency. |
| [HTTPCore2](https://github.com/pydantic/httpx2/blob/main/src/httpcore2) | 2.13.1 | BSD-3-Clause | HTTP transport dependency of HTTPX2. |
| [HTTPX2 JS Fetch](https://pypi.org/project/httpx2-jsfetch/1.0/) | 1.0 | BSD-3-Clause | Conditional `emscripten` backend dependency of HTTPX2; included in the cross-platform lock. |
| [jsonschema](https://github.com/python-jsonschema/jsonschema) | 4.26.0 | MIT | MCP SDK schema validation dependency; this package was already a direct project dependency, and its missing notice is added below. |
| [jsonschema-specifications](https://github.com/python-jsonschema/jsonschema-specifications) | 2025.9.1 | MIT | `jsonschema` runtime dependency providing published schema resources. |
| [attrs](https://github.com/python-attrs/attrs) | 26.1.0 | MIT | `jsonschema` runtime dependency for immutable data classes. |
| [referencing](https://github.com/python-jsonschema/referencing) | 0.37.0 | MIT | `jsonschema` runtime dependency for reference resolution. |
| [rpds-py](https://github.com/python-jsonschema/rpds-py) | 2026.6.3 | MIT | `jsonschema` runtime dependency for persistent data structures. |
| [OpenTelemetry API](https://github.com/open-telemetry/opentelemetry-python) | 1.45.0 | Apache-2.0 | Optional instrumentation API required by the MCP SDK package metadata. |
| [pywin32](https://github.com/mhammond/pywin32) | 312 | PSF-2.0 distribution metadata; selected components include separate notices | Windows-only dependency declared by the MCP SDK. |
| [sse-starlette](https://github.com/sysid/sse-starlette) | 3.5.0 | BSD-3-Clause | Server-sent event support declared by the MCP SDK. |
| [truststore](https://github.com/sethmlarson/truststore) | 0.10.4 | MIT | System certificate-store integration used by HTTPX2 and HTTPCore2. |
| [cn](https://github.com/shadcn-ui/cn) | 0.4.0 | MIT | Tailwind-aware class merging in the locally owned shadcn component source. |
| [Lucide React](https://github.com/lucide-icons/lucide) | 1.49.0 | ISC | Close icon used by the generated Dialog component. |
| [React Flow (`@xyflow/react`)](https://github.com/xyflow/xyflow) | 12.12.0 | MIT | Renders the bounded knowledge entity graph; the page also provides an accessible relationship-list fallback. |
| [`@xyflow/system`](https://github.com/xyflow/xyflow) | 0.0.83 | MIT | React Flow's graph and interaction system. |
| [`classcat`](https://github.com/jorgebucaran/classcat) | 5.0.5 | MIT | React Flow class-name helper. |
| [`zustand`](https://github.com/pmndrs/zustand) | 4.5.7 | MIT | React Flow state store; this version is nested under `@xyflow/react`. |
| [`use-sync-external-store`](https://github.com/facebook/react/tree/main/packages/use-sync-external-store) | 1.7.0 | MIT | Zustand's external-store compatibility dependency. |
| [neo4j Python driver](https://github.com/neo4j/neo4j-python-driver) | 5.28.3 | Apache-2.0 | Mandatory Graphiti dependency metadata; no Neo4j server is deployed. |
| [NumPy](https://github.com/numpy/numpy) | 2.5.3 | BSD-3-Clause, 0BSD, MIT, Zlib, CC0-1.0 | Required Graphiti numerical dependency; native target-image and resource compatibility remain unverified. |
| [PostHog](https://github.com/PostHog/posthog-python) | 7.61.1 | MIT | Required Graphiti dependency metadata; telemetry remains disabled before Graphiti import. |
| [python-dateutil](https://github.com/dateutil/dateutil) | 2.9.0.post0 | Apache-2.0 / BSD-3-Clause | Required metadata dependency of the pinned FalkorDB client. |
| [pytz](https://github.com/stub42/pytz) | 2026.4 | MIT | Required Graphiti dependency metadata. |
| [tenacity](https://github.com/jd/tenacity) | 9.1.4 | Apache-2.0 | Required Graphiti retry utility dependency; ModelGateway remains the provider retry owner. |
| [`d3-color`](https://github.com/d3/d3-color), [`d3-dispatch`](https://github.com/d3/d3-dispatch), [`d3-drag`](https://github.com/d3/d3-drag), [`d3-interpolate`](https://github.com/d3/d3-interpolate), [`d3-selection`](https://github.com/d3/d3-selection), [`d3-timer`](https://github.com/d3/d3-timer), [`d3-transition`](https://github.com/d3/d3-transition), [`d3-zoom`](https://github.com/d3/d3-zoom) | 3.1.0, 3.0.1, 3.0.0, 3.0.1, 3.0.0, 3.0.1, 3.0.1, 3.0.0 | ISC | Runtime dependencies of `@xyflow/system`. Their copyright and permission notices are in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). |
| [`d3-ease`](https://github.com/d3/d3-ease) | 3.0.1 | BSD-3-Clause | Runtime dependency of `@xyflow/system`; notice is in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). |
| [`@types/d3-color`](https://github.com/DefinitelyTyped/DefinitelyTyped/tree/master/types/d3-color), [`@types/d3-drag`](https://github.com/DefinitelyTyped/DefinitelyTyped/tree/master/types/d3-drag), [`@types/d3-interpolate`](https://github.com/DefinitelyTyped/DefinitelyTyped/tree/master/types/d3-interpolate), [`@types/d3-selection`](https://github.com/DefinitelyTyped/DefinitelyTyped/tree/master/types/d3-selection), [`@types/d3-transition`](https://github.com/DefinitelyTyped/DefinitelyTyped/tree/master/types/d3-transition), [`@types/d3-zoom`](https://github.com/DefinitelyTyped/DefinitelyTyped/tree/master/types/d3-zoom) | 3.1.3, 3.0.7, 3.0.4, 3.0.12, 3.0.9, 3.0.9 | MIT | Type declarations installed as `@xyflow/system` dependencies; not runtime JavaScript. |

The shadcn components are repository-owned source in `apps/web/src/components/ui/dialog.tsx`, `apps/web/src/components/ui/dropdown-menu.tsx`, `apps/web/src/components/ui/select.tsx`, `apps/web/src/components/ui/checkbox.tsx`, `apps/web/src/components/ui/tabs.tsx`, and `apps/web/src/components/ui/sheet.tsx`, and `apps/web/src/components/ui/button.tsx`; Button is adapted from the New York button registry item with the Radix Slot and semantic tokens; Sheet and Tabs are adapted from the official New York registry items. `apps/web/components.json` records their New York style and Radix family. Install frontend dependencies with `npm ci` and backend dependencies with `uv sync`; versions are recorded in `package-lock.json` and `uv.lock`. React Flow's runtime dependency license notices are retained in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md); the `@types` entries above document compile-time packages only.

`apps/web/src/components/ui/textarea.tsx` adopts the Tailwind v4 New York Textarea from the official shadcn registry item ([source](https://ui.shadcn.com/r/styles/new-york-v4/textarea.json), MIT), retaining the repository's `cn` helper and required local JSDoc.

Google sign-in requests only `openid`, `email`, and `profile`; it does not grant Gmail access.

## Product references

[WorldMonitor](https://github.com/koala73/worldmonitor/) and [GOIES](https://github.com/tanu-1403/GOIES) are design references for the world-data dashboard. They are not installed dependencies or copied-source components in the current integrated inventory. The local intelligence projections and CII method are described in [intelligence-methods.md](docs/intelligence-methods.md); do not represent them as imported WorldMonitor scores. Any later source reuse requires its exact revision, local paths, modifications and license notices to be recorded here before delivery.

The actual adapted Chat source is AnythingLLM, with revision and local modifications recorded in [anythingllm-port.md](docs/anythingllm-port.md) and the table above. Backup host tooling is inventoried above from accepted source at `5212def`. Docker Compose v2.24 or newer is required for the override syntax; age/age-keygen are separately installed operator tools rather than bundled application dependencies. Their actual host version/restore compatibility remains a deferred acceptance record.

## Direct application runtime dependencies

These additions use exact lockfile versions and matching installed distribution/package metadata. License labels describe the top-level package; bundled native/transitive notices still require release reconciliation. Source links follow the local package metadata.

| Package | Locked version | License | Use |
| --- | --- | --- | --- |
| [Alembic](https://github.com/sqlalchemy/alembic/) | 1.20.0 | MIT | Serialized PostgreSQL schema migrations. |
| [argon2-cffi](https://github.com/hynek/argon2-cffi) | 25.1.0 | MIT | Owner password hashing. |
| [ARQ](https://github.com/python-arq/arq) | 0.28.0 | MIT | Bounded worker execution; PostgreSQL retains durable intents. |
| [asyncpg](https://github.com/MagicStack/asyncpg) | 0.31.0 | Apache-2.0 | Async PostgreSQL application driver. |
| [FastAPI](https://github.com/fastapi/fastapi) | 0.141.1 | MIT | Owner-authenticated API services. |
| [pypdf](https://github.com/py-pdf/pypdf) | 6.19.0 | BSD-3-Clause | Imported PDF text parsing. |
| [pydantic-settings](https://github.com/pydantic/pydantic-settings) | 2.15.0 | MIT | Typed protected server configuration. |
| [python-multipart](https://github.com/Kludex/python-multipart) | 0.0.32 | Apache-2.0 | API multipart upload handling. |
| [python-docx](https://github.com/python-openxml/python-docx) | 1.2.0 | MIT | Imported DOCX parsing. |
| [Redis Python client](https://github.com/redis/redis-py) | 5.3.1 | MIT | Queue/cache client; distinct from Redis server licensing. |
| [SQLAlchemy](https://github.com/sqlalchemy/sqlalchemy) | 2.1.0 | MIT | Application persistence and transaction/query contracts. |
| [structlog](https://github.com/hynek/structlog) | 25.5.0 | MIT OR Apache-2.0 | Structured application logging. |
| [tiktoken](https://github.com/openai/tiktoken) | 0.14.0 | MIT License | Bounded document token/chunk accounting. |
| [Uvicorn](https://github.com/Kludex/uvicorn) | 0.54.0 | BSD-3-Clause | API ASGI server. |
| [OpenAI Python SDK](https://github.com/openai/openai-python) | 2.54.0 | Apache-2.0 | OpenAI-compatible OmniRoute transport; gateway retains policy ownership. |
| [cryptography](https://github.com/pyca/cryptography) | 50.0.2 | Apache-2.0 OR BSD-3-Clause | Protected connector/settings credentials. |
| [@hookform/resolvers](https://github.com/react-hook-form/resolvers) | 5.9.1 | MIT | Form schema integration. |
| [@tanstack/react-query](https://github.com/TanStack/query) | 5.103.2 | MIT | Authenticated client query/mutation caching. |
| [Next.js](https://github.com/vercel/next.js) | 16.3.6 | MIT | Frontend application framework and server. |
| [React](https://github.com/react/react) | 19.3.0 | MIT | Frontend component/runtime rendering. |
| [React DOM](https://github.com/react/react) | 19.3.0 | MIT | Browser DOM rendering. |
| [react-hook-form](https://github.com/react-hook-form/react-hook-form) | 7.88.0 | MIT | Native Settings and application forms. |
| [Zod](https://github.com/colinhacks/zod) | 4.6.5 | MIT | Frontend schema validation. |
| [Zustand](https://github.com/pmndrs/zustand) | 5.0.15 | MIT | Direct application state dependency; separate from nested4.x. |
## Runtime image and optional browser inventory

| Component | Source version/reference | Notice state |
| --- | --- | --- |
| PostgreSQL with pgvector | pgvector/pgvector:0.8.6-pg16-bookworm | PostgreSQL, pgvector and Debian bundle notices pending; tag specifies PostgreSQL major16, not exact patched version. |
| Redis server | redis:7.4.11-alpine | Server/Alpine bundle notices pending; Redis client license above does not describe the server. |
| Python base | python:3.12.14-slim-bookworm | Interpreter and Debian bundle notices pending. |
| Node base | node:24.20.0-alpine3.23 | Node and Alpine bundle notices pending. |
| Firewall build base | alpine:3.22 | Copied firewall artifact/component notices pending. |
| Beautiful Soup | 4.15.0 | Optional browser runtime; license/source/notice metadata reconciliation pending. |
| Crawlee | 1.10.2 | Optional browser runtime; license/source/notice metadata reconciliation pending. |
| Playwright Python | 1.63.0 | Optional browser runtime; license/source/notice metadata reconciliation pending. Chromium binary revision is separate. |

Base tags above are recorded from source and have no digest pin in the referenced Dockerfiles/Compose. They do not prove the resolved image contents. Optional Chromium binary notices, application transitive dependencies, development/build tooling and redistribution notice coverage remain release gates.

Build tooling: Tailwind CSS and @tailwindcss/postcss4.3.3 have matching MIT package metadata; uv0.12.11 is the Dockerfile installer reference. Hatchling is constrained by pyproject to>=1.27,<2 but has no resolved version recorded in uv.lock. Build tools are separate from application runtime; their full notice reconciliation remains pending.
Native license/notice texts for the24 additional direct runtime packages above are preserved in THIRD_PARTY_NOTICES.md (28 files, exact source paths and SHA-256 hashes). This closes those specific notice-copy gaps; optional browser, container and remaining transitive/build inventory reconciliation is still pending.
