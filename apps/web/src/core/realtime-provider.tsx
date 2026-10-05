'use client';

import { useQuery, useQueryClient } from '@tanstack/react-query';
import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from 'react';
import { useTranslations } from 'next-intl';
import { ApiError, apiRequest } from '@/core/api';
import { useDisplayPreferences } from '@/core/query-provider';

export type RealtimeStatus = 'connecting' | 'connected' | 'reconnecting' | 'unavailable' | 'expired';

type RealtimeContextValue = {
  status: RealtimeStatus;
  newDocumentCount: number;
  documentRefreshRequired: boolean;
  documentRefreshFailed: boolean;
  consumeDocumentUpdates: () => Promise<void>;
};

type ProtectedReadPath = '/api/v1/realtime/snapshot' | '/api/v1/auth/session';
type SnapshotAttempt = { id: number; controller: AbortController };
/** Marks a snapshot read that was canceled because a newer connection attempt took ownership. */
class SupersededSnapshotAttempt extends Error {}

/** Reads protected JSON data and rejects stale attempts, unauthorized responses, and unsuccessful HTTP results. */
async function protectedJsonRead<T>(
  path: ProtectedReadPath,
  signal: AbortSignal,
  isCurrent: () => boolean,
): Promise<T> {
  const response = await fetch(path, {
    cache: 'no-store', credentials: 'same-origin', signal,
  });
  if (!isCurrent()) throw new SupersededSnapshotAttempt();
  if (response.status === 401) throw new ApiError(401, 'Authentication required');
  let body: unknown;
  try {
    body = await response.json();
  } catch (error) {
    if (!response.ok) throw new ApiError(response.status, 'Request failed');
    throw error;
  }
  if (!isCurrent()) throw new SupersededSnapshotAttempt();
  if (!response.ok) {
    const message = body && typeof body === 'object'
      && 'error' in body && typeof body.error === 'object' && body.error
      && 'message' in body.error && typeof body.error.message === 'string'
      ? body.error.message : 'Request failed';
    throw new ApiError(response.status, message);
  }
  return body as T;
}

type Snapshot = { cursor: string; floor_sequence: string };
type ReplayEnvelope = { schema_version: 1 };
type SourceEvent = ReplayEnvelope & {
  source_id: string; generation: number; status: 'active' | 'paused' | 'archived';
  connector_state?: string | null; operation_id?: string | null;
};
type IngestionEvent = ReplayEnvelope & {
  source_id: string; run_id: string; status: 'queued' | 'running' | 'retrying' | 'succeeded' | 'failed' | 'cancelled' | 'needs_ocr';
  stage_key?: string | null; stage_status?: string | null;
};
type KnowledgeEvent = ReplayEnvelope & {
  scope?: 'source' | 'index' | 'graph' | 'timeline' | 'timeline_collection'; source_id?: string | null; document_id?: string | null; version?: number | null;
  deleted: boolean; index_generation_id?: string | null;
  entity_id?: string | null; relationship_id?: string | null;
  event_id?: string | null; event_revision?: number | null;
  index_status?: 'queued' | 'running' | 'active' | 'failed' | 'retired' | null;
  indexed_items?: number | null; failed_items?: number | null;
};
type DashboardEvent = ReplayEnvelope & {
  type: 'dashboard.changed'; scope: 'dashboard' | 'definition' | 'brief'; id: string; revision: number; deleted: boolean;
};

const RealtimeContext = createContext<RealtimeContextValue | null>(null);
const CURSOR_RE = /^([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}):(0|[1-9][0-9]{0,18})$/;
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const MAX_QUEUED_DOCUMENTS = 500;

/** Returns realtime connection and refresh state or throws when called outside its provider. */
export function useRealtime() {
  const value = useContext(RealtimeContext);
  if (!value) throw new Error('Realtime status is unavailable');
  return value;
}

/** Parses JSON only when it matches the supported replay envelope schema. */
function parseEnvelope<T extends ReplayEnvelope>(data: string): T | null {
  try {
    const value: unknown = JSON.parse(data);
    if (!value || typeof value !== 'object' || (value as ReplayEnvelope).schema_version !== 1) return null;
    return value as T;
  } catch {
    return null;
  }
}

/** Validates the bounded realtime cursor wire format. */
function validCursor(value: string): boolean {
  return value.length <= 60 && CURSOR_RE.test(value);
}

/** Checks that a cursor advances within the same stream identity. */
function isCurrent(cursor: string, previous: string): boolean {
  const nextMatch = CURSOR_RE.exec(cursor);
  const previousMatch = CURSOR_RE.exec(previous);
  if (!nextMatch || !previousMatch || nextMatch[1] !== previousMatch[1]) return false;
  // Compare sequence numbers numerically; lexical ordering misorders values once digit counts differ.
  return BigInt(nextMatch[2]) > BigInt(previousMatch[2]);
}

/** Owns the authenticated event stream, cursor replay, domain query invalidation, and bounded document refresh queue for descendants. */
export function RealtimeProvider({ children }: { children: ReactNode }) {
  const client = useQueryClient();
  const display = useDisplayPreferences();
  const t = useTranslations('shell');
  const session = useQuery({
    queryKey: ['session'],
    queryFn: () => apiRequest<{ authenticated: true; csrfToken: string }>('/api/v1/auth/session'),
  });
  const isAuthenticated = Boolean(session.data);
  const sessionExpired = !isAuthenticated && session.error instanceof ApiError && session.error.status === 401;
  const [status, setStatus] = useState<RealtimeStatus>('connecting');
  const [barrierGeneration, setBarrierGeneration] = useState<number | null>(null);
  const [newDocumentCount, setNewDocumentCount] = useState(0);
  const [documentRefreshRequired, setDocumentRefreshRequired] = useState(false);
  const [documentRefreshFailed, setDocumentRefreshFailed] = useState(false);
  const retryRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const queuedDocuments = useRef(new Map<string, number>());
  const documentRevision = useRef(0);
  const unknownChangeRevision = useRef(0);
  const fullRefreshQueued = useRef(false);
  const cursorRef = useRef('');

  /** Queues document IDs for targeted refresh and switches to a full refresh when the bounded queue is exceeded or an event lacks an ID. */
  const queueDocumentUpdate = useCallback((id: string | null) => {
    documentRevision.current += 1;
    // A missing ID or a full queue loses targeted coverage, so require a full document refresh.
    if (!id || fullRefreshQueued.current || queuedDocuments.current.size >= MAX_QUEUED_DOCUMENTS) {
      queuedDocuments.current.clear();
      unknownChangeRevision.current = documentRevision.current;
      fullRefreshQueued.current = true;
    } else {
      queuedDocuments.current.set(id, documentRevision.current);
    }
    setNewDocumentCount(queuedDocuments.current.size);
    if (fullRefreshQueued.current) setDocumentRefreshRequired(true);
  }, []);

  /** Invalidates operational and dashboard configuration queries after reconnect so snapshots cannot leave stale availability visible. */
  const invalidateOperationalQueries = useCallback(async () => {
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
      client.invalidateQueries({ queryKey: ['dashboards'] }),
      client.invalidateQueries({ queryKey: ['dashboard'] }),
      client.invalidateQueries({ queryKey: ['gadget-definitions'] }),
      client.invalidateQueries({ queryKey: ['gadget-definition'] }),
      client.invalidateQueries({ queryKey: ['gadget-renderers'] }),
      client.invalidateQueries({ queryKey: ['gadget-sources'] }),
      client.invalidateQueries({ queryKey: ['dashboard-presets'] }),
      client.invalidateQueries({ queryKey: ['dashboard-preset-preview'] }),
    ]);
  }, [client]);

  /** Refreshes document data and clears only updates covered by the completed query refresh. */
  const consumeDocumentUpdates = useCallback(async () => {
    const generation = display.authGeneration;
    const consumed = new Map(queuedDocuments.current);
    const unknownRevision = unknownChangeRevision.current;
    try {
      await client.invalidateQueries(
        { queryKey: ['documents'], exact: true },
        { throwOnError: true },
      );
      if (!display.isCurrentGeneration(generation)) return;
      // Remove only revisions included in this snapshot so events queued during the fetch survive.
      for (const [id, revision] of consumed) {
        if (queuedDocuments.current.get(id) === revision) queuedDocuments.current.delete(id);
      }
      setNewDocumentCount(queuedDocuments.current.size);
      if (unknownChangeRevision.current === unknownRevision) {
        fullRefreshQueued.current = false;
        setDocumentRefreshRequired(false);
      }
      setDocumentRefreshFailed(false);
    } catch {
      if (display.isCurrentGeneration(generation)) setDocumentRefreshFailed(true);
    }
  }, [client, display.authGeneration, display.isCurrentGeneration]);

  useEffect(() => {
    const generation = display.authGeneration;
    const sessionState = isAuthenticated
      ? 'authenticated'
      : sessionExpired ? 'expired' : 'unavailable';
    if (sessionState !== 'authenticated') {
      setBarrierGeneration(generation);
      setStatus(sessionState);
      return;
    }

    let active = true;
    let attemptSequence = 0;
    let currentAttempt: SnapshotAttempt | null = null;
    let eventSource: EventSource | null = null;
    let authCheckController: AbortController | null = null;
    let retryDelay = 1000;
    setBarrierGeneration(null);
    setStatus('connecting');
    /** Checks whether asynchronous stream work still belongs to the active auth generation. */
    const isCurrentGeneration = () => active && display.isCurrentGeneration(generation);
    /** Checks that a snapshot attempt belongs to the active auth generation and has not been aborted. */
    const ownsAttempt = (attempt: SnapshotAttempt) =>
      isCurrentGeneration() && currentAttempt === attempt && !attempt.controller.signal.aborted;
    /** Aborts the prior snapshot and auth check before creating a new attempt token. */
    const supersedeAttempt = () => {
      currentAttempt?.controller.abort();
      authCheckController?.abort();
      authCheckController = null;
      const attempt = { id: ++attemptSequence, controller: new AbortController() };
      currentAttempt = attempt;
      return attempt;
    };
    /** Aborts and clears the active snapshot attempt and auth check. */
    const cancelAttempt = () => {
      currentAttempt?.controller.abort();
      currentAttempt = null;
      authCheckController?.abort();
      authCheckController = null;
    };
    /** Closes and clears the current event stream and its associated auth check. */
    const closeStream = () => {
      eventSource?.close();
      eventSource = null;
      authCheckController?.abort();
      authCheckController = null;
    };
    /** Cancels and clears the scheduled snapshot retry timer. */
    const clearSnapshotRetry = () => {
      if (retryRef.current) clearTimeout(retryRef.current);
      retryRef.current = null;
    };
    /** Fetches a session snapshot for the current stream attempt and reconciles its cursor before connecting. */
    const openSnapshot = async (
      reconnect: boolean, attempt: SnapshotAttempt, possibleDocumentChanges: boolean,
    ) => {
      if (!ownsAttempt(attempt)) return;
      try {
        const snapshot = await protectedJsonRead<Snapshot>(
          '/api/v1/realtime/snapshot', attempt.controller.signal, () => ownsAttempt(attempt),
        );
        if (!ownsAttempt(attempt)) return;
        if (!validCursor(snapshot.cursor) || !/^(0|[1-9][0-9]{0,18})$/.test(snapshot.floor_sequence)) {
          setStatus('unavailable');
          scheduleSnapshotRetry();
          return;
        }
        if (reconnect) {
          await invalidateOperationalQueries();
          if (!ownsAttempt(attempt)) return;
          if (possibleDocumentChanges) queueDocumentUpdate(null);
        }
        if (!ownsAttempt(attempt)) return;
        openStream(snapshot.cursor, attempt);
        if (ownsAttempt(attempt)) setBarrierGeneration(generation);
      } catch (error) {
        if (!ownsAttempt(attempt) || error instanceof SupersededSnapshotAttempt) return;
        if (error instanceof ApiError && error.status === 401) {
          setStatus('expired');
          window.dispatchEvent(new Event('bbd:unauthorized'));
        } else {
          setStatus('unavailable');
          setBarrierGeneration(generation);
          scheduleSnapshotRetry();
        }
      }
    };
    /** Schedules a bounded-delay snapshot retry only while the current generation remains active. */
    const scheduleSnapshotRetry = () => {
      if (!isCurrentGeneration() || retryRef.current) return;
      retryRef.current = setTimeout(() => {
        retryRef.current = null;
        const attempt = supersedeAttempt();
        void openSnapshot(true, attempt, true);
      }, retryDelay);
      retryDelay = Math.min(30_000, retryDelay * 2);
    };
    /** Replays events from a snapshot cursor and marks broad refresh when event changes cannot be targeted. */
    const resync = (possibleDocumentChanges: boolean) => {
      const attempt = supersedeAttempt();
      closeStream();
      if (!ownsAttempt(attempt)) return;
      setStatus('reconnecting');
      void openSnapshot(true, attempt, possibleDocumentChanges);
    };
    /** Installs one named event listener on the active stream and routes messages through its callback. */
    const register = (
      stream: EventSource, attempt: SnapshotAttempt, name: string,
      callback: (event: MessageEvent<string>) => void,
    ) => {
      stream.addEventListener(name, (event) => {
        if (!(event instanceof MessageEvent) || eventSource !== stream || !ownsAttempt(attempt)) return;
        const id = event.lastEventId;
        if (!validCursor(id) || (cursorRef.current && !isCurrent(id, cursorRef.current))) {
          if (id && CURSOR_RE.exec(id)?.[1] !== CURSOR_RE.exec(cursorRef.current)?.[1]) resync(true);
          return;
        }
        callback(event as MessageEvent<string>);
        if (eventSource === stream && ownsAttempt(attempt)) cursorRef.current = id;
      });
    };
    /** Opens an event stream for the cursor and snapshot attempt after confirming that attempt still owns the connection. */
    const openStream = (cursor: string, attempt: SnapshotAttempt) => {
      if (!ownsAttempt(attempt) || !validCursor(cursor)) return;
      closeStream();
      if (!ownsAttempt(attempt)) return;
      cursorRef.current = cursor;
      const stream = new EventSource(`/api/v1/realtime/events?cursor=${encodeURIComponent(cursor)}`);
      eventSource = stream;
      /** Checks that the event stream and snapshot attempt still match the active connection. */
      const ownsStream = () => eventSource === stream && ownsAttempt(attempt);
      stream.onopen = () => {
        if (!ownsStream()) return;
        retryDelay = 1000;
        clearSnapshotRetry();
        setStatus('connected');
      };
      stream.onerror = () => {
        if (!ownsStream()) return;
        setStatus('reconnecting');
        authCheckController?.abort();
        const controller = new AbortController();
        authCheckController = controller;
        /** Checks that the auth probe still belongs to the active stream and has not been aborted. */
        const ownsAuthCheck = () => ownsStream() && authCheckController === controller && !controller.signal.aborted;
        void protectedJsonRead<unknown>(
          '/api/v1/auth/session', controller.signal, ownsAuthCheck,
        ).catch((error) => {
          if (!ownsAuthCheck()) return;
          if (error instanceof ApiError && error.status === 401) {
            setStatus('expired');
            window.dispatchEvent(new Event('bbd:unauthorized'));
          }
        }).finally(() => {
          if (authCheckController === controller) authCheckController = null;
        });
      };
      /** Refreshes source-backed definition/dashboard availability and presets without carrying source or dashboard content in the event. */
      const handleSourceChange = (event: MessageEvent<string>) => {
        const value = parseEnvelope<SourceEvent>(event.data);
        if (!value || !UUID_RE.test(value.source_id) || !Number.isSafeInteger(value.generation)) { resync(true); return; }
        void Promise.all([
          client.invalidateQueries({ queryKey: ['sources'] }),
          client.invalidateQueries({ queryKey: ['connector-configuration', value.source_id] }),
          client.invalidateQueries({ queryKey: ['connector-activation', value.source_id] }),
          client.invalidateQueries({ queryKey: ['source-ingestion', value.source_id] }),
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
      };
      register(stream, attempt, 'source.changed', handleSourceChange);
      register(stream, attempt, 'ingestion.changed', (event) => {
        const value = parseEnvelope<IngestionEvent>(event.data);
        if (!value || !UUID_RE.test(value.source_id) || !UUID_RE.test(value.run_id)) { resync(true); return; }
        void Promise.all([
          client.invalidateQueries({ queryKey: ['sources', value.source_id] }),
          client.invalidateQueries({ queryKey: ['source-ingestion', value.source_id] }),
          client.invalidateQueries({ queryKey: ['ingestion-run', value.run_id] }),
          client.invalidateQueries({ queryKey: ['connector-activation', value.source_id] }),
        ]);
      });
      register(stream, attempt, 'knowledge.changed', (event) => {
        const value = parseEnvelope<KnowledgeEvent>(event.data);
        if (!value) { resync(true); return; }
        if (value.scope === 'index') {
          if (
            !value.index_generation_id || !UUID_RE.test(value.index_generation_id)
            || !['queued', 'running', 'active', 'failed', 'retired'].includes(value.index_status ?? '')
            || !Number.isSafeInteger(value.indexed_items) || (value.indexed_items ?? -1) < 0
            || !Number.isSafeInteger(value.failed_items) || (value.failed_items ?? -1) < 0
            || value.source_id != null || value.document_id != null || value.version != null || value.deleted !== false
            || value.entity_id != null || value.relationship_id != null
          ) { resync(true); return; }
          void client.invalidateQueries({ queryKey: ['search-index'] });
          return;
        }
        if (value.scope === 'graph') {
          if (
            (value.entity_id == null) === (value.relationship_id == null)
            || (value.entity_id != null && !UUID_RE.test(value.entity_id))
            || (value.relationship_id != null && !UUID_RE.test(value.relationship_id))
            || typeof value.deleted !== 'boolean'
            || value.source_id != null || value.document_id != null || value.version != null
            || value.index_generation_id != null || value.index_status != null
            || value.indexed_items != null || value.failed_items != null
          ) { resync(true); return; }
          void Promise.all([
            client.invalidateQueries({ queryKey: ['entities'] }),
            client.invalidateQueries({ queryKey: ['relationships'] }),
            client.invalidateQueries({ queryKey: ['search'] }),
          ]);
          return;
        }
        if (value.scope === 'timeline') {
          if (
            !value.event_id || !UUID_RE.test(value.event_id)
            || !Number.isSafeInteger(value.event_revision) || (value.event_revision ?? 0) < 1
            || value.source_id != null || value.document_id != null || value.version != null
            || value.entity_id != null || value.relationship_id != null
            || value.index_generation_id != null || value.index_status != null
            || value.indexed_items != null || value.failed_items != null
            || typeof value.deleted !== 'boolean'
          ) { resync(true); return; }
          void Promise.all([
            client.invalidateQueries({ queryKey: ['events'] }),
            client.invalidateQueries({ queryKey: ['timeline'] }),
            // Entity-scoped timelines share event occurrence and participant data.
            client.invalidateQueries({ queryKey: ['entities'] }),
          ]);
          return;
        }
        if (value.scope === 'timeline_collection') {
          if (
            ((value.source_id == null) === (value.entity_id == null))
            || (value.source_id != null && !UUID_RE.test(value.source_id))
            || (value.entity_id != null && !UUID_RE.test(value.entity_id))
            || value.document_id != null || value.version != null || value.deleted
            || value.relationship_id != null
            || value.event_id != null || value.event_revision != null
            || value.index_generation_id != null || value.index_status != null
            || value.indexed_items != null || value.failed_items != null
          ) { resync(true); return; }
          void Promise.all([
            client.invalidateQueries({ queryKey: ['events'] }),
            client.invalidateQueries({ queryKey: ['timeline'] }),
            client.invalidateQueries({ queryKey: ['entities'] }),
          ]);
          return;
        }
        if (
          (value.scope !== undefined && value.scope !== 'source')
          || !value.source_id || !UUID_RE.test(value.source_id)
          || (value.document_id && !UUID_RE.test(value.document_id))
          || value.index_generation_id != null || value.index_status != null
          || value.indexed_items != null || value.failed_items != null
          || value.entity_id != null || value.relationship_id != null
          || value.event_id != null || value.event_revision != null
          || typeof value.deleted !== 'boolean'
        ) { resync(true); return; }
        // Projection updates use source invalidations even before an entity exists.
        // Refetch canonical-backed status instead of treating the event as proof.
        void Promise.all([
          client.invalidateQueries({ queryKey: ['graph-status'] }),
          client.invalidateQueries({ queryKey: ['entities'] }),
        ]);
        if (value.deleted) {
          void Promise.all([
            client.invalidateQueries({ queryKey: ['entities'] }),
            client.invalidateQueries({ queryKey: ['relationships'] }),
            client.invalidateQueries({ queryKey: ['search'] }),
          ]);
        }
        if (value.document_id) queueDocumentUpdate(value.document_id);
        else queueDocumentUpdate(null);
      });
      /** Validates the public identifier-only dashboard event before invalidating its resource and dependent query families. */
      const handleDashboardChange = (event: MessageEvent<string>) => {
        const value = parseEnvelope<DashboardEvent>(event.data);
        if (
          !value || value.type !== 'dashboard.changed'
          || Object.keys(value).some((key) => !['schema_version', 'type', 'scope', 'id', 'revision', 'deleted'].includes(key))
          || (value.scope !== 'dashboard' && value.scope !== 'definition' && value.scope !== 'brief')
          || typeof value.id !== 'string' || !UUID_RE.test(value.id)
          || !Number.isSafeInteger(value.revision) || value.revision < 1
          || typeof value.deleted !== 'boolean'
        ) { resync(true); return; }
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
      };
      register(stream, attempt, 'dashboard.changed', handleDashboardChange);
      /** Checks whether a control event came from the current event stream. */
      const ownControlEvent = () => ownsStream();
      stream.addEventListener('resync_required', () => { if (ownControlEvent()) resync(true); });
      stream.addEventListener('auth_expired', () => {
        if (!ownControlEvent()) return;
        closeStream();
        window.dispatchEvent(new Event('bbd:unauthorized'));
      });
      stream.addEventListener('connection_unavailable', () => {
        if (!ownControlEvent()) return;
        closeStream();
        setStatus('unavailable');
        scheduleSnapshotRetry();
      });
    };
    setStatus('connecting');
    const initialAttempt = supersedeAttempt();
    void openSnapshot(false, initialAttempt, false);
    /** Handles session expiration by ending authenticated client state and stopping the active stream. */
    const authEnding = () => {
      active = false;
      cancelAttempt();
      closeStream();
      clearSnapshotRetry();
      cursorRef.current = '';
      queuedDocuments.current.clear();
      fullRefreshQueued.current = false;
      setNewDocumentCount(0);
      setDocumentRefreshRequired(false);
      setDocumentRefreshFailed(false);
    };
    window.addEventListener('bbd:auth-ending', authEnding);
    return () => {
      active = false;
      cancelAttempt();
      closeStream();
      clearSnapshotRetry();
      window.removeEventListener('bbd:auth-ending', authEnding);
    };
  }, [client, display.authGeneration, display.isCurrentGeneration, invalidateOperationalQueries, isAuthenticated, queueDocumentUpdate, sessionExpired]);

  const context = { status, newDocumentCount, documentRefreshRequired, documentRefreshFailed, consumeDocumentUpdates };
  const waitingForBarrier = isAuthenticated && barrierGeneration !== display.authGeneration;
  return <RealtimeContext.Provider value={context}>
    {waitingForBarrier
      ? <main className="shell"><div className="status-panel skeleton" aria-label={t('loadingWorkspace')} /></main>
      : children}
  </RealtimeContext.Provider>;
}
