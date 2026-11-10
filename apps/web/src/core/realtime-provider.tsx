'use client';

import { useQuery, useQueryClient } from '@tanstack/react-query';
import { usePathname } from 'next/navigation';
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { useTranslations } from 'next-intl';
import { ApiError, apiRequest } from '@/core/api';
import { useDisplayPreferences } from '@/core/query-provider';
import { RealtimeConnection, type RealtimeStatus } from '@/core/realtime-connection';
import { invalidateOperationalQueries } from '@/core/realtime-events';
import { useResetOnChange } from '@/modules/shared';

export type { RealtimeStatus };

type RealtimeContextValue = {
  status: RealtimeStatus;
  newDocumentCount: number;
  documentRefreshRequired: boolean;
  documentRefreshFailed: boolean;
  consumeDocumentUpdates: () => Promise<void>;
};

const RealtimeContext = createContext<RealtimeContextValue | null>(null);
const MAX_QUEUED_DOCUMENTS = 500;

/** Returns realtime connection and refresh state or throws when called outside its provider. */
export function useRealtime() {
  const value = useContext(RealtimeContext);
  if (!value) throw new Error('Realtime status is unavailable');
  return value;
}

/**
 * Owns the bounded queue of document ids changed by realtime events and the refresh state exposed to consumers.
 * `queueDocumentUpdate(null)` (or an overflow past 500 ids) degrades to a full refresh requirement;
 * `consumeDocumentUpdates` clears only the revisions covered by its completed refetch so events queued meanwhile survive.
 */
export function useDocumentUpdateQueue(
  client: ReturnType<typeof useQueryClient>,
  authGeneration: number,
  isCurrentAuthGeneration: (generation: number) => boolean,
) {
  const [newDocumentCount, setNewDocumentCount] = useState(0);
  const [documentRefreshRequired, setDocumentRefreshRequired] = useState(false);
  const [documentRefreshFailed, setDocumentRefreshFailed] = useState(false);
  const queuedDocuments = useRef(new Map<string, number>());
  const documentRevision = useRef(0);
  const unknownChangeRevision = useRef(0);
  const fullRefreshQueued = useRef(false);

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

  /** Drops all queued state when authentication ends. */
  const resetDocumentState = useCallback(() => {
    queuedDocuments.current.clear();
    fullRefreshQueued.current = false;
    setNewDocumentCount(0);
    setDocumentRefreshRequired(false);
    setDocumentRefreshFailed(false);
  }, []);

  /** Refreshes document data and clears only updates covered by the completed query refresh. */
  const consumeDocumentUpdates = useCallback(async () => {
    const generation = authGeneration;
    const consumed = new Map(queuedDocuments.current);
    const unknownRevision = unknownChangeRevision.current;
    try {
      await client.invalidateQueries(
        { queryKey: ['documents'], exact: true },
        { throwOnError: true },
      );
      if (!isCurrentAuthGeneration(generation)) return;
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
      if (isCurrentAuthGeneration(generation)) setDocumentRefreshFailed(true);
    }
  }, [client, authGeneration, isCurrentAuthGeneration]);

  return {
    newDocumentCount, documentRefreshRequired, documentRefreshFailed,
    queueDocumentUpdate, resetDocumentState, consumeDocumentUpdates,
  };
}

/** Owns the authenticated event stream, cursor replay, domain query invalidation, and bounded document refresh queue for descendants. */
export function RealtimeProvider({ children }: { children: ReactNode }) {
  const client = useQueryClient();
  const pathname = usePathname();
  const isPublicPage = pathname === '/' || ['/login', '/setup', '/invite', '/invitations/accept', '/pick', '/privacy', '/terms', '/refunds', '/account-deletion'].includes(pathname);
  const { authGeneration, isCurrentGeneration: isCurrentAuthGeneration } = useDisplayPreferences();
  const t = useTranslations('shell');
  const session = useQuery({
    queryKey: ['session'],
    queryFn: () => apiRequest<{ authenticated: true; csrfToken: string }>('/api/v1/auth/session'),
  });
  const isAuthenticated = Boolean(session.data);
  const sessionExpired = !isAuthenticated && session.error instanceof ApiError && session.error.status === 401;
  const isConnectable = !isPublicPage && isAuthenticated;
  const [connectedStatus, setStatus] = useState<RealtimeStatus>('connecting');
  const status: RealtimeStatus = isConnectable ? connectedStatus : isPublicPage ? 'unavailable' : sessionExpired ? 'expired' : 'unavailable';
  const [barrierGeneration, setBarrierGeneration] = useState<number | null>(null);
  const documents = useDocumentUpdateQueue(client, authGeneration, isCurrentAuthGeneration);
  const retryRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const cursorRef = useRef('');
  const { newDocumentCount, documentRefreshRequired, documentRefreshFailed, consumeDocumentUpdates } = documents;
  const { queueDocumentUpdate, resetDocumentState } = documents;

  // A new connection attempt (client, auth generation or connectability change) restarts at the barrier.
  useResetOnChange([client, authGeneration, isConnectable], () => {
    setBarrierGeneration(null);
    setStatus('connecting');
  });

  useEffect(() => {
    const generation = authGeneration;
    // Public pages and non-authenticated sessions never open a stream; their status is derived below.
    if (!isConnectable) return;
    const connection = new RealtimeConnection({
      client,
      isCurrentAuthGeneration: () => isCurrentAuthGeneration(generation),
      setStatus,
      markBarrier: () => setBarrierGeneration(generation),
      cursorRef,
      retryRef,
      invalidateOperationalQueries: () => invalidateOperationalQueries(client),
      queueDocumentUpdate,
      resetDocumentState,
    });
    connection.start();
    window.addEventListener('bbd:auth-ending', connection.authEnding);
    return () => {
      connection.dispose();
      window.removeEventListener('bbd:auth-ending', connection.authEnding);
    };
  }, [client, authGeneration, isCurrentAuthGeneration, isConnectable, queueDocumentUpdate, resetDocumentState]);

  // Memoize context value so consumers do not re-render when internal barrier state ticks without changing exposed values.
  const context = useMemo(
    () => ({ status, newDocumentCount, documentRefreshRequired, documentRefreshFailed, consumeDocumentUpdates }),
    [status, newDocumentCount, documentRefreshRequired, documentRefreshFailed, consumeDocumentUpdates],
  );
  const waitingForBarrier = isConnectable && barrierGeneration !== authGeneration;
  return <RealtimeContext.Provider value={context}>
    {waitingForBarrier
      ? <main className="shell"><div className="status-panel skeleton" aria-label={t('loadingWorkspace')} /></main>
      : children}
  </RealtimeContext.Provider>;
}
