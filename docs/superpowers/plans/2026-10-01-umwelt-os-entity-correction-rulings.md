# P04-T3 correction owner rulings

Accepted entry: P04-T2 commit `4f5ee2e19a01422cc935076d28c853d163ee64b3`, following integrated T1/RN1 `56b4e0d`. Production patch SHA256 `CC9BE22C6590502E4CBF01891682A21CF6645933A933DF6B7A83DB233CBDCF49` matches the reviewed freeze. The original task/spec remains authoritative.

## Relationship supports

Extend existing citation support uniqueness to the exact source/target evidence membership pair. Preserve distinct pairs with the same version/chunk; no second binding table is needed. Define legacy NULL semantics in the forward migration and owner queries. Unknown endpoint bindings require a controlled split conflict. Update actual manual/extraction writers, reassignment, cleanup and DTO readers together. Count distinct version/chunk citations for corroboration; confidence remains SQL MAX over valid support. No historical migration rewrites.

## Persistent decisions and selectors

Persist consumed owner assignment/suppression decisions and canonical redirects. New extracted memberships receive a deterministic nonplaintext fingerprint from actual normalized candidate type/value with explicit source/document/evidence dependencies. Response-local keys and current mutable entity names are not historical selectors. Missing legacy fingerprints remain unknown, without fabricated backfill.

Default selection is the explicitly chosen immutable evidence. Applying a rule to future document revisions requires explicit owner confirmation of document scope and a usable fingerprint or bounded owner-provided selector. Do not silently broaden it to other sources/documents or promote names to verified identity. Conflicting rules, same-selector candidate ambiguity and changed post-lock decisions return review/conflict. Store hashes/IDs without copying source text; dependent rules are removed with document/source forgetting. Independent owner identity and redirect IDs may survive without cached source text.

Consume decisions/redirects before sorted entity locking and during authoritative post-lock refresh, plus relevant publication. Changed targets never acquire late entity locks. External identity is unavailable without actual verified namespaced owner/source facts; document/provider IDs are not entity identity.

## Correction transaction

Discover a bounded complete closure before mutation: memberships, aliases/field supports, relationship supports/equality candidates, neighbors and document/source dependencies. Fit initial atomic limits to the existing owner caps (100 entity/evidence refs, 200 membership refs); oversized operations return conflict, never partial correction. Lock globally sorted source/document dependencies through the document owner, then the complete sorted entities, then relationships/supports. Revalidate revisions, retained evidence and closure completeness. Paused/archived historical evidence follows owner library policy, not remote extraction gates.

Merge preserves membership UUIDs, target owner-selected identity and redirectable old IDs. Unresolved protected-value/alias conflicts and self edges return explicit conflict. Split moves only selected UUIDs and their actual support bindings. Use noncommitting relationship-owner commands, authenticated `AuthSession.owner_id`, bounded reason/server UTC and one final `commit_with_replay`. Evidence/preview/conflict DTOs serve T4; no new service or generic policy engine.

Add a forward migration after `0008_entity_extraction`; register actual models. Code/build and independent review precede commit. Tests/runtime/migration execution/provider/capacity acceptance remain deferred.

## P04-T4 consumed resolution-review assignment

Accepted T3 entry fe6207a. The existing lossy review rows (candidate name/reason/possible IDs) cannot authorize assignment. Future extraction results retain a bounded immutable typed candidate snapshot with opaque persisted candidate ID, actual type/value fingerprint, exact chunk IDs, confidence and original work-local retry locator; expose work/result IDs and expected snapshot digest. A response key only locates its locked durable result and never becomes cross-extraction historical authority. Legacy rows remain explicitly non-actionable; no inferred identity/backfill.

The consumed owner assignment command selects that persisted snapshot, validates retained exact evidence through public owners, source generation, snapshot and canonical target revision/type/terminal state, and follows source/document/entity/work/result lock order compatible with extraction. Materialize the exact memberships, persist existing evidence-scoped assignment decisions, mark review state and audit under one caller-owned replay transaction. Explicit future-document scope remains opt-in. Do not globally confirm an alias, overwrite protected owner fields or nest committing commands. Manual retained paused/archived evidence follows library policy; remote-send active/privacy gates are not manual-read authority.

Worker reconciliation must honor a valid exact explicit owner assignment despite fuzzy-resolution review, while retaining all F4-R2 persisted/current selector multiplicity conflicts, post-lock refresh and no-late-target-lock invariants. This narrow writer serves T4 UI; no generic unused registry or additional persistence layer unless existing durable result storage cannot preserve these invariants.

Relationship-review rows are typed and cannot be assigned as entity candidates. Legacy lossy rows cannot authorize reconstruction/publication. Fresh bounded snapshots retain relationship type, exact cited chunk/confidence and work-local endpoint locators bound to the same immutable result. A consumed local retry after endpoint assignments resolves persisted mappings and requires both exact canonical endpoint memberships for that version/chunk before calling the relationship owner, audit and one replay commit. Unresolved/deleted/ambiguous endpoints remain review; no invented evidence or automatic model call. Scheduling existing unique succeeded extraction work does not implement re-extraction. Legacy rows expose snapshot_unavailable and endpoint/document links without being marked solved. Retained evidence uses manual library policy; any actual remote send separately requires extraction consent/current gates.

## P04 duplicate-index source repair — 2026-10-02

The owner explicitly instructed proactive completion without further stops. Apply the narrow source repair to the unmerged P04 correction revision: remove its two duplicate membership-index CREATE calls and two matching DROP calls. The ancestor delta remains the sole owner of those identical indexes. Preserve all revision IDs, ancestry, constraints, schema/data operations and previously shipped migration files. This exception to the earlier blanket no-rewrite ruling concerns only the four erroneous source calls in the P04 branch; it does not authorize running migrations, altering a database or assuming application history.

Application history remains unknown. Record that fact and require the actual upgrade/downgrade/restore compatibility checks in the deferred validation stage; builds and source review cannot prove those checks. No IF NOT EXISTS masking, bridge revision, data rewrite, database reset or deployment is required to repair this source defect. Proceed with code/build/review and phase integration; do not invent a never-applied claim or stop routine source work on that missing runtime fact.
