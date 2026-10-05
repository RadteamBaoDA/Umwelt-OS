# Open-source inventory

| Package | Locked version | License | Use |
| --- | --- | --- | --- |
| [Authlib](https://github.com/authlib/authlib) | 1.8.0 | BSD-3-Clause | Server-side OpenID Connect discovery, authorization-code + PKCE flow, ID-token/JWKS validation for Google sign-in. |
| [backoff](https://github.com/litl/backoff) | 2.2.1 | MIT | Required dependency metadata for pinned PostHog; Graphiti telemetry is disabled before imports. Older release, with advisory review deferred. |
| [FalkorDB Python client](https://github.com/FalkorDB/falkordb-py) | 1.2.0 | MIT | Graphiti's optional FalkorDB driver, isolated from ARQ Redis. `modules/knowledge/temporal/adapter.py` wraps its public driver path for bounds/redaction; no dependency source is modified. |
| [FalkorDB server image](https://github.com/FalkorDB/FalkorDB) | 4.22.0 (`sha256:4ac83f55062d364dcf151692635138b0bd2e12578019a56eb86ef53d067b116b`) | SSPL-1.0 | Optional disabled service in `infrastructure/graph/compose.yml` for derived temporal knowledge. The selected private single-owner self-hosted scope is recorded in the Phase 5 decision report; distribution or service use needs separate license review. |
| [Graphiti Core](https://github.com/getzep/graphiti) | 0.30.2 | Apache-2.0 | `modules/knowledge/temporal/adapter.py` injects controlled LLM/embed/rerank clients and wraps the Falkor driver; no Graphiti package source is modified. |
| [itsdangerous](https://github.com/pallets/itsdangerous) | 2.2.0 | BSD-3-Clause | Required by Starlette `SessionMiddleware` for signed session-cookie support. |
| [Radix UI](https://github.com/radix-ui/primitives) | 1.6.7 | MIT | Primitives used by the account DropdownMenu and Dialog. |
| [next-intl](https://github.com/amannn/next-intl) | 4.14.8 | MIT | English and Vietnamese interface catalogs and locale context. |
| [next-themes](https://github.com/pacocoursey/next-themes) | 0.4.6 | MIT | System-aware light/dark theme class bootstrap. |
| [HTTPX](https://github.com/encode/httpx) | 0.28.1 | BSD-3-Clause | OpenAI SDK HTTP transport with DNS result CIDR approval, numeric-IP pinning, and proxy/redirect denial at the OmniRoute boundary. |
| [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) | 2.3.0 | MIT | Maintained Model Context Protocol client/server SDK used for the remote MCP transport and typed protocol operations. |
| [MCP Types](https://github.com/modelcontextprotocol/python-sdk/tree/main/src/mcp-types) | 2.3.0 | MIT | Typed protocol models required by the MCP Python SDK. |
| [LangGraph](https://github.com/langchain-ai/langgraph/tree/1.2.12/libs/langgraph) | 1.2.12 | MIT | Runs the single bounded read-only assistant graph; orchestration uses the existing ModelGateway and ToolRegistry. |
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

The shadcn components are repository-owned source in `apps/web/src/components/ui/dialog.tsx`, `apps/web/src/components/ui/dropdown-menu.tsx`, `apps/web/src/components/ui/select.tsx`, `apps/web/src/components/ui/checkbox.tsx`, `apps/web/src/components/ui/tabs.tsx`, and `apps/web/src/components/ui/sheet.tsx`; Sheet and Tabs are adapted from the official New York registry items. `apps/web/components.json` records their New York style and Radix family. Install frontend dependencies with `npm ci` and backend dependencies with `uv sync`; versions are recorded in `package-lock.json` and `uv.lock`. React Flow's runtime dependency license notices are retained in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md); the `@types` entries above document compile-time packages only.

`apps/web/src/components/ui/textarea.tsx` adopts the Tailwind v4 New York Textarea from the official shadcn registry item ([source](https://ui.shadcn.com/r/styles/new-york-v4/textarea.json), MIT), retaining the repository's `cn` helper and required local JSDoc.

Google sign-in requests only `openid`, `email`, and `profile`; it does not grant Gmail access.
