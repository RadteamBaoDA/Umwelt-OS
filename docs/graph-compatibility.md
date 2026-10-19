# Graphiti and Neo4j compatibility record

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

Graphiti Core 0.30.2 uses its Neo4j backend through the `neo4j` Python driver 5.28.3 (Apache-2.0, already a mandatory Graphiti dependency). Redis 5.3.1, ARQ 0.28.0, OpenAI 2.54.0, Pydantic 2.13.5 and HTTPX 0.28.1 are retained. Hashes for the locked distributions are recorded in `uv.lock`; they were not verified against locally downloaded wheel bytes, and no claim of advisory absence is made. Notices and intended use are listed in `OSS_USED.md`.

| Distribution | Locked version | License observation |
| --- | --- | --- |
| graphiti-core | 0.30.2 | Apache-2.0 |
| neo4j (Python driver) | 5.28.3 | Apache-2.0 |

The isolated optional service uses `neo4j:5.26.2-community@sha256:099b9f74968c123209972835417985ed2a1cc19c0422c0753a313e26a736c365` in the `graph` profile of `docker-compose.base.yml`. The server is GPLv3 (see the image's `NOTICE.txt`) and is run unmodified; private single-owner self-hosting is the selected scope, and any redistribution requires a separate GPL review.

Service and adapter bounds:

- Single database: Community edition has one user database (`neo4j`); partitions are isolated by the `group_id` property, not by database. There is no RBAC.
- Each query runs in an explicit Bolt transaction with a 5-second transaction timeout and a 100-row result cap. The adapter performs no retries; Bolt transaction metadata carries the dispatch token used to prove cessation of a prior dispatch.
- The service persists into its own `graph-data` volume, runs as uid 7474, publishes Bolt on loopback only, and has `stop_grace_period: 60s` so it checkpoints cleanly on stop. The default compose startup does not enable the profile.
- Password rotation: `NEO4J_AUTH` only applies to an empty `/data`; later changes to `GRAPH_PASSWORD` require `ALTER USER` inside the running database and a matching `.env` update.

## Reviewed source contracts

- Graphiti 0.30.2 `Graphiti.__init__` accepts injected `graph_driver`, `llm_client`, `embedder` and `cross_encoder`; omitted clients can construct direct OpenAI defaults.
- `LLMClient.generate_response` performs Graphiti's prompt/schema/language normalization and disk-cache path. The adapter leaves that public behavior in place while overriding the retry seam so retries remain bounded by ModelGateway. `_generate_response` consumes `Message` objects and returns a parsed dictionary; structured values are validated against the supplied Pydantic model.
- `EmbedderClient.create`/`create_batch` consume text inputs and return vectors. The adapter rejects unsupported token iterables, validates ordered indexes, finite nonzero vector values and configured dimensions. No default embedding dimension is assumed.
- `CrossEncoderClient.rank(query, passages)` returns passage/score pairs. The adapter routes this call through the configured ModelGateway reranking capability and validates score indexes and values.
- `Graphiti.add_episode(..., uuid=...)` reads an existing `EpisodicNode`; it does not use the supplied UUID to create an arbitrary new node. The adapter persists an identifiable UUID shell before extraction, and existing IDs require owner reconciliation instead of blind re-extraction. Graph writes span non-atomic statements; timeouts and cancellation report an unknown outcome.
- Graphiti's `remove_episode` can remove an edge whose first supporting episode is deleted even if later support remains. The adapter accepts bounded ordered receipts and current typed recovery actions under the owner fence, retaining the union of original entity, mention and fact IDs. Support subtraction uses an exact-ID/group/endpoint query that changes only `edge.episodes`; zero-support edges are deleted after full vector-aware state rechecks. Final reads require every latest intended survivor to be present with its full expected state and every authorized deleted fact to be absent. Missing rows remain unresolved without receipt-proved absence or a current owner-authorized deletion action. Shared facts identified by an owned cleanup receipt remain pending across retries even when current support already excludes the deleted episode. Support-absent validity changes remain pending when proved by the extraction history or a cleanup receipt; only a current owner-authorized replacement/deletion proof clears those obligations. Current node retain/replacement/deletion decisions are reauthorized and exact-read before their IDs leave the pending set. Episode removal remains distinct from full source purge or recomputation readiness.
- In Graphiti Core 0.30.2, `add_nodes_and_edges_bulk` calls `session.execute_write(add_nodes_and_edges_bulk_tx, episodic_nodes, episodic_edges, entity_nodes, entity_edges, embedder, driver=driver)`. The adapter intercepts only that pinned Graphiti callable, captures its four structured write calls in memory under an aggregate byte bound, validates the exact phase and query shape, and preserves the pinned query payloads. It journals the known IDs and prior node state first, then the final hydrated node state and fact support before flushing any captured query. Capture/materialization failures dispatch no bulk writes; failures after flush begins have a complete prewrite receipt. No query text or raw payload is persisted in receipts. This is a multi-statement best-effort operation, not an atomic transaction; T3 must retain the ordered receipt history and owner reconciliation must verify exact IDs.
- Node fingerprints include group and identity, physical labels, creation time, normalized attributes and the persisted name vector. New writer intents explicitly include Graphiti's mandatory `Entity` label; persisted reads must actually contain that label or fail closed. Existing-node replacements carry the exact current physical label set and `created_at`, because the pinned `SET n:...` is additive and property replacement does not change labels. Absent-node recovery replacement requires a stable owner-selected `replacement_created_at` under the current callback; no timestamp is inferred from narrative/event data. The public node read is followed by Graphiti's pinned `load_name_embedding` operation. Numeric vectors are dimension-checked and normalized to IEEE float32 before hashing for deterministic hashing; unsupported vector representations fail closed. These writer/read semantics are source-verified; runtime storage representation remains deferred.
- Fact fingerprints retain complete genuine attributes and vectors while validating Graphiti's generated endpoint aliases (`source_node_uuid`/`target_node_uuid` from bulk writes and `source_uuid`/`target_uuid` from single saves) against the exact relationship endpoints, then project either spelling as the same topology pair. Other reserved graph-property names are rejected on fresh replacement attributes. Intended values follow the pinned Graphiti driver's recursive UTC datetime serialization and string-value NUL stripping; naive datetimes, non-JSON values and mismatched aliases fail closed. Support subtraction mutates only the `episodes` property after full prior-state checks, then reloads the vector and verifies the complete normalized state.
- Canonical bindings provide owner-selected graph UUIDs and optional source-proved field seeds. For a missing node, a nonblank name must match the supplied name-field SHA256 and exact membership support; the adapter journals its UUID and intended node-state fingerprint before `EntityNode.save`, then reads the UUID back and verifies its group and fingerprint. Existing-node reuse also requires an owner-mapped prior state fingerprint. Receipts remove seed text while retaining bounded field hashes and supporting IDs. Graphiti-extracted candidates never become canonical by inference. Candidate recovery carries a detached exact UUID/group replacement with surviving field hashes/support and no canonical identity; the owner callback must authorize current support before retain, delete or replacement. Global entity names are not sent to model extraction.
- Node and fact recovery use bounded exact IDs, complete incident-link inventories and exact state fingerprints. `OperationAuthorization` may carry ephemeral bounded `node_recovery_actions` and `fact_recovery_actions`; delete retries call the existing owner callbacks before graph inspection and again before terminal verification. These callbacks authorize the live recovery lease, prior-dispatch cessation and current support/manual/canonical decisions; receipts continue to retain only IDs, hashes and support. Fact correction can delete a support-absent stale fact or replace it from a fresh owner-approved snapshot; it never reconstructs fact text or validity from a digest or episode time. Unknown, incomplete or mismatched inventory remains pending.
- Search constructs the pinned nested `DateFilter` predicates for `valid_at <= instant` and `(invalid_at > instant OR invalid_at IS NULL)` and passes the same group-partition driver used by writes. The comparison is half-open at the invalidation boundary. The selected predicate does not infer transaction-time policy from `expired_at`.
- Replacing node metadata writes only fresh source-supported name/summary, preserves the exact current physical labels and `created_at`, and explicitly clears the old name embedding; the returned state fingerprint verifies those fields and the cleared vector. The mapping remains pending until a separately authorized hydration produces a current permitted embedding.
- Episode `reference_time` is the explicit narrative chronology supplied to Graphiti and anchors the shell's `valid_at`; shell `created_at` is the actual processing time. Canonical fact validity remains separate, including unknown precision, and is not inferred from episode observation time.
- The Graphiti Neo4j driver's constructor may schedule index setup when constructed inside a running loop; the adapter constructs it in a worker thread without a loop, then explicitly awaits non-destructive setup. The upstream driver logs Cypher and parameters; the protected driver path bounds and redacts those query paths.
- Lifecycle transitions serialize under one lock. `READY` is returned only with an adopted driver; initialization closes a stale driver before retry. Timeout or cancellation joins constructor and close tasks before local ownership is released; no hard wall-clock join bound is claimed. Close clears readiness and retains the driver handle if transport shutdown raises. The worker keeps shared heavy capacity through awaited publication and cleanup, while database connection loss remains a separate ownership limitation. Failed-close handle retry after the job exits is not established by this source review.
- Graphiti resolves the requested group into a driver clone. The adapter uses that same protected per-group clone for live episode inventory, index setup and exact-ID mutations. Search requires exact equality with the bounded owner inventory; only a target authorized by the owner for upsert, delete or graph-only reconcile may be absent. The owner callback proves canonical support readiness, exact current source/document authorization, tombstone or receipt ownership, and operation-specific partial-write state. Reconcile inspection performs no model calls. A recent ten-episode history is not a substitute for the complete bounded partition closure.

Official source references:

- [Graphiti v0.30.2 `graphiti.py`](https://github.com/getzep/graphiti/blob/v0.30.2/graphiti_core/graphiti.py)
- [Graphiti v0.30.2 Neo4j driver](https://github.com/getzep/graphiti/blob/v0.30.2/graphiti_core/driver/neo4j_driver.py)
- [Graphiti v0.30.2 LLM client](https://github.com/getzep/graphiti/blob/v0.30.2/graphiti_core/llm_client/client.py)
- [neo4j Python driver 5.28](https://github.com/neo4j/neo4j-python-driver/tree/5.28)

## Build evidence

The prescribed `./scripts/dev.ps1 build` result is recorded in the bounded source-repair report. Build success checks the affected production deliverables only; it does not establish provider behavior, graph protocol interoperability, startup/restart, license clearance for a changed deployment scope or target-host capacity.

## Deferred gates

- Verify live Neo4j 5.26.2 Community connection and authentication, index behavior, query/filter semantics, episode upsert/search/delete, kill-mid-dispatch cessation, read-mode rejection, and restart.
- Verify configured connector structured output, embeddings, reranking and embedding dimension; keep telemetry disabled and enforce privacy/revocation on every retry.
- Verify exact support mappings, episode reprocessing, partial writes, shared-edge deletion, tombstones, reconciliation and stale replay prevention.
- Runtime activation remains blocked until T3 persists every ordered phase receipt, owner-proved node seeds and mapping fingerprints, cleanup history, and live-lease recovery authorization. `inspect_write_receipt` reports bounded exact-ID state and links; it does not grant permission to publish or replay graph content.
- Review resolved artifact notices and current advisories, including the older client/backoff maintenance tradeoff.
- Measure the enabled stack on the specified 2-core/8-GiB target. Until all gates pass, keep `GRAPH_ENABLED=false` and the graph compose profile off.
