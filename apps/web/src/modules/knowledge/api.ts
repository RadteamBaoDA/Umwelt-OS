import { apiRequest, csrfHeaders } from '@/core/api';

export type Document = {
  id: string; source_id: string; external_id: string | null; title: string;
  content_type: string | null; mime_type: string | null; raw_uri: string | null;
  canonical_url: string | null; author: string | null; metadata: Record<string, unknown>;
  current_version: number; content_hash: string; published_at: string | null;
  observed_at: string | null; language: string | null; created_at: string; updated_at: string;
};
export type DocumentVersion = { id: string; document_id: string; version_number: number; content: string; content_hash: string; observed_at: string; created_at: string };
export type CitationTarget = { document_id: string; document_version_id: string; version_number: number; chunk_id: string; title: string; excerpt: string; observed_at: string };
export type DocumentPage = { items: Document[]; next_cursor: string | null };
/** Durable owner receipt returned by the accepted document deletion and its status read. */
export type DocumentDeletionReceipt = {
  operation_id: string;
  status: 'queued' | 'running' | 'succeeded' | 'failed';
  record_status: 'deleted';
  graph_status: 'tombstoned';
  raw_status: 'queued' | 'not_present' | 'retained_shared' | 'succeeded' | 'failed';
  evidence_scope_status: 'capturing' | 'captured' | 'unavailable';
  copied_status: 'queued' | 'running' | 'succeeded' | 'failed';
  chat_status: 'queued' | 'running' | 'succeeded' | 'failed';
  chat_error_code: string | null;
  memory_status: 'queued' | 'running' | 'succeeded' | 'failed';
  memory_error_code: string | null;
  memory_unresolved_count: number;
  memory_cache_pending: boolean;
  agent_status: 'queued' | 'running' | 'succeeded' | 'failed';
  agent_error_code: string | null;
  agent_unresolved_count: number;
  agent_waiting_for_lease: boolean;
  materialization_status: 'queued' | 'running' | 'succeeded' | 'failed';
  materialization_error_code: string | null;
  materialization_unresolved_count: number;
  brief_status: 'queued' | 'running' | 'succeeded' | 'failed';
  brief_error_code: string | null;
  brief_unresolved_count: number;
  immediate_access_revoked: true;
  error_code: string | null;
  copied_error_code: string | null;
};
export type GadgetDocumentProjection = {
  document_id: string; document_version_id: string; version_number: number; source_id: string;
  title: string; canonical_url: string | null; published_at: string | null; observed_at: string;
  excerpt: string; metadata_is_version_snapshot: boolean;
  read_at: string | null; bookmarked_at: string | null;
  provider_metadata: {
    provider: string;
    source_fields: Record<string, unknown>;
    telegram?: {
      channel_id: string; message_id: string; thread_id: string | null; reply_to_message_id: string | null;
      channel_label: string | null; channel_username: string | null; edited_received: boolean;
      published_at: string; edited_at: string | null;
      media: { kind: 'photo' | 'video' | 'audio' | 'voice' | 'document' | 'animation' | 'sticker' | 'other'; caption: string | null; count: number }[];
    } | null;
  } | null;
};
export type VersionPage = { items: DocumentVersion[]; next_cursor: string | null };
export type Entity = { id: string; type: string; name: string | null; canonical_name: string | null; description: string | null; name_origin: string | null; description_origin: string | null; metadata: Record<string, unknown>; revision: number; first_seen_at: string | null; last_seen_at: string | null; created_at: string; updated_at: string; aliases: { id: string; entity_id: string; alias: string; source_id: string | null; confirmed: boolean; origin: string; confidence: number | null; created_at: string }[] };
export type EntityPage = { items: Entity[]; next_cursor: string | null };
export type EntityEvidence = { id: string; entity_id: string; document_id: string; document_version_id: string; version_number: number; chunk_id: string; observed_at: string; extracted_at: string; confidence: number; source_id: string; title: string; canonical_url: string | null; metadata_is_version_snapshot: boolean; excerpt: string };
export type EntityEvidencePage = { items: EntityEvidence[]; next_cursor: string | null };
export type EntityCorrectionPreview = { operation: 'merge' | 'split'; entity_ids: string[]; membership_ids: string[]; relationship_ids: string[]; evidence_ref_count: number; conflicts: { code: string; message: string; entity_ids: string[]; membership_ids: string[]; relationship_ids: string[] }[] };
export type EntityCorrectionResult = { operation: 'merge' | 'split' | 'suppress'; entity_id: string; canonical_entity_id: string; replacement_entity_ids: string[]; revision: number; conflicts: Record<string, unknown>[] };
export const documentKeys = { all: ['documents'] as const, detail: (id: string) => ['documents', id] as const, versions: (id: string) => ['documents', id, 'versions'] as const };
/** Keeps deletion receipts in a cache namespace independent of removed documents. */
export const documentDeletionKeys = { detail: (id: string) => ['document-deletion-operations', id] as const };
export const entityKeys = { all: ['entities'] as const, list: (type?: string, q?: string) => ['entities', 'list', type ?? '', q ?? ''] as const, detail: (id: string) => ['entities', id] as const, evidence: (id: string) => ['entities', id, 'evidence'] as const, history: (id: string) => ['entities', id, 'history'] as const, /** Scopes entity-timeline pages to canonical identity and normalized display filters. */ timeline: (id: string, filters: Record<string, string>) => ['entities', id, 'timeline', filters] as const, neighbors: (id: string) => ['relationships', 'neighbors', id] as const, graphNeighbors: (id: string) => ['relationships', 'graph-neighbors', id] as const };

export type EntityHistoryPage = { items: { id: string; recorded_at: string; operation: string; affected_ids: string[]; revisions: Record<string, number> }[]; next_cursor: string | null; historical_values_available: false; memberships: EntityEvidence[]; membership_next_cursor: string | null };
export type GraphStatus = { mapping_id: string; document_version_id: string; episode_id: string | null; partition_id: string | null; status: string; desired_revision: number; applied_revision: number | null; error_code: string | null; graph_enabled: boolean; applied_at: string | null };
export type EntityTimelineResult = { canonical_entity_id: string; timeline: import('@/modules/timeline/api').TimelinePageResult; graph_statuses: GraphStatus[] };

/** Lists documents from the knowledge API in pages of 50 and appends the opaque cursor when provided. */
export function listDocuments(cursor?: string, sourceId?: string) { const query = new URLSearchParams({ limit: '50' }); if (cursor) query.set('cursor', cursor); if (sourceId) query.set('source_id', sourceId); return apiRequest<DocumentPage>(`/api/v1/documents?${query}`); }
/** Reads current active source records through the owner-validated dashboard projection. */
export function listGadgetDocumentProjections(sourceIds: string[], channelIds: string[] = []) {
  const query = new URLSearchParams({ limit: '100' });
  for (const sourceId of [...new Set(sourceIds)].slice(0, 32)) query.append('source_ids', sourceId);
  for (const channelId of [...new Set(channelIds)].slice(0, 32)) query.append('channel_ids', channelId);
  return apiRequest<{ items: GadgetDocumentProjection[]; next_cursor: string | null }>(`/api/v1/documents/dashboard-projections?${query}`);
}
/** Persist read/bookmark state for one current immutable version. */
export function setGadgetDocumentInteraction(
  documentId: string,
  versionNumber: number,
  payload: { read?: boolean; bookmarked?: boolean },
  csrfToken: string,
) {
  return apiRequest<{ document_version_id: string; read_at: string | null; bookmarked_at: string | null }>(
    `/api/v1/documents/${documentId}/versions/${versionNumber}/interaction`,
    { method: 'PUT', headers: { 'Content-Type': 'application/json', ...csrfHeaders(csrfToken) }, body: JSON.stringify(payload) },
  );
}
/** Fetches the document with the supplied ID from the knowledge API. */
export function getDocument(id: string) { return apiRequest<Document>(`/api/v1/documents/${id}`); }
/** Fetches the requested numbered document version. */
export function getVersion(id: string, number: number) { return apiRequest<DocumentVersion>(`/api/v1/documents/${id}/versions/${number}`); }
/** Resolves the exact retained citation chunk through Documents' owner-checked reader. */
export function getCitationTarget(id: string, versionId: string, chunkId: string) {
  const query = new URLSearchParams({ document_version_id: versionId, chunk_id: chunkId });
  return apiRequest<CitationTarget>(`/api/v1/documents/${id}/citation-target?${query}`);
}
/** Lists version history for the selected document. */
export function listVersions(id: string, cursor?: string) { return apiRequest<VersionPage>(`/api/v1/documents/${id}/versions?limit=50${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ''}`); }
/** Creates a document from the supplied source, title, and content using the supplied CSRF token. */
export function createDocument(payload: { source_id: string; title: string; content: string }, csrfToken: string) { return apiRequest<Document>('/api/v1/documents', { method: 'POST', headers: { 'Content-Type': 'application/json', ...csrfHeaders(csrfToken) }, body: JSON.stringify(payload) }); }
/** Updates a document’s title and metadata with the supplied CSRF token. */
export function updateDocument(id: string, payload: { title: string; metadata: Record<string, unknown> }, csrfToken: string) { return apiRequest<Document>(`/api/v1/documents/${id}`, { method: 'PATCH', headers: { 'Content-Type': 'application/json', ...csrfHeaders(csrfToken) }, body: JSON.stringify(payload) }); }
/** Replaces a document’s content using optimistic concurrency with the expected version and supplied CSRF token. */
export function updateContent(id: string, content: string, expectedVersion: number, csrfToken: string) { return apiRequest<Document>(`/api/v1/documents/${id}/content`, { method: 'PUT', headers: { 'Content-Type': 'application/json', ...csrfHeaders(csrfToken) }, body: JSON.stringify({ content, expected_version: expectedVersion }) }); }
/** Deletes a document and returns the durable cleanup receipt before its detail record disappears. */
export function deleteDocument(id: string, csrfToken: string) { return apiRequest<DocumentDeletionReceipt>(`/api/v1/documents/${id}`, { method: 'DELETE', headers: csrfHeaders(csrfToken) }); }

/** Reads one owner-authorized cleanup receipt with cancellation and a bounded request lifetime. */
export function getDocumentDeletionOperation(operationId: string, signal?: AbortSignal) {
  const timeout = AbortSignal.timeout(10_000);
  const requestSignal = signal ? AbortSignal.any([signal, timeout]) : timeout;
  return apiRequest<DocumentDeletionReceipt>(`/api/v1/documents/deletion-operations/${encodeURIComponent(operationId)}`, { signal: requestSignal });
}

/** Lists entities with optional cursor, type, and text filters. */
export function listEntities(cursor?: string, type?: string, q?: string) { const p = new URLSearchParams({ limit: '50' }); if (cursor) p.set('cursor', cursor); if (type) p.set('type', type); if (q) p.set('q', q); return apiRequest<EntityPage>(`/api/v1/entities?${p}`); }
/** Fetches the entity with the supplied ID. */
export function getEntity(id: string) { return apiRequest<Entity>(`/api/v1/entities/${id}`); }
/** Creates an entity with the supplied evidence rationale and CSRF token. */
export function createEntity(payload: { type: string; name: string; description?: string | null; aliases?: string[]; reason: string }, csrfToken: string) { return apiRequest<Entity>('/api/v1/entities', { method: 'POST', headers: { 'Content-Type': 'application/json', ...csrfHeaders(csrfToken) }, body: JSON.stringify(payload) }); }
/** Adds an alias to an entity with an explicit confirmation value and CSRF token. */
export function addEntityAlias(id: string, payload: { alias: string; confirmed: boolean; reason: string }, csrfToken: string) { return apiRequest<Entity>(`/api/v1/entities/${id}/aliases`, { method: 'POST', headers: { 'Content-Type': 'application/json', ...csrfHeaders(csrfToken) }, body: JSON.stringify(payload) }); }
/** Deletes the selected alias and records the supplied reason through the CSRF-protected API. */
export function deleteEntityAlias(id: string, aliasId: string, reason: string, csrfToken: string) { return apiRequest<void>(`/api/v1/entities/${id}/aliases/${aliasId}?reason=${encodeURIComponent(reason)}`, { method: 'DELETE', headers: csrfHeaders(csrfToken) }); }
/** Lists version-pinned evidence for an entity using optional cursor pagination. */
export function listEntityEvidence(id: string, cursor?: string) { const p = new URLSearchParams({ limit: '50' }); if (cursor) p.set('cursor', cursor); return apiRequest<EntityEvidencePage>(`/api/v1/entities/${id}/evidence?${p}`); }
/** Reads actual owner audit identifiers and currently permitted evidence using independent cursors. */
export function listEntityHistory(id: string, cursor?: string, membershipCursor?: string) { const p = new URLSearchParams({ limit: '50' }); if (cursor) p.set('cursor', cursor); if (membershipCursor) p.set('membership_cursor', membershipCursor); return apiRequest<EntityHistoryPage>(`/api/v1/entities/${id}/history?${p}`); }
/** Reads a canonical entity's event timeline and per-version graph worker status. */
export function getEntityTimeline(id: string, filters: { date_from?: string; date_to?: string; timezone: string; type?: string }, cursor?: string) { const p = new URLSearchParams({ limit: '50', timezone: filters.timezone }); for (const key of ['date_from', 'date_to', 'type'] as const) if (filters[key]) p.set(key, filters[key]!); if (cursor) p.set('cursor', cursor); return apiRequest<EntityTimelineResult>(`/api/v1/entities/${id}/timeline?${p}`); }
/** Reads actual graph synchronization outcomes for a bounded evidence-version batch. */
export function getGraphStatuses(versionIds: string[]) { const p = new URLSearchParams(); for (const id of [...new Set(versionIds)].slice(0, 100)) p.append('document_version_ids', id); return apiRequest<GraphStatus[]>(`/api/v1/system/graph/status?${p}`); }
/** Fetches a bounded page of an entity’s neighboring entities and relationships. */
export function getEntityNeighbors(id: string, cursor?: string, limit = 50) { const p = new URLSearchParams({ limit: String(limit) }); if (cursor) p.set('cursor', cursor); return apiRequest<{ items: { entity: { id: string; type: string; name: string | null; revision: number }; relationship: RelationshipView }[]; truncated: boolean; next_cursor: string | null }>(`/api/v1/entities/${id}/neighbors?${p}`); }
export type RelationshipView = { id: string; source_entity_id: string; target_entity_id: string; type: string; origin: 'owner' | 'derived'; confidence: number | null; valid_from: string | null; valid_to: string | null; validity_precision: 'bounded' | 'unknown'; evidence: RelationshipEvidence[] };
export type RelationshipEvidence = { id: string; relationship_id: string; document_id: string; document_version_id: string; version_number: number; chunk_id: string; observed_at: string; extracted_at: string; confidence: number; source_entity_membership_id: string | null; target_entity_membership_id: string | null; title: string; canonical_url: string | null; source_id: string; excerpt: string; metadata_is_version_snapshot: boolean };
export type RelationshipEvidencePage = { items: RelationshipEvidence[]; next_cursor: string | null };
export type RelationshipHistoryPage = { items: { id: string; source_entity_id: string; target_entity_id: string; type: string; origin: 'owner' | 'derived'; confidence: number | null; valid_from: string | null; valid_to: string | null; validity_precision: 'bounded' | 'unknown'; metadata: Record<string, unknown>; created_at: string; evidence: RelationshipEvidence[] }[]; next_cursor: string | null; canonical_history_available: boolean; knowledge_as_of: string | null; observation_history_only: boolean; unavailable_relationship_ids: string[] };
/** Lists current or genuinely recorded relationship state with separate valid-time and knowledge-time bounds. */
export function listRelationshipHistory(entityId: string, validAt?: string, knowledgeAsOf?: string, cursor?: string) { const p = new URLSearchParams({ limit: '50', entity_id: entityId, include_unknown_validity: 'true' }); if (validAt) p.set('valid_at', `${validAt}T00:00:00Z`); if (knowledgeAsOf) p.set('knowledge_as_of', `${knowledgeAsOf}T23:59:59.999Z`); if (cursor) p.set('cursor', cursor); return apiRequest<RelationshipHistoryPage>(`/api/v1/relationships?${p}`); }
/** Lists version-pinned evidence for a relationship using optional cursor pagination. */
export function getRelationshipEvidence(id: string, cursor?: string) { const p = new URLSearchParams({ limit: '20' }); if (cursor) p.set('cursor', cursor); return apiRequest<RelationshipEvidencePage>(`/api/v1/relationships/${id}/evidence?${p}`); }
export type EntityReviewEvidence = { document_id: string; document_version_id: string; version_number: number; chunk_id: string; source_id: string; source_name: string; title: string; canonical_url: string | null; metadata_is_version_snapshot: boolean; observed_at: string; excerpt: string };
export type EntityReviewEndpoint = { state: 'assigned' | 'unassigned' | 'ambiguous'; entity_id: string | null; entity_name: string | null; entity_type: string | null; membership_id: string | null };
export type EntityReviewCandidate = { kind: 'entity' | 'relationship'; candidate_id: string | null; work_id: string; result_id: string; snapshot_digest: string | null; document_version_id: string; source_generation: number; owner_generation: number | null; document_id: string | null; version_number: number | null; source_id: string | null; source_name: string | null; evidence: EntityReviewEvidence[]; relationship_type: string | null; source_endpoint: EntityReviewEndpoint | null; target_endpoint: EntityReviewEndpoint | null; actionable: boolean; status: string; candidate_name: string; candidate_type: string | null; reason: string; possible_entity_ids: string[] };
export type EntityReviewPage = { items: EntityReviewCandidate[]; next_cursor: string | null };
/** Lists pending entity review candidates using optional cursor pagination. */
export function listEntityReview(cursor?: string) { const p = new URLSearchParams({ limit: '50' }); if (cursor) p.set('cursor', cursor); return apiRequest<EntityReviewPage>(`/api/v1/entities/review?${p}`); }
/** Assigns a review candidate to the selected entity using its snapshot, generation, and revision fences plus CSRF token. */
export function assignEntityReview(candidateId: string, payload: { result_id: string; snapshot_digest: string; expected_source_generation: number; expected_owner_generation: number; target_entity_id: string; expected_target_revision: number; reason: string }, csrfToken: string) { return apiRequest<{ candidate_id: string; target_entity_id: string; membership_ids: string[]; revision: number }>(`/api/v1/entities/review/${candidateId}/assign`, { method: 'POST', headers: { 'Content-Type': 'application/json', ...csrfHeaders(csrfToken) }, body: JSON.stringify(payload) }); }
/** Resolves a relationship review candidate using its snapshot and generation fences plus CSRF token. */
export function resolveRelationshipReview(candidateId: string, payload: { result_id: string; snapshot_digest: string; expected_source_generation: number; expected_owner_generation: number; reason: string }, csrfToken: string) { return apiRequest<{ candidate_id: string; relationship_id: string }>(`/api/v1/entities/review/${candidateId}/resolve-relationship`, { method: 'POST', headers: { 'Content-Type': 'application/json', ...csrfHeaders(csrfToken) }, body: JSON.stringify(payload) }); }
/** Updates entity fields only at the supplied expected revision and sends the audit reason with CSRF protection. */
export function updateEntity(id: string, payload: { expected_revision: number; name?: string; description?: string | null; reason: string }, csrfToken: string) { return apiRequest<Entity>(`/api/v1/entities/${id}`, { method: 'PATCH', headers: { 'Content-Type': 'application/json', ...csrfHeaders(csrfToken) }, body: JSON.stringify(payload) }); }
/** Previews a merge against the supplied source and target revisions without applying the correction. */
export function previewMergeEntity(id: string, payload: { into_id: string; expected_revision: number; expected_into_revision: number; reason: string }) { return apiRequest<EntityCorrectionPreview>(`/api/v1/entities/${id}/corrections/merge-preview`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) }); }
/** Previews a split using the selected evidence and expected entity revision without applying the correction. */
export function previewSplitEntity(id: string, payload: { evidence_ids: string[]; expected_revision: number; new_entity: { type: string; name: string; description?: string | null; metadata?: Record<string, unknown>; aliases?: string[]; reason: string }; reason: string }) { return apiRequest<EntityCorrectionPreview>(`/api/v1/entities/${id}/corrections/split-preview`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) }); }
/** Applies an entity merge at the supplied source and target revisions with an audit reason and CSRF token. */
export function mergeEntity(id: string, payload: { into_id: string; expected_revision: number; expected_into_revision: number; reason: string }, csrfToken: string) { return apiRequest<EntityCorrectionResult>(`/api/v1/entities/${id}/merge`, { method: 'POST', headers: { 'Content-Type': 'application/json', ...csrfHeaders(csrfToken) }, body: JSON.stringify(payload) }); }
/** Applies an entity split using the selected evidence, expected revision, audit reason, and CSRF token. */
export function splitEntity(id: string, payload: { evidence_ids: string[]; expected_revision: number; new_entity: { type: string; name: string; description?: string | null; metadata?: Record<string, unknown>; aliases?: string[]; reason: string }; reason: string }, csrfToken: string) { return apiRequest<EntityCorrectionResult>(`/api/v1/entities/${id}/split`, { method: 'POST', headers: { 'Content-Type': 'application/json', ...csrfHeaders(csrfToken) }, body: JSON.stringify(payload) }); }
/** Suppresses the selected evidence at the expected revision with an audit reason and CSRF token. */
export function suppressEntityEvidence(id: string, payload: { evidence_ids: string[]; expected_revision: number; reason: string }, csrfToken: string) { return apiRequest<EntityCorrectionResult>(`/api/v1/entities/${id}/suppressions`, { method: 'POST', headers: { 'Content-Type': 'application/json', ...csrfHeaders(csrfToken) }, body: JSON.stringify(payload) }); }

export type MemoryItem = {
  id: string;
  content: string;
  type: string;
  provenance: Record<string, unknown>;
  confidence: number;
  reason: string | null;
  status: 'active' | 'invalidated' | 'superseded' | 'forgotten';
  is_manual: boolean;
  superseded_by_id: string | null;
  candidate_id: string | null;
  created_at: string;
  updated_at: string;
  invalidated_at: string | null;
  forgotten_at: string | null;
};

export type MemoryPage = {
  items: MemoryItem[];
  next_cursor: string | null;
  total_count?: number | null;
};

export type MemoryCandidate = {
  id: string;
  content: string;
  type: string;
  provenance: Record<string, unknown>;
  confidence: number;
  novelty_score: number;
  usefulness_score: number;
  reason: string | null;
  status: 'pending' | 'accepted' | 'rejected' | 'superseded' | 'expired';
  rejection_reason: string | null;
  created_at: string;
  updated_at: string;
  evaluated_at: string | null;
};

export type MemoryCandidatePage = {
  items: MemoryCandidate[];
  next_cursor: string | null;
};

export type MemoryPrivacyConfig = {
  store_conversation_history: boolean;
  store_agent_memory: boolean;
  auto_accept_memory: boolean;
};

export type MemoryPurgeRequest = {
  purge_forgotten_memories?: boolean;
  purge_rejected_candidates?: boolean;
  purge_conversation_history?: boolean;
};

export type MemoryPurgeResponse = {
  purged_memories_count: number;
  purged_candidates_count: number;
  purged_conversations_count: number;
};

export const memoryKeys = {
  all: ['memories'] as const,
  list: (status?: string, type?: string, q?: string) => ['memories', 'list', status ?? 'active', type ?? '', q ?? ''] as const,
  detail: (id: string) => ['memories', id] as const,
  candidates: (status?: string) => ['memories', 'candidates', status ?? 'pending'] as const,
  privacy: ['settings', 'memory-privacy'] as const,
};

/** Lists active or filtered memories with optional cursor pagination. */
export function listMemories(cursor?: string, type?: string, status = 'active', q?: string) {
  const p = new URLSearchParams({ limit: '50', status });
  if (cursor) p.set('cursor', cursor);
  if (type) p.set('type', type);
  if (q) p.set('q', q);
  return apiRequest<MemoryPage>(`/api/v1/memories?${p}`);
}

/** Fetches a single memory item by identifier. */
export function getMemory(id: string) {
  return apiRequest<MemoryItem>(`/api/v1/memories/${id}`);
}

/** Explicitly creates an owner memory item using CSRF protection. */
export function createMemory(payload: { content: string; type?: string; reason?: string }, csrfToken: string) {
  return apiRequest<MemoryItem>('/api/v1/memories', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...csrfHeaders(csrfToken) },
    body: JSON.stringify(payload),
  });
}

/** Updates an active memory with CSRF protection. */
export function updateMemory(id: string, payload: { content?: string; type?: string; reason?: string }, csrfToken: string) {
  return apiRequest<MemoryItem>(`/api/v1/memories/${id}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json', ...csrfHeaders(csrfToken) },
    body: JSON.stringify(payload),
  });
}

/** Forgets a memory immediately, purging it from retrieval context. */
export function forgetMemory(id: string, reason: string | undefined, csrfToken: string) {
  return apiRequest<MemoryItem>(`/api/v1/memories/${id}/forget`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...csrfHeaders(csrfToken) },
    body: JSON.stringify({ reason: reason || 'Forgotten by owner' }),
  });
}

/** Marks an active memory invalidated with an explanation. */
export function invalidateMemory(id: string, reason: string, csrfToken: string) {
  return apiRequest<MemoryItem>(`/api/v1/memories/${id}/invalidate`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...csrfHeaders(csrfToken) },
    body: JSON.stringify({ reason }),
  });
}

/** Lists proposed memory candidates for owner review. */
export function listMemoryCandidates(cursor?: string, status = 'pending') {
  const p = new URLSearchParams({ limit: '50', status });
  if (cursor) p.set('cursor', cursor);
  return apiRequest<MemoryCandidatePage>(`/api/v1/memories/candidates/list?${p}`);
}

/** Accepts a proposed memory candidate into active memory. */
export function acceptMemoryCandidate(id: string, csrfToken: string) {
  return apiRequest<MemoryItem>(`/api/v1/memories/candidates/${id}/accept`, {
    method: 'POST',
    headers: csrfHeaders(csrfToken),
  });
}

/** Rejects a proposed memory candidate with an optional explanation. */
export function rejectMemoryCandidate(id: string, reason: string | undefined, csrfToken: string) {
  return apiRequest<MemoryCandidate>(`/api/v1/memories/candidates/${id}/reject`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...csrfHeaders(csrfToken) },
    body: JSON.stringify({ reason }),
  });
}

/** Reads owner memory and conversation privacy settings. */
export function getMemoryPrivacyConfig() {
  return apiRequest<MemoryPrivacyConfig>('/api/v1/settings/memory-privacy');
}

/** Updates owner memory and conversation privacy controls. */
export function updateMemoryPrivacyConfig(payload: Partial<MemoryPrivacyConfig>, csrfToken: string) {
  return apiRequest<MemoryPrivacyConfig>('/api/v1/settings/memory-privacy', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json', ...csrfHeaders(csrfToken) },
    body: JSON.stringify(payload),
  });
}

/** Purges forgotten memories, rejected candidates, or unpinned conversations. */
export function purgeMemoryData(payload: MemoryPurgeRequest, csrfToken: string) {
  return apiRequest<MemoryPurgeResponse>('/api/v1/settings/memory-privacy/purge', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...csrfHeaders(csrfToken) },
    body: JSON.stringify(payload),
  });
}
