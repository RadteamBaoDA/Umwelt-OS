'use client';

import { useQuery, useQueryClient } from '@tanstack/react-query';
import { createContext, useCallback, useContext, useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { apiRequest, configureWorkspace } from '@/core/api';
import { workspaceKeys } from '@/core/query-keys';

export type WorkspaceRead = {
  id: string; name: string; owner_user_id: number; is_default: boolean;
  role: 'owner' | 'member'; configuration_revision: number;
};
export type WorkspaceSelection = { id: string; role: 'owner' | 'member'; revision: number };
export type WorkspaceContextValue = {
  selection: WorkspaceSelection | null;
  defaultWorkspaceId: string | null;
  workspaces: WorkspaceRead[];
  generation: number;
  isOwner: boolean;
  selectWorkspace(id: string): void;
};

const STORAGE_KEY = 'umwelt:workspace';
const WorkspaceContext = createContext<WorkspaceContextValue | null>(null);

/** Returns the workspace selection, list and switch action. */
export function useWorkspace(): WorkspaceContextValue {
  const value = useContext(WorkspaceContext);
  if (!value) throw new Error('Workspace context is unavailable');
  return value;
}

function readStored(): string | null {
  try { return window.sessionStorage.getItem(STORAGE_KEY); } catch { return null; }
}
function writeStored(id: string | null) {
  try {
    if (id) window.sessionStorage.setItem(STORAGE_KEY, id); else window.sessionStorage.removeItem(STORAGE_KEY);
  } catch { /* sessionStorage can be blocked; the selection then lives only in memory. */ }
}

/** Loads the account's workspaces, owns the per-tab selection, and resets all scoped state on a switch. */
export function WorkspaceProvider({ children }: { children: ReactNode }) {
  const client = useQueryClient();
  // A 401 leaves the list empty and the selection null, so bootstrap and login work as before.
  const list = useQuery({
    queryKey: workspaceKeys.list(),
    queryFn: () => apiRequest<{ items: WorkspaceRead[] }>('/api/v1/workspaces'),
    refetchOnMount: 'always',
  });
  const workspaces = useMemo(() => list.data?.items ?? [], [list.data]);
  const defaultWorkspaceId = workspaces.find((w) => w.is_default)?.id ?? null;

  const [requestedId, setRequestedId] = useState<string | null>(null);
  const [generation, setGeneration] = useState(0);
  const generationRef = useRef(0);
  const controllerRef = useRef(new AbortController());
  const [stored] = useState(() => typeof window === 'undefined' ? null : readStored());

  const chosen = requestedId ?? stored;
  const current = workspaces.find((w) => w.id === chosen) ?? workspaces.find((w) => w.is_default) ?? null;
  const selection = useMemo<WorkspaceSelection | null>(
    () => current ? { id: current.id, role: current.role, revision: current.configuration_revision } : null,
    [current],
  );
  const selectedIdRef = useRef<string | null>(null);
  useLayoutEffect(() => { selectedIdRef.current = selection?.id ?? null; }, [selection]);

  useLayoutEffect(() => {
    configureWorkspace({
      selectedId: () => selectedIdRef.current,
      generation: () => generationRef.current,
      signal: () => controllerRef.current.signal,
    });
    return () => configureWorkspace(null);
  }, []);

  const reset = useCallback(async (id: string | null) => {
    const keep = workspaces;
    generationRef.current += 1;
    controllerRef.current.abort();
    controllerRef.current = new AbortController();
    selectedIdRef.current = id;
    await client.cancelQueries();
    client.clear();
    // The list itself must survive the clear or the selection would drop to null.
    client.setQueryData(workspaceKeys.list(), { items: keep });
    setGeneration(generationRef.current);
  }, [client, workspaces]);

  const selectWorkspace = useCallback((id: string) => {
    if (id === selectedIdRef.current || !workspaces.some((w) => w.id === id)) return;
    writeStored(id);
    setRequestedId(id);
    void reset(id);
  }, [reset, workspaces]);

  // A revoked or vanished selected workspace falls back to the account default.
  useEffect(() => {
    const fallback = () => {
      if (!defaultWorkspaceId || selectedIdRef.current === defaultWorkspaceId) return;
      writeStored(null);
      setRequestedId(defaultWorkspaceId);
      void reset(defaultWorkspaceId);
    };
    window.addEventListener('bbd:workspace-invalid', fallback);
    return () => window.removeEventListener('bbd:workspace-invalid', fallback);
  }, [defaultWorkspaceId, reset]);

  const value = useMemo<WorkspaceContextValue>(() => ({
    selection, defaultWorkspaceId, workspaces, generation,
    isOwner: selection?.role === 'owner', selectWorkspace,
  }), [selection, defaultWorkspaceId, workspaces, generation, selectWorkspace]);

  // Remounting on the selection clears protected view state and chat drafts.
  return <WorkspaceContext.Provider value={value}>
    <div key={selection?.id ?? 'pending'} className="contents">{children}</div>
  </WorkspaceContext.Provider>;
}
