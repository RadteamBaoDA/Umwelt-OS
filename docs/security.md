# Security, deletion and recovery boundaries

Source baseline: develop `98365a1`, 2026-10-06. P12 deletion/recovery implementation is incomplete. This document specifies recovery evidence and preserves existing owner safeguards; it does not report completed runtime acceptance.

## Access and owner boundaries

Use authenticated owner APIs and the existing CSRF dependency for mutations. A login grant and a data-collection grant are separate authorities. Tool/MCP actions retain their existing grant, approval, idempotency and remote-effect receipts. Source content and tool output are data, not authorization to run instructions. Never log credentials or private response payloads as recovery evidence.

Keep current upload size/archive/PDF, URL/SSRF and provider controls in their owning modules. Their live rejection behavior remains part of deferred acceptance. Internal databases, Docker socket and administrator-managed tool processes are private deployment interfaces; use the supported HTTPS proxy configuration in [deployment.md](deployment.md).

## Deletion completion

Immediate access revocation, canonical cleanup, raw-file cleanup and external graph convergence are distinct stages. Source generation/tombstone changes deny stale access before asynchronous cleanup. Connector-only removal preserves imported data; with-data removal requires owned derived copies to be cleaned. Shared facts remain when independent retained evidence supports them.

Source canonical cleanup now captures exact per-Document child receipts before cascade and delegates raw cleanup to the Documents owner (`fe601bc`), preserving shared-URI fences and retry identity. Its linked-child progress is separate from complete historical and Source-local copied-owner coverage. Individual Document deletion now records an owner-only durable operation and outbox event before canonical commit, with independent record/graph-tombstone/raw status, exact shared-URI publication fencing and retryable contained unlink (`3d2c51e`). This is accepted code/build/source review, not physical-erasure runtime proof. Bounded Chat copied-evidence cleanup is integrated at `f6da2e2`: immutable version/chunk identities are captured before cascade, cleanup runs without a raw file, structured copies are scrubbed, and current reads/SSE replay/model request opening/publication revalidate exact evidence. Raw, Chat and aggregate copy status remain distinct; Chat success does not mark the aggregate complete. Document deletion receipt UI is integrated (`4416843`); Memory, Agent, Dashboard, Notification and Automation copied owners, historical same-source receipts and Source-local Memory coverage still require P12 implementation. Zero current Documents does not prove all historical copies are erased. A source operation marked successful must not be used as evidence that an external graph is absent. Disabled or unavailable external components cannot establish erasure.

Conversation deletion preserves independent action-effect tombstones and uncertainty. Memory bulk history cleanup now uses the accepted Chat public deletion contract under the Memory privacy lock (`94c7c1b`). Expired Chat worker cleanup is integrated at `32a6ccc`: bounded candidate batches, fresh expiration and identity checks, Memory → Chat → Agent lock order, pinned ephemeral TTL, and persistent-parent retention for run-only expiry. The returned cleanup count includes cascaded stream and mutation-receipt rows. Its prescribed production build and independent source reviews passed; runtime expiry/concurrency acceptance remains deferred. Privacy consent fences are integrated, but consent fencing alone does not close the deletion task.

Already delivered provider/client bytes cannot be recalled. Live deletion does not erase independently encrypted historical backups; archive retention and protected-key handling are separate controls.

## Recovery evidence matrix

The following are required deferred checks, not passed checks. Use disposable data and preserve owner volumes; commands must come from the final accepted implementation.

| Failure boundary | Required durable evidence | Required recovery/visibility | Current qualification |
| --- | --- | --- | --- |
| API/worker dies before canonical deletion commit | Transaction absent or rolled back | Existing owner data stays consistent; retry authenticated request with its identity | Runtime pending |
| Worker dies after canonical commit, before raw unlink | Tombstone/generation and exact raw-cleanup intent | Retrieval denied; retry only captured owned file cleanup | Source and individual-Document journals exist; runtime pending |
| Raw cleanup fails or storage is unavailable | Error and pending retry identity without private content | Canonical deletion remains effective; physical cleanup stays pending | Source retry exists; runtime pending |
| Graph dies before/after deletion dispatch | Exact scope, desired/applied revisions and durable worker intent | Resume reconciliation; retain independently supported facts | Existing temporal recovery; cross-stage status integration pending |
| Redis is lost | PostgreSQL intent/event/run and effect receipts | Recreate execution from durable identities, without replaying uncertain remote effects | Runtime pending |
| Agent/automation dies around remote effect | Attempt/result/uncertainty receipt and approval identity | Reconcile requires-review effects; never assume absent response means no effect | Runtime pending |
| SSE reconnects after consent/deletion change | Current consent/evidence fence and terminal event identity | Redact/cancel stale copied payloads; client refetches authorized state | Privacy fences integrated; full deletion redaction pending |
| n8n becomes unavailable during backup recovery | Original workflow states and attempt/result journal | Keep recovery visibly incomplete until required reconciliation finishes | Backup/recovery source integrated at 5212def; runtime pending |
| Restore fails checksum/key/schema/component readiness | Manifest and explicit failed component evidence | Preserve isolated restore state; do not claim production restoration | Isolated restore identity/archive/readiness source integrated at 5212def; runtime pending |

## Release evidence

Record exact source revision, configured capabilities and receipt identities without secrets. Keep failure, pending and uncertain states explicit. Use [release-checklist.md](release-checklist.md), [performance-report.md](performance-report.md) and the execution ledger for final gates. Container builds and source review do not prove service recovery, erasure, target capacity or successful restore.

