# Packaged connectors

## GitHub App collection

The native `github` provider uses BBD-OS's registered GitHub App OAuth callback with browser-bound single-use S256 PKCE. Owners save the repository and read-resource selection before connecting; access and rotating refresh tokens are encrypted by the API and never sent to n8n. The callback verifies the selected repository belongs to an installed App and exercises each enabled read permission. The n8n workflow only schedules the source-scoped collector webhook and does not receive GitHub credentials. Public `github_releases` remains a separate unauthenticated provider.

GitHub App registration is a manual deployment step; BBD-OS does not create or administer the App. Set `GITHUB_APP_ID` to its positive numeric App ID, configure the exact callback URL from `GITHUB_APP_CALLBACK_URL`, and set a separate random `GITHUB_APP_WEBHOOK_SECRET`. Configure the App's webhook URL as `https://<public-host>/api/v1/connectors/github/webhook`, enable SSL verification, and subscribe to `ping`, `issues`, `pull_request`, `release`, `push`, `repository`, `public`, `installation`, and `installation_repositories`. Grant only the read permissions required by the selected resources: repository metadata plus Issues, Pull requests, Contents, and Releases as applicable. OAuth client credentials and the webhook secret are distinct values.

The receiver verifies GitHub's `sha256=` HMAC over the exact streamed request bytes before parsing. It accepts at most 256 KiB within five seconds and only stores a delivery digest plus bounded identifiers; webhook payload content is never used as document content. The receiver returns success only after the durable digest and dispatch slot commit. Duplicate delivery IDs with the same digest are idempotent; a reused ID with different bytes is rejected. Delivery-derived event, App/installation/repository IDs, and target details are scrubbed after 30 days once fanout completes; the delivery ID and digest remain for replay protection. `GITHUB_WEBHOOK_RECEIVER_REVISION` namespaces delivery IDs. Increment it only as an explicit deduplication-namespace retirement: doing so begins a new replay-protection namespace, so delayed deliveries from the old namespace can be admitted again. Webhooks are best-effort invalidation hints: polling remains the catch-up path, delivery order is not assumed, and a missing or forbidden target is not proof of deletion. Target requests use the accepted numeric repository identity; a coalesced overflow hint reads bounded pages for its specific resource and never advances the polling cursor. If that reconciliation reaches 100 pages, the source hint remains in attention with incomplete coverage. An ambiguous missing/forbidden current item pauses the source and hides it from current Search/News evidence; the Settings editor shows the uncertainty. Retained owner history remains available and is distinct from current evidence. The current provider evidence does not carry a matching accepted version that can safely authorize a document tombstone, so automatic deletion is not performed. The owner can still delete imported data explicitly through the existing deletion contract.

Each unfinished outbox freezes at most 50 detached binding identities and their cursor in its private `fanout_page`; up to 100 authenticated targets per delivery yield at most 5,000 distinct per-source admissions. A canonical SHA-256 of each complete target distinguishes multiple targets for the same source. The hint change, owned capacity transfer/reservation, and admission result commit together. Failed pages retain admissions for retry, so interleaved deliveries cannot restart a previously admitted overflow reconciliation. Successful cursor advancement or terminal completion clears that page state. Deferred capacity recovery rechecks current source/grant fences; candidates without a current binding are scheduled at least 60 seconds later so they cannot block other sources. Neither a frozen fanout binding nor a deferred hint authorizes provider access; every first admission and every claim revalidates current owner state.

The worker fans out at most 50 due delivery pages per pass and sends at most one n8n wake per pass. It uses a global 100,000-digest and 100,000-pending-work limit; a source-less outbox reservation transfers to the first durable source hint, and additional hints require free capacity. Retry spacing grows from 30 seconds to one hour. After five failed attempts, work remains in `needs_attention` and the worker retries it hourly; owners can also use the source's existing **Collect now** action to wake packaged collection. The Sources page shows whether receiver credentials are configured, queue totals, oldest outstanding work, and attention count; it does not verify GitHub's remote App settings.

Collection is fenced by source generation, connector revision, grant operation and token revision. Refresh and app/user-wide revoke are serialized by a durable owner coordinator. Disconnect first presents a bounded peer inventory; the owner reviews all affected sources, then the API rechecks that inventory under the coordinator and source locks before pausing sources and revoking the GitHub grant. If the remote revoke result is uncertain, local sources remain paused and the coordinator reports reconciliation required. Expired or uncertain grants require owner reconnection; no provider call is replayed automatically.

Webhook delivery digests are kept for replay protection for 30 days after their details are scrubbed; the worker then deletes digests whose fanout is complete and returns their capacity to the 100,000-digest limit (a replayed ID after that window is harmless because deliveries are only revalidated hints and HMAC verification still applies). Archiving or purging a GitHub source clears its stored tokens. If other live sources use the same GitHub account the account grant is left alone, because GitHub's revoke is app/user-wide; if it was the last live source, the worker makes one best-effort remote revoke outside the deletion transaction and then clears the tokens whatever the outcome, recording only an opaque outcome code on the grant. Deletion is never blocked by the revoke.

Polling persists the verified numeric repository, installation, and App identity. Each collector request fetches one resource page and commits its raw proof, normalized records, and bounded cursor together through the Ingestion receipt. The configurable bootstrap horizon defaults to 90 days and is limited to 1–365 days; incremental issue, pull, and commit scans use a 24-hour overlap. Resource sweeps stop after 100 pages or 10,000 examined objects and report incomplete coverage when that bound is reached. A GitHub page acknowledgement never means the repository's entire history has been collected or indexed.

## Managed connector provisioning

The owner configures packaged RSS/Atom, web and REST sources through Umwelt-OS APIs. `GET /api/v1/connectors/catalog` reports implemented and unavailable provider capabilities. `PUT /api/v1/connectors/{source_id}/configuration` saves a validated configuration against `expected_revision`; `POST /api/v1/connectors/{source_id}/validate` validates the saved configuration without activation; `GET /api/v1/connectors/{source_id}/activation` reports desired/applied revisions and state.

`POST /api/v1/connectors/{source_id}/activate` accepts the current `expected_revision`, and accepts a REST provider secret only for an explicit replacement. The secret is sent to n8n's protected credential store and is not written into Umwelt-OS desired configuration. Umwelt-OS stores opaque credential IDs and a per-source collector token reference. The existing `N8N_WEBHOOK_TOKEN` secures manual triggers. Managed workflow templates bind the source UUID, source generation, desired revision, and actual credential references before provisioning.

`POST /api/v1/connectors/{source_id}/deactivate` pauses the source and revokes collector access in the source transaction, then leaves an exact-owned workflow deactivation intent for the worker. Prepared external steps can resume after a crash. Ambiguous dispatched writes remain fenced and are never blindly replayed. To remove a REST provider credential, first deactivate the source, then call `DELETE /api/v1/connectors/{source_id}/credentials/provider?expected_revision=...`; this advances the desired revision and queues deletion of the recorded credential ID. A lost credential-create response with no returned ID is different: n8n 2.5.2 has no public credential lookup/list or idempotency key, so Umwelt-OS fences that slot and does not issue another create. The catalog exposes this recovery limit.

The adapter uses n8n's public API only. `N8N_API_KEY` is server-side configuration for the API and worker. Credential creation uses POST; rotation uses public PATCH with a known ID; removal uses DELETE with a known ID. Provider inputs are encrypted at rest with the dedicated `CONNECTOR_CREDENTIAL_ENCRYPTION_KEY`. No n8n database access or credential-list API is used. The periodic worker consumes persisted prepared credential/workflow steps, resumes activation after resolved credentials, and can settle a lost workflow-create response by the operation-specific workflow name. A source becomes collection-eligible only after workflow activation succeeds and a fresh source-generation and connector-revision check still matches.

OAuth is not enabled for RSS, web or REST. GitHub collection uses the OAuth-backed read-only collector described above. Gmail, Calendar and Drive remain unavailable until an owning provider adapter consumes separate explicit grants; owner sign-in does not grant collection access.

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

MCP source collection configuration accepts `schedule_interval_minutes` (15, 30, 60, 360 or 1440) and an IANA
`timezone`. Save the MCP source configuration, then activate its packaged workflow through the normal connector
activation API. n8n receives a separate persistent `mcp:collect` credential; it cannot submit ingestion batches.
Its `POST /api/v1/connectors/sources/{id}/mcp-collect` endpoint requires the active source generation, applied
connector revision and configured connection ID, and rechecks the MCP token during each transport authorization.
Connection and grant revisions are resolved from current owner-reviewed grants for every call. Pause or disable fences
new calls, while already ingested observations remain until explicit source deletion.

The catalog advertises scheduled and manual collection. In Data sources, the owner selects an enabled MCP connection,
chooses active collection grants already reviewed for that exact source, supplies fixed JSON arguments, and saves or
activates its bounded schedule. MCP connections and grant review remain in MCP settings. Runtime n8n credential
provisioning and provider acceptance remain deferred validation gates.
