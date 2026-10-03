# Graphiti and FalkorDB compatibility record

## Status

```json
{
  "source_constraints_reviewed": true,
  "runtime_compatibility_verified": false,
  "live_models_verified": false,
  "target_host_verified": false,
  "graph_profile_enabled_by_default": false
}
```

This record captures the selected source and locked artifacts. The prescribed application build is reported below when run. No import, service, provider, graph protocol, restart, or 2-core/8-GiB capacity acceptance is inferred from lock resolution or a build.

## Selected software and image

Graphiti Core 0.30.2 uses the FalkorDB backend through its supported `falkordb` extra. The Python environment retains Redis 5.3.1, ARQ 0.28.0, OpenAI 2.54.0, Pydantic 2.13.5 and HTTPX 0.28.1. Nine new distributions are locked; the generated lock resolved 100 packages and reported only the nine additions below. Source and metadata constraints were reviewed, but actual Python import and graph/server API compatibility remain deferred.

| Distribution | Locked version | License observation | Selected artifact SHA256 |
| --- | --- | --- | --- |
| graphiti-core[falkordb] | 0.30.2 | Apache-2.0 | `97674a49514130db175faecf23221ce9338240be3cbe13a7e23e5a5033892b9f` |
| falkordb | 1.2.0 | MIT | `7572d9cc377735d22efc52fe6fe73c7a435422c827b6ea3ca223a850a77be12e` |
| neo4j | 5.28.3 | Apache-2.0; Python dependency only | `dbf6d9211b861bc3dd62dccbf8a74d1e33e0c602084dd123b753edf46e1fdfad` |
| pytz | 2026.4 | MIT | `9d514388fbc89ca0833203464272ac485b8828568ab73532f5020f17e892a0ff` |
| numpy | 2.5.3 | BSD-3-Clause, 0BSD, MIT, Zlib, CC0-1.0 | Linux CPython 3.12 x86_64: `b7e18c623bb5c95acb3b3328861272816ba199fb531921c5d6d0b675f1fde9e3`; aarch64: `76c2c1e6bfa5c84adc6434dfbf013aa92096a7985221762c8f11fedfd20fff58` |
| tenacity | 9.1.4 | Apache-2.0 | `6095a360c919085f28c6527de529e76a06ad89b23659fa881ae0649b867a9d55` |
| posthog | 7.61.1 | MIT | `fa391cbb333dc3571112eacdf69616470929410d915a6ccce32ae9b4322708cb` |
| backoff | 2.2.1 | MIT | `63579f9a0628e06278f7e47b7d7d5b6ce20dc65c5e96a6f3ca99a6adca0396e8` |
| python-dateutil | 2.9.0.post0 | Apache-2.0 / BSD-3-Clause | `a8b2bc7bffae282281c8140a97d3aa9c14da0b136dfe83f850eea9a5f7470427` |

Hashes are selected official PyPI artifact identifiers recorded in `uv.lock`; they were not verified against locally downloaded wheel bytes. NumPy has architecture-specific wheels recorded in the lock. Package notices and intended use are listed in `OSS_USED.md`. The older FalkorDB client and backoff releases have not received a complete advisory review; no claim of advisory absence is made.

The isolated optional service uses `docker.io/falkordb/falkordb:v4.22.0@sha256:4ac83f55062d364dcf151692635138b0bd2e12578019a56eb86ef53d067b116b`. The official multi-platform index reported linux/amd64 child `sha256:ffc97adebdd027ec71ddf7143f38d95c65846d6abd38404ab452873d045db37b` and linux/arm64 child `sha256:f36b2d00c8b88c0b3a76e56d534638540de549ce8b2a6f66cfa815b6e29d70e4`; amd64 image config was `sha256:8df4e2b5318fefe21c1eb7f8faa2b59d58aac82cbf3549a9d85279dc31df84d1`. The upstream image license is SSPL-1.0. Preparation is scoped to private single-owner self-hosting; distribution or third-party service use requires a separate SSPL obligations review. The server's Redis version is separate from the application Redis client and queue service.

`infrastructure/graph/compose.yml` is opt-in under the `graph` profile, publishes no host port, disables the bundled browser, requires a dedicated graph password, persists into its own `graph-data` volume and applies 1-CPU/1536-MiB, 1-GiB `noeviction`, four queued queries, 1000 result rows and 5000-ms query limits. It does not share the ARQ Redis volume. The default application compose startup does not include this file or enable the profile. Runtime activation remains disabled until deferred acceptance.

## Reviewed source contracts

- Graphiti 0.30.2 `Graphiti.__init__` accepts injected `graph_driver`, `llm_client`, `embedder` and `cross_encoder`; omitted clients can construct direct OpenAI defaults.
- `LLMClient.generate_response` performs Graphiti's prompt/schema/language normalization and disk-cache path. The adapter leaves that public behavior in place while overriding the retry seam so retries remain bounded by ModelGateway. `_generate_response` consumes `Message` objects and returns a parsed dictionary; structured values are validated against the supplied Pydantic model.
- `EmbedderClient.create`/`create_batch` consume text inputs and return vectors. The adapter rejects unsupported token iterables, validates ordered indexes, finite nonzero vector values and configured dimensions. No default embedding dimension is assumed.
- `CrossEncoderClient.rank(query, passages)` returns passage/score pairs. The adapter routes this call through the configured ModelGateway reranking capability and validates score indexes and values.
- `Graphiti.add_episode(..., uuid=...)` reads an existing `EpisodicNode`; it does not use the supplied UUID to create an arbitrary new node. The adapter persists an identifiable UUID shell before extraction, and existing IDs require owner reconciliation instead of blind re-extraction. Graph writes span non-atomic statements; timeouts and cancellation report an unknown outcome.
- Graphiti's `remove_episode` can remove an edge whose first supporting episode is deleted even if later support remains. The adapter accepts bounded ordered receipts and current typed recovery actions under the owner fence, retaining the union of original entity, mention and fact IDs. Support subtraction uses an exact-ID/group/endpoint query that changes only `edge.episodes`; zero-support edges are deleted after full vector-aware state rechecks. Final reads require every latest intended survivor to be present with its full expected state and every authorized deleted fact to be absent. Missing rows remain unresolved without receipt-proved absence or a current owner-authorized deletion action. Shared facts identified by an owned cleanup receipt remain pending across retries even when current support already excludes the deleted episode. Support-absent validity changes remain pending when proved by the extraction history or a cleanup receipt; only a current owner-authorized replacement/deletion proof clears those obligations. Current node retain/replacement/deletion decisions are reauthorized and exact-read before their IDs leave the pending set. Episode removal remains distinct from full source purge or recomputation readiness.
- In Graphiti Core 0.30.2, `add_nodes_and_edges_bulk` calls `session.execute_write(add_nodes_and_edges_bulk_tx, episodic_nodes, episodic_edges, entity_nodes, entity_edges, embedder, driver=driver)`. The adapter intercepts only that pinned Falkor callable, captures its four structured write calls in memory under an aggregate byte bound, validates the exact phase and query shape, and preserves the pinned query payloads. It journals the known IDs and prior node state first, then the final hydrated node state and fact support before flushing any captured query. Capture/materialization failures dispatch no bulk writes; failures after flush begins have a complete prewrite receipt. No query text or raw payload is persisted in receipts. This is a multi-statement best-effort operation, not an atomic transaction; T3 must retain the ordered receipt history and owner reconciliation must verify exact IDs.
- Node fingerprints include group and identity, physical labels, creation time, normalized attributes and the persisted name vector. New writer intents explicitly include Graphiti's mandatory `Entity` label; persisted reads must actually contain that label or fail closed. Existing-node replacements carry the exact current physical label set and `created_at`, because Falkor's pinned `SET n:...` is additive and property replacement does not change labels. Absent-node recovery replacement requires a stable owner-selected `replacement_created_at` under the current callback; no timestamp is inferred from narrative/event data. The public node read is followed by Graphiti's pinned `load_name_embedding` operation. Numeric vectors are dimension-checked and normalized to IEEE float32 before hashing to match Falkor's `vecf32` storage; unsupported vector representations fail closed. These writer/read semantics are source-verified; runtime storage representation remains deferred.
- Fact fingerprints retain complete genuine attributes and vectors while validating Graphiti's generated endpoint aliases (`source_node_uuid`/`target_node_uuid` from bulk writes and `source_uuid`/`target_uuid` from single saves) against the exact relationship endpoints, then project either spelling as the same topology pair. Other reserved graph-property names are rejected on fresh replacement attributes. Intended values follow the pinned Falkor driver's recursive UTC datetime serialization and string-value NUL stripping; naive datetimes, non-JSON values and mismatched aliases fail closed. Support subtraction mutates only the `episodes` property after full prior-state checks, then reloads the vector and verifies the complete normalized state.
- Canonical bindings provide owner-selected graph UUIDs and optional source-proved field seeds. For a missing node, a nonblank name must match the supplied name-field SHA256 and exact membership support; the adapter journals its UUID and intended node-state fingerprint before `EntityNode.save`, then reads the UUID back and verifies its group and fingerprint. Existing-node reuse also requires an owner-mapped prior state fingerprint. Receipts remove seed text while retaining bounded field hashes and supporting IDs. Graphiti-extracted candidates never become canonical by inference. Candidate recovery carries a detached exact UUID/group replacement with surviving field hashes/support and no canonical identity; the owner callback must authorize current support before retain, delete or replacement. Global entity names are not sent to model extraction.
- Node and fact recovery use bounded exact IDs, complete incident-link inventories and exact state fingerprints. `OperationAuthorization` may carry ephemeral bounded `node_recovery_actions` and `fact_recovery_actions`; delete retries call the existing owner callbacks before graph inspection and again before terminal verification. These callbacks authorize the live recovery lease, prior-dispatch cessation and current support/manual/canonical decisions; receipts continue to retain only IDs, hashes and support. Fact correction can delete a support-absent stale fact or replace it from a fresh owner-approved snapshot; it never reconstructs fact text or validity from a digest or episode time. Unknown, incomplete or mismatched inventory remains pending.
- Search constructs the pinned nested `DateFilter` predicates for `valid_at <= instant` and `(invalid_at > instant OR invalid_at IS NULL)` and passes the same group-partition driver used by writes. The comparison is half-open at the invalidation boundary. The selected predicate does not infer transaction-time policy from `expired_at`.
- Replacing node metadata writes only fresh source-supported name/summary, preserves the exact current physical labels and `created_at`, and explicitly clears the old name embedding; the returned state fingerprint verifies those fields and the cleared vector. The mapping remains pending until a separately authorized hydration produces a current permitted embedding.
- Episode `reference_time` is the explicit narrative chronology supplied to Graphiti and anchors the shell's `valid_at`; shell `created_at` is the actual processing time. Canonical fact validity remains separate, including unknown precision, and is not inferred from episode observation time.
- FalkorDriver's constructor schedules index setup when constructed inside a running loop; the adapter constructs it in a worker thread without a loop, then explicitly awaits non-destructive setup. FalkorDriver's upstream `execute_query` logs Cypher and parameters and `health_check` prints exceptions; the protected driver path bounds and redacts those query paths.
- Lifecycle transitions serialize under one lock. `READY` is returned only with an adopted driver; initialization closes a stale driver before retry. Timeout or cancellation joins constructor and close tasks before local ownership is released; no hard wall-clock join bound is claimed. Close clears readiness and retains the driver handle if transport shutdown raises. The worker keeps shared heavy capacity through awaited publication and cleanup, while database connection loss remains a separate ownership limitation. Failed-close handle retry after the job exits is not established by this source review.
- Graphiti resolves the requested group into a driver clone. The adapter uses that same protected per-group clone for live episode inventory, index setup and exact-ID mutations. Search requires exact equality with the bounded owner inventory; only a target authorized by the owner for upsert, delete or graph-only reconcile may be absent. The owner callback proves canonical support readiness, exact current source/document authorization, tombstone or receipt ownership, and operation-specific partial-write state. Reconcile inspection performs no model calls. A recent ten-episode history is not a substitute for the complete bounded partition closure.

Official source references:

- [Graphiti v0.30.2 `graphiti.py`](https://github.com/getzep/graphiti/blob/v0.30.2/graphiti_core/graphiti.py)
- [Graphiti v0.30.2 Falkor driver](https://github.com/getzep/graphiti/blob/v0.30.2/graphiti_core/driver/falkordb_driver.py)
- [Graphiti v0.30.2 LLM client](https://github.com/getzep/graphiti/blob/v0.30.2/graphiti_core/llm_client/client.py)
- [FalkorDB v1.2.0 client](https://github.com/FalkorDB/falkordb-py/tree/v1.2.0)
- [FalkorDB v4.22.0 server release](https://github.com/FalkorDB/FalkorDB/releases/tag/v4.22.0)
- [FalkorDB v4.22.0 run script](https://github.com/FalkorDB/FalkorDB/blob/v4.22.0/build/docker/run.sh)
- [FalkorDB v4.22.0 license](https://github.com/FalkorDB/FalkorDB/blob/v4.22.0/LICENSE.txt)

## Build evidence

The prescribed `./scripts/dev.ps1 build` result is recorded in the bounded source-repair report. Build success checks the affected production deliverables only; it does not establish provider behavior, graph protocol interoperability, startup/restart, license clearance for a changed deployment scope or target-host capacity.

## Deferred gates

- Verify actual FalkorDB 1.2.0 client and v4.22.0 server connection, authentication, RESP2, index behavior, query/filter semantics, episode upsert/search/delete and restart.
- Verify configured OmniRoute structured output, embeddings, reranking and embedding dimension; keep telemetry disabled and enforce privacy/revocation on every retry.
- Verify exact support mappings, episode reprocessing, partial writes, shared-edge deletion, tombstones, reconciliation and stale replay prevention.
- Runtime activation remains blocked until T3 persists every ordered phase receipt, owner-proved node seeds and mapping fingerprints, cleanup history, and live-lease recovery authorization. `inspect_write_receipt` reports bounded exact-ID state and links; it does not grant permission to publish or replay graph content.
- Review resolved artifact notices and current advisories, including the older client/backoff maintenance tradeoff.
- Measure the enabled stack on the specified 2-core/8-GiB target. Until all gates pass, keep `GRAPH_ENABLED=false` and the graph compose profile off.
