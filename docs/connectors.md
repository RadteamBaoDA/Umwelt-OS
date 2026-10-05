# Packaged connectors

## Managed connector provisioning

The owner configures packaged RSS/Atom, web and REST sources through Umwelt-OS APIs. `GET /api/v1/connectors/catalog` reports implemented and unavailable provider capabilities. `PUT /api/v1/connectors/{source_id}/configuration` saves a validated configuration against `expected_revision`; `POST /api/v1/connectors/{source_id}/validate` validates the saved configuration without activation; `GET /api/v1/connectors/{source_id}/activation` reports desired/applied revisions and state.

`POST /api/v1/connectors/{source_id}/activate` accepts the current `expected_revision`, and accepts a REST provider secret only for an explicit replacement. The secret is sent to n8n's protected credential store and is not written into Umwelt-OS desired configuration. Umwelt-OS stores opaque credential IDs and a per-source collector token reference. The existing `N8N_WEBHOOK_TOKEN` secures manual triggers. Managed workflow templates bind the source UUID, source generation, desired revision, and actual credential references before provisioning.

`POST /api/v1/connectors/{source_id}/deactivate` pauses the source and revokes collector access in the source transaction, then leaves an exact-owned workflow deactivation intent for the worker. Prepared external steps can resume after a crash. Ambiguous dispatched writes remain fenced and are never blindly replayed. To remove a REST provider credential, first deactivate the source, then call `DELETE /api/v1/connectors/{source_id}/credentials/provider?expected_revision=...`; this advances the desired revision and queues deletion of the recorded credential ID. A lost credential-create response with no returned ID is different: n8n 2.5.2 has no public credential lookup/list or idempotency key, so Umwelt-OS fences that slot and does not issue another create. The catalog exposes this recovery limit.

The adapter uses n8n's public API only. `N8N_API_KEY` is server-side configuration for the API and worker. Credential creation uses POST; rotation uses public PATCH with a known ID; removal uses DELETE with a known ID. Provider inputs are encrypted at rest with the dedicated `CONNECTOR_CREDENTIAL_ENCRYPTION_KEY`. No n8n database access or credential-list API is used. The periodic worker consumes persisted prepared credential/workflow steps, resumes activation after resolved credentials, and can settle a lost workflow-create response by the operation-specific workflow name. A source becomes collection-eligible only after workflow activation succeeds and a fresh source-generation and connector-revision check still matches.

OAuth is not enabled for RSS, web or REST. GitHub remains planned until the concrete OAuth-backed read-only collector in P09-T1 is delivered. Gmail, Calendar and Drive remain unavailable until an owning provider adapter consumes separate explicit grants; owner sign-in does not grant collection access.

The existing source setup panel still issues a scoped token and validates a source, but does not yet call the managed activation API. Its editor integration is pending R04; it no longer directs owners to configure global n8n source IDs or credentials by hand.

## Collection and network boundaries

Configure RSS/Atom sources with `feed_url`, web sources with `url` and optional `js_render`, and REST sources with `url`, `items_path`, `id_field`, `title_field`, `content_field`, and optional `updated_field`. Unknown fields, non-public hosts and invalid IANA timezones are rejected. New and unconfigured sources use `Asia/Ho_Chi_Minh`. RSS/Atom reads at most 10 pages, catches up from the last accepted cursor with a one-day overlap, and caps aggregate response data at 25 MiB. Provider record identity, versions, timestamps and provenance are preserved through receipt and ingestion.

REST next-page URLs must remain on the configured origin; redirects are disabled. Missing or invalid update timestamps use a stable observation time and leave the cursor unchanged, so unchanged pages reuse the same batch identity. Runs coalesce missed ticks by reading from the last accepted cursor. The n8n collector credential is scoped to one source. Collection accepts only the captured source generation and connector revision while the source grant and applied connector state remain current.

Run `docker compose -f docker-compose.yml -f docker-compose.connectors.yml up -d --build`. The API and worker reach the browser sidecar over a private connector network. n8n has a separate private network to the API and does not join the PostgreSQL/Redis network; its container firewall permits API traffic and DNS plus public HTTP(S), and blocks private/reserved destinations. Set `BROWSER_SHARED_TOKEN` to the same random value for the API and browser container. The browser service accepts only authenticated API requests, allows one active job, and uses byte-counted streaming HTTP with BeautifulSoup by default or Playwright when `js_render` is enabled. Defaults and hard maxima are 10 pages, depth 2, 60 seconds, and 25 MiB per job. URLs, DNS answers, redirects and browser subresources are checked; collection never performs model-driven navigation.

URL crawl submissions return a durable `run_id` after enqueueing through the ingestion outbox; poll owner-authenticated `GET /api/v1/ingestion/runs/{run_id}` for status. Paused or archived sources cannot validate, submit batches or crawl. Workflow exports in `infrastructure/n8n/workflows/` are templates; Umwelt-OS replaces source and credential placeholders before sending workflows through the supported API.

## MCP collection adapter

`modules/connectors/mcp.py` collects from an MCP connection into a source of type `mcp` through the same ingestion
receipt API as other connectors (`receive_connector_batch`, native ingestion, so no `connector_revision`). Authority is
the MCP grant: only tools or resources that the owner reviewed with `purpose="collection"` (read-only, destination
`local`, scoped to exactly that source) can be called, one grant per capability, no wildcards. Calls execute through
`modules/tools/mcp_collection.py`, which reuses the reviewed SDK client, admission slots, egress policy, credential store
and stdio profile catalog. Arguments come only from the source configuration saved with
`PUT /api/v1/connectors/sources/{id}/mcp-collection` (`{expected_generation, configuration: {connection_id, calls:
[{grant_id, arguments}]}}`, at most 10 calls); arguments are validated against the reviewed input schema, and no command,
URL or tool name is ever accepted from the UI or a model. Stdio remains admin-allowlisted.

`POST /api/v1/connectors/sources/{id}/collect` is the protected collection API and runs the adapter for `mcp` sources.
Every request and the result return re-check the connection (enabled, revision), grant (revision, expiry, revocation),
descriptor, stdio profile identity and source generation. Disabling the connection or revoking the grant stops new calls
and keeps every observation already ingested; deleting is a separate source purge.

Declared contract (`CONTRACT` in the module):

- Identity: `mcp:<connection>:<digest of remote key and canonical arguments>:<item>`, where item is the resource URI, the
  `id` of a `structuredContent.items[]` object, or the text-block index (stable only while block order is).
- Normalization: resource text, `structuredContent.items[]` objects as canonical JSON, or non-empty text blocks. Binary
  content is skipped. `version` is a content SHA-256; `observed_at` is the collection time (MCP has no item timestamp).
- Provenance (record metadata): connection, grant and revision, connection revision, capability kind and remote key,
  descriptor hash, arguments digest.
- Pagination and history: one request per call per run, provider cursors are not followed, no backfill; every run appends
  observations, so unchanged items repeat with an identical version.

Open gates: the n8n schedule that calls the collection API (workflow template, provisioning and a collector-authenticated
trigger) is not provisioned, so collection is manual for now; the owner editor (`mcp-editor.tsx`) belongs to P08; the
catalog entry lists both under `unavailable_operations`.
