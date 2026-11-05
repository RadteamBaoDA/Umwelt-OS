import type { QueryClient } from '@tanstack/react-query';

/** Wire envelope every replayable realtime event carries; unknown versions are rejected. */
export type ReplayEnvelope = { schema_version: 1 };
export type SourceEvent = ReplayEnvelope & {
  source_id: string; generation: number; status: 'active' | 'paused' | 'archived';
  connector_state?: string | null; operation_id?: string | null;
};
export type IngestionEvent = ReplayEnvelope & {
  source_id: string; run_id: string; status: 'queued' | 'running' | 'retrying' | 'succeeded' | 'failed' | 'cancelled' | 'needs_ocr';
  stage_key?: string | null; stage_status?: string | null;
};
export type KnowledgeEvent = ReplayEnvelope & {
  scope?: 'source' | 'index' | 'graph' | 'timeline' | 'timeline_collection'; source_id?: string | null; document_id?: string | null; version?: number | null;
  deleted: boolean; index_generation_id?: string | null;
  entity_id?: string | null; relationship_id?: string | null;
  event_id?: string | null; event_revision?: number | null;
  index_status?: 'queued' | 'running' | 'active' | 'failed' | 'retired' | null;
  indexed_items?: number | null; failed_items?: number | null;
};
export type DashboardEvent = ReplayEnvelope & {
  type: 'dashboard.changed'; scope: 'dashboard' | 'definition' | 'brief'; id: string; revision: number; deleted: boolean;
};

export const CURSOR_RE = /^([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}):(0|[1-9][0-9]{0,18})$/;
export const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const SEQUENCE_RE = /^(0|[1-9][0-9]{0,18})$/;
const INDEX_STATUSES = ['queued', 'running', 'active', 'failed', 'retired'];
const DASHBOARD_EVENT_KEYS = ['schema_version', 'type', 'scope', 'id', 'revision', 'deleted'];

/** Parses JSON only when it matches the supported replay envelope schema. */
export function parseEnvelope<T extends ReplayEnvelope>(data: string): T | null {
  try {
    const value: unknown = JSON.parse(data);
    if (!value || typeof value !== 'object' || (value as ReplayEnvelope).schema_version !== 1) return null;
    return value as T;
  } catch {
    return null;
  }
}

/** Validates the bounded realtime cursor wire format. */
export function validCursor(value: string): boolean {
  return value.length <= 60 && CURSOR_RE.test(value);
}

/** Validates the snapshot floor sequence wire format (unsigned decimal without leading zeros, at most 19 digits). */
export function validFloorSequence(value: string): boolean {
  return SEQUENCE_RE.test(value);
}

/** Checks that a cursor advances within the same stream identity. */
export function isCurrent(cursor: string, previous: string): boolean {
  const nextMatch = CURSOR_RE.exec(cursor);
  const previousMatch = CURSOR_RE.exec(previous);
  if (!nextMatch || !previousMatch || nextMatch[1] !== previousMatch[1]) return false;
  // Compare sequence numbers numerically; lexical ordering misorders values once digit counts differ.
  return BigInt(nextMatch[2]) > BigInt(previousMatch[2]);
}

/** True when every named field of the event is absent (null or undefined), i.e. unused by the event's scope. */
function allUnset(value: KnowledgeEvent, keys: Array<keyof KnowledgeEvent>): boolean {
  return keys.every((key) => value[key] == null);
}

/** True for a safe, non-negative integer counter. */
function isCount(value: unknown): boolean {
  return Number.isSafeInteger(value) && (value as number) >= 0;
}

/** True for a UUID when present; absent (null/undefined) fields are left to the caller's presence rules. */
function uuidOrAbsent(value: string | null | undefined): boolean {
  return value == null || UUID_RE.test(value);
}

const INDEX_FIELDS: Array<keyof KnowledgeEvent> = ['source_id', 'document_id', 'version', 'entity_id', 'relationship_id'];
const GRAPH_FIELDS: Array<keyof KnowledgeEvent> = [
  'source_id', 'document_id', 'version', 'index_generation_id', 'index_status', 'indexed_items', 'failed_items',
];
const TIMELINE_FIELDS: Array<keyof KnowledgeEvent> = [
  'source_id', 'document_id', 'version', 'entity_id', 'relationship_id',
  'index_generation_id', 'index_status', 'indexed_items', 'failed_items',
];
const COLLECTION_FIELDS: Array<keyof KnowledgeEvent> = [
  'document_id', 'version', 'relationship_id', 'event_id', 'event_revision',
  'index_generation_id', 'index_status', 'indexed_items', 'failed_items',
];
const SOURCE_FIELDS: Array<keyof KnowledgeEvent> = [
  'index_generation_id', 'index_status', 'indexed_items', 'failed_items',
  'entity_id', 'relationship_id', 'event_id', 'event_revision',
];

/** Strict shape check for `scope: "index"` events (search index generation progress). */
export function validIndexEvent(value: KnowledgeEvent): boolean {
  return Boolean(value.index_generation_id) && UUID_RE.test(value.index_generation_id as string)
    && INDEX_STATUSES.includes(value.index_status ?? '')
    && isCount(value.indexed_items) && isCount(value.failed_items)
    && allUnset(value, INDEX_FIELDS) && value.deleted === false;
}

/** Strict shape check for `scope: "graph"` events: exactly one of entity/relationship id, no other scope fields. */
export function validGraphEvent(value: KnowledgeEvent): boolean {
  return (value.entity_id == null) !== (value.relationship_id == null)
    && uuidOrAbsent(value.entity_id) && uuidOrAbsent(value.relationship_id)
    && typeof value.deleted === 'boolean'
    && allUnset(value, GRAPH_FIELDS);
}

/** Strict shape check for `scope: "timeline"` events (one event id with a positive revision). */
export function validTimelineEvent(value: KnowledgeEvent): boolean {
  return Boolean(value.event_id) && UUID_RE.test(value.event_id as string)
    && Number.isSafeInteger(value.event_revision) && (value.event_revision ?? 0) >= 1
    && allUnset(value, TIMELINE_FIELDS)
    && typeof value.deleted === 'boolean';
}

/** Strict shape check for `scope: "timeline_collection"` events: exactly one of source/entity id, never deleted. */
export function validTimelineCollectionEvent(value: KnowledgeEvent): boolean {
  return (value.source_id == null) !== (value.entity_id == null)
    && uuidOrAbsent(value.source_id) && uuidOrAbsent(value.entity_id)
    && !value.deleted
    && allUnset(value, COLLECTION_FIELDS);
}

/** Strict shape check for source-scoped (default) events carrying a source id and optional document id. */
export function validSourceKnowledgeEvent(value: KnowledgeEvent): boolean {
  return (value.scope === undefined || value.scope === 'source')
    && Boolean(value.source_id) && UUID_RE.test(value.source_id as string)
    && (!value.document_id || UUID_RE.test(value.document_id))
    && allUnset(value, SOURCE_FIELDS)
    && typeof value.deleted === 'boolean';
}

/** Strict shape check for the identifier-only dashboard change event; unknown keys are rejected. */
export function validDashboardEvent(value: DashboardEvent | null): value is DashboardEvent {
  return Boolean(value) && (value as DashboardEvent).type === 'dashboard.changed'
    && Object.keys(value as DashboardEvent).every((key) => DASHBOARD_EVENT_KEYS.includes(key))
    && ['dashboard', 'definition', 'brief'].includes((value as DashboardEvent).scope)
    && typeof (value as DashboardEvent).id === 'string' && UUID_RE.test((value as DashboardEvent).id)
    && Number.isSafeInteger((value as DashboardEvent).revision) && (value as DashboardEvent).revision >= 1
    && typeof (value as DashboardEvent).deleted === 'boolean';
}

/** Validates the source change envelope's identifying fields. */
export function validSourceEvent(value: SourceEvent | null): value is SourceEvent {
  return Boolean(value) && UUID_RE.test((value as SourceEvent).source_id)
    && Number.isSafeInteger((value as SourceEvent).generation);
}

/** Validates the ingestion run change envelope's identifying fields. */
export function validIngestionEvent(value: IngestionEvent | null): value is IngestionEvent {
  return Boolean(value) && UUID_RE.test((value as IngestionEvent).source_id) && UUID_RE.test((value as IngestionEvent).run_id);
}

/** Invalidates operational and dashboard configuration queries after reconnect so snapshots cannot leave stale availability visible. */
export async function invalidateOperationalQueries(client: QueryClient): Promise<void> {
  await Promise.all([
    client.invalidateQueries({ queryKey: ['sources'] }),
    client.invalidateQueries({ queryKey: ['source-ingestion'] }),
    client.invalidateQueries({ queryKey: ['ingestion-run'] }),
    client.invalidateQueries({ queryKey: ['connector-catalog'] }),
    client.invalidateQueries({ queryKey: ['connector-configuration'] }),
    client.invalidateQueries({ queryKey: ['connector-activation'] }),
    client.invalidateQueries({ queryKey: ['operation'] }),
    client.invalidateQueries({ queryKey: ['search-index'] }),
    client.invalidateQueries({ queryKey: ['entities'] }),
    client.invalidateQueries({ queryKey: ['relationships'] }),
    client.invalidateQueries({ queryKey: ['search'] }),
    client.invalidateQueries({ queryKey: ['world-observations'] }),
    client.resetQueries({ queryKey: ['intelligence-correlations'] }),
    client.invalidateQueries({ queryKey: ['dashboards'] }),
    client.invalidateQueries({ queryKey: ['dashboard'] }),
    client.invalidateQueries({ queryKey: ['gadget-definitions'] }),
    client.invalidateQueries({ queryKey: ['gadget-definition'] }),
    client.invalidateQueries({ queryKey: ['gadget-renderers'] }),
    client.invalidateQueries({ queryKey: ['gadget-sources'] }),
    client.invalidateQueries({ queryKey: ['dashboard-presets'] }),
    client.invalidateQueries({ queryKey: ['dashboard-preset-preview'] }),
  ]);
}

/** Refreshes source-backed definition/dashboard availability and presets without carrying source or dashboard content in the event. */
export function applySourceChange(client: QueryClient, value: SourceEvent): void {
  // Source policy can make cached series unauthorized; clear them before refetching.
  client.removeQueries({ queryKey: ['world-observations'] });
  void client.resetQueries({ queryKey: ['intelligence-correlations'] });
  void Promise.all([
    client.invalidateQueries({ queryKey: ['sources'] }),
    client.invalidateQueries({ queryKey: ['connector-configuration', value.source_id] }),
    client.invalidateQueries({ queryKey: ['connector-activation', value.source_id] }),
    client.invalidateQueries({ queryKey: ['source-ingestion', value.source_id] }),
    client.invalidateQueries({ queryKey: ['world-observations'] }),
    client.invalidateQueries({ queryKey: ['search'] }),
    ...(value.operation_id ? [client.invalidateQueries({ queryKey: ['operation', value.operation_id] })] : []),
    client.invalidateQueries({ queryKey: ['gadget-sources'] }),
    // Definitions expose source lifecycle warnings, so a source event invalidates the whole owner library.
    client.invalidateQueries({ queryKey: ['gadget-definitions'] }),
    client.invalidateQueries({ queryKey: ['gadget-definition'] }),
    client.invalidateQueries({ queryKey: ['dashboards'] }),
    client.invalidateQueries({ queryKey: ['dashboard'] }),
    client.invalidateQueries({ queryKey: ['dashboard-presets'] }),
    client.invalidateQueries({ queryKey: ['dashboard-preset-preview'] }),
  ]);
}

/** Invalidates the queries that show one ingestion run and its source. */
export function applyIngestionChange(client: QueryClient, value: IngestionEvent): void {
  void Promise.all([
    client.invalidateQueries({ queryKey: ['sources', value.source_id] }),
    client.invalidateQueries({ queryKey: ['source-ingestion', value.source_id] }),
    client.invalidateQueries({ queryKey: ['ingestion-run', value.run_id] }),
    client.invalidateQueries({ queryKey: ['connector-activation', value.source_id] }),
  ]);
}

/** Invalidates graph-backed queries (entities, relationships, search). */
function invalidateGraphQueries(client: QueryClient): void {
  void Promise.all([
    client.invalidateQueries({ queryKey: ['entities'] }),
    client.invalidateQueries({ queryKey: ['relationships'] }),
    client.invalidateQueries({ queryKey: ['search'] }),
  ]);
}

/** Resets correlations and refreshes timeline-backed queries (shared by timeline and timeline_collection scopes). */
function invalidateTimelineQueries(client: QueryClient): void {
  void client.resetQueries({ queryKey: ['intelligence-correlations'] });
  void Promise.all([
    client.invalidateQueries({ queryKey: ['events'] }),
    client.invalidateQueries({ queryKey: ['timeline'] }),
    // Entity-scoped timelines share event occurrence and participant data.
    client.invalidateQueries({ queryKey: ['entities'] }),
  ]);
}

/** Applies a validated source/document-scoped knowledge event; queues the document for the bounded refresh banner. */
function applySourceKnowledgeChange(
  client: QueryClient, value: KnowledgeEvent, queueDocumentUpdate: (id: string | null) => void,
): void {
  if (value.deleted) {
    // Deletion revokes the evidence behind cached measurements; reset clears it and refetches mounted consumers.
    void client.resetQueries({ queryKey: ['world-observations'] });
  } else {
    // Keep ordinary updates stable while the user reads; refresh their values on focus/remount.
    void client.invalidateQueries({ queryKey: ['world-observations'], refetchType: 'none' });
  }
  // Source/document lifecycle can revoke current support; clear cached IDs before any refetch.
  void client.resetQueries({ queryKey: ['intelligence-correlations'] });
  // Projection updates use source invalidations even before an entity exists.
  // Refetch canonical-backed status instead of treating the event as proof.
  void Promise.all([
    client.invalidateQueries({ queryKey: ['graph-status'] }),
    client.invalidateQueries({ queryKey: ['entities'] }),
  ]);
  if (value.deleted) invalidateGraphQueries(client);
  queueDocumentUpdate(value.document_id ? value.document_id : null);
}

/**
 * Validates one knowledge event for its scope and applies the matching cache invalidations.
 * @returns false when the event violates its scope's strict shape, so the caller must resync from a snapshot.
 */
export function applyKnowledgeChange(
  client: QueryClient, value: KnowledgeEvent, queueDocumentUpdate: (id: string | null) => void,
): boolean {
  switch (value.scope) {
    case 'index':
      if (!validIndexEvent(value)) return false;
      void client.invalidateQueries({ queryKey: ['search-index'] });
      return true;
    case 'graph':
      if (!validGraphEvent(value)) return false;
      invalidateGraphQueries(client);
      return true;
    case 'timeline':
      if (!validTimelineEvent(value)) return false;
      invalidateTimelineQueries(client);
      return true;
    case 'timeline_collection':
      if (!validTimelineCollectionEvent(value)) return false;
      invalidateTimelineQueries(client);
      return true;
    default:
      if (!validSourceKnowledgeEvent(value)) return false;
      applySourceKnowledgeChange(client, value, queueDocumentUpdate);
      return true;
  }
}

/** Invalidates the resource named by a validated dashboard event and its dependent query families. */
export function applyDashboardChange(client: QueryClient, value: DashboardEvent): void {
  const invalidations = value.scope === 'brief'
    ? [
      client.invalidateQueries({ queryKey: ['daily-context'] }),
      client.invalidateQueries({ queryKey: ['notifications'] }),
    ]
    : value.scope === 'dashboard'
    ? [
      client.invalidateQueries({ queryKey: ['dashboards'] }),
      client.invalidateQueries({ queryKey: ['dashboard', value.id] }),
    ]
    : [
      client.invalidateQueries({ queryKey: ['gadget-definitions'] }),
      client.invalidateQueries({ queryKey: ['gadget-definition', value.id] }),
      client.invalidateQueries({ queryKey: ['dashboard'] }),
    ];
  void Promise.all(invalidations);
}
