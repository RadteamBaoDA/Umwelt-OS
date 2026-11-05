import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, renderHook, waitFor } from '@testing-library/react';
import type { ReactNode } from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { Entity } from './api';
import { useEntityDetailController, useEntityDraftState, useEntityQueries } from './use-entity-detail';

const mocks = vi.hoisted(() => ({
  replace: vi.fn(),
  registerLeaveGuard: vi.fn(),
  ensureEntityDocument: vi.fn(() => true),
  guardDispose: vi.fn(),
  api: {
    getEntity: vi.fn(),
    listEntityEvidence: vi.fn(),
    listEntityHistory: vi.fn(),
    getEntityTimeline: vi.fn(),
    getEntityNeighbors: vi.fn(),
    getRelationshipEvidence: vi.fn(),
    listEntities: vi.fn(),
    updateEntity: vi.fn(),
    addEntityAlias: vi.fn(),
    deleteEntityAlias: vi.fn(),
    mergeEntity: vi.fn(),
    previewMergeEntity: vi.fn(),
    previewSplitEntity: vi.fn(),
    splitEntity: vi.fn(),
    suppressEntityEvidence: vi.fn(),
  },
}));

// Stable references: hook effects depend on these objects, so fresh values per render would loop.
const router = { replace: mocks.replace };
const session = { csrfToken: 'csrf-1' };
const display = { timezone: 'UTC' };
const chat = {};
const guarded = { registerLeaveGuard: mocks.registerLeaveGuard, ensureEntityDocument: mocks.ensureEntityDocument };
const translate = (key: string) => key;

vi.mock('next/navigation', () => ({ useParams: () => ({ entityId: 'e-1' }), useRouter: () => router }));
vi.mock('next-intl', () => ({ useTranslations: () => translate }));
vi.mock('@/core/app-shell/chat-controller', () => ({ useChatController: () => chat }));
vi.mock('@/core/app-shell/workspace-shell', () => ({ useWorkspaceSession: () => session }));
vi.mock('@/core/guarded-navigation', () => ({ useGuardedNavigation: () => guarded }));
vi.mock('@/core/query-provider', () => ({ useDisplayPreferences: () => display }));
vi.mock('@/core/workspace-context', () => ({ useWorkspace: () => ({ isAdminViewer: true }) }));
vi.mock('./api', async (importOriginal) => ({ ...(await importOriginal<typeof import('./api')>()), ...mocks.api }));

function entity(overrides: Partial<Entity> = {}): Entity {
  return {
    id: 'e-1',
    type: 'person',
    name: 'Ada',
    canonical_name: 'ada',
    description: 'Mathematician',
    name_origin: null,
    description_origin: null,
    metadata: {},
    revision: 3,
    first_seen_at: null,
    last_seen_at: null,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    aliases: [],
    ...overrides,
  };
}

function wrapper() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return function Wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  mocks.registerLeaveGuard.mockReturnValue(mocks.guardDispose);
  mocks.ensureEntityDocument.mockReturnValue(true);
  const empty = { items: [], next_cursor: null };
  mocks.api.getEntity.mockResolvedValue(entity());
  mocks.api.listEntityEvidence.mockResolvedValue(empty);
  mocks.api.listEntityHistory.mockResolvedValue({ ...empty, memberships: [], membership_next_cursor: null });
  mocks.api.getEntityTimeline.mockResolvedValue({ timeline: { next_cursor: null } });
  mocks.api.getEntityNeighbors.mockResolvedValue(empty);
  mocks.api.getRelationshipEvidence.mockResolvedValue(empty);
  mocks.api.listEntities.mockResolvedValue(empty);
});

describe('useEntityDraftState', () => {
  it('starts on the overview tab with no selection and a clean dirty flag', () => {
    const { result } = renderHook(() => useEntityDraftState(), { wrapper: wrapper() });

    expect(result.current.entityId).toBe('e-1');
    expect(result.current.tab).toBe('overview');
    expect(result.current.selected).toEqual([]);
    expect(result.current.dirtyRef.current).toBe(false);
  });

  it('marks the draft dirty when evidence is selected or suppression is confirmed, and clean again afterwards', () => {
    const { result } = renderHook(() => useEntityDraftState(), { wrapper: wrapper() });

    act(() => result.current.setSelected(['ev-1']));
    expect(result.current.dirtyRef.current).toBe(true);
    act(() => result.current.setSelected([]));
    expect(result.current.dirtyRef.current).toBe(false);
    act(() => result.current.setSuppressConfirmed(true));
    expect(result.current.dirtyRef.current).toBe(true);
  });

  it('marks the draft dirty once a form field is edited', async () => {
    const { result } = renderHook(() => useEntityDraftState(), { wrapper: wrapper() });

    act(() => result.current.identity.setValue('name', 'Grace', { shouldDirty: true }));

    await waitFor(() => expect(result.current.dirtyRef.current).toBe(true));
  });
});

describe('useEntityQueries', () => {
  it('loads the entity, opens its document and exposes merge targets without the entity itself', async () => {
    mocks.api.listEntities.mockResolvedValue({ items: [entity(), entity({ id: 'e-2', name: 'Grace' })], next_cursor: null });
    const { result } = renderHook(
      () => {
        const draft = useEntityDraftState();
        return { draft, queries: useEntityQueries(draft) };
      },
      { wrapper: wrapper() },
    );

    await waitFor(() => expect(result.current.queries.entity.data?.id).toBe('e-1'));
    expect(mocks.api.getEntity).toHaveBeenCalledWith('e-1');
    expect(result.current.draft.documentReadyId).toBe('e-1');

    // Target search is only enabled for 2+ characters.
    expect(mocks.api.listEntities).not.toHaveBeenCalled();
    act(() => result.current.draft.setTargetSearch('g'));
    expect(mocks.api.listEntities).not.toHaveBeenCalled();
    act(() => result.current.draft.setTargetSearch('gr'));
    await waitFor(() => expect(result.current.queries.targetItems.map((item) => item.id)).toEqual(['e-2']));
    expect(mocks.api.listEntities).toHaveBeenCalledWith(undefined, 'person', 'gr');
  });

  it('does not mark the document ready until guarded navigation allows it', async () => {
    mocks.ensureEntityDocument.mockReturnValue(false);
    const { result } = renderHook(
      () => {
        const draft = useEntityDraftState();
        useEntityQueries(draft);
        return draft;
      },
      { wrapper: wrapper() },
    );

    await waitFor(() => expect(mocks.api.getEntity).toHaveBeenCalled());
    expect(result.current.documentReadyId).toBeNull();
  });
});

describe('useEntityDetailController', () => {
  async function loaded() {
    const hook = renderHook(() => useEntityDetailController(), { wrapper: wrapper() });
    await waitFor(() => expect(hook.result.current.entity.data?.id).toBe('e-1'));
    await waitFor(() => expect(hook.result.current.baselineRef.current?.revision).toBe(3));
    return hook;
  }

  it('seeds the identity form from the loaded entity and registers one leave guard', async () => {
    const { result } = await loaded();

    expect(result.current.identity.getValues()).toMatchObject({ name: 'Ada', description: 'Mathematician' });
    expect(result.current.baselineRef.current).toEqual({ id: 'e-1', revision: 3, name: 'Ada', description: 'Mathematician' });
    expect(mocks.registerLeaveGuard).toHaveBeenCalled();
    const guard = mocks.registerLeaveGuard.mock.calls.at(-1)![0];
    expect(guard.hasUnsavedChanges()).toBe(false);
  });

  it('reports unsaved changes through the guard, asks before discarding and clears the draft on leave', async () => {
    const { result } = await loaded();
    act(() => result.current.setSelected(['ev-1']));
    act(() => result.current.setTargetSearch('gr'));
    const guard = mocks.registerLeaveGuard.mock.calls.at(-1)![0];

    expect(guard.hasUnsavedChanges()).toBe(true);
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false);
    expect(guard.confirmDiscard()).toBe(false);
    expect(confirm).toHaveBeenCalledWith('discardChangesQuestion');

    act(() => guard.acceptLeave());
    expect(result.current.selected).toEqual([]);
    expect(result.current.targetSearch).toBe('');
    expect(result.current.baselineRef.current).toBeNull();
    expect(guard.hasUnsavedChanges()).toBe(false);
    confirm.mockRestore();
  });

  it('unregisters the leave guard when the screen unmounts', async () => {
    const { unmount } = await loaded();

    unmount();

    expect(mocks.guardDispose).toHaveBeenCalled();
  });

  it('sends only the changed identity fields with the baseline revision and CSRF token', async () => {
    const updated = entity({ name: 'Ada L.', revision: 4 });
    mocks.api.updateEntity.mockResolvedValue(updated);
    // The post-save invalidation refetches the entity; the server now returns the new revision.
    mocks.api.getEntity.mockResolvedValueOnce(entity()).mockResolvedValue(updated);
    const { result } = await loaded();
    act(() => result.current.identity.setValue('name', 'Ada L.', { shouldDirty: true }));
    await waitFor(() => expect(result.current.identity.formState.dirtyFields.name).toBe(true));

    await act(async () => {
      await result.current.update.mutateAsync(result.current.identity.getValues());
    });

    expect(mocks.api.updateEntity).toHaveBeenCalledWith('e-1', { expected_revision: 3, reason: 'owner_review', name: 'Ada L.' }, 'csrf-1');
    await waitFor(() => expect(result.current.baselineRef.current?.revision).toBe(4));
  });

  it('rejects an identity save with no changed field without calling the API', async () => {
    const { result } = await loaded();

    await act(async () => {
      await expect(result.current.update.mutateAsync(result.current.identity.getValues())).rejects.toThrow('saveIdentity');
    });

    expect(mocks.api.updateEntity).not.toHaveBeenCalled();
  });

  it('refuses to preview a merge without a chosen target and previews with both revisions once chosen', async () => {
    const { result } = await loaded();
    const values = { targetId: 'e-2', splitName: '', reason: 'owner_review' };

    await act(async () => {
      await expect(result.current.mergePreview.mutateAsync(values)).rejects.toThrow('chooseMergeTarget');
    });
    expect(mocks.api.previewMergeEntity).not.toHaveBeenCalled();

    mocks.api.previewMergeEntity.mockResolvedValue({ operation: 'merge', conflicts: [] });
    act(() => result.current.setSelectedMergeTarget(entity({ id: 'e-2', name: 'Grace', revision: 7 })));
    await act(async () => {
      await result.current.mergePreview.mutateAsync(values);
    });

    expect(mocks.api.previewMergeEntity).toHaveBeenCalledWith('e-1', {
      into_id: 'e-2',
      expected_revision: 3,
      expected_into_revision: 7,
      reason: 'owner_review',
    });
  });

  it('rejects a merge target of a different type', async () => {
    const { result } = await loaded();
    act(() => result.current.setSelectedMergeTarget(entity({ id: 'e-2', type: 'organization' })));

    await act(async () => {
      await expect(result.current.mergePreview.mutateAsync({ targetId: 'e-2', splitName: '', reason: 'owner_review' })).rejects.toThrow(
        'mergeTargetTypeChanged',
      );
    });
  });

  it('requires selected evidence before previewing a split and routes to the replacement entity after splitting', async () => {
    mocks.api.previewSplitEntity.mockResolvedValue({ operation: 'split', conflicts: [] });
    mocks.api.splitEntity.mockResolvedValue({
      operation: 'split',
      entity_id: 'e-1',
      canonical_entity_id: 'e-1',
      replacement_entity_ids: ['e-9'],
      revision: 4,
      conflicts: [],
    });
    const { result } = await loaded();
    const values = { targetId: '', splitName: 'Ada Two', reason: 'owner_review' };

    await act(async () => {
      await expect(result.current.splitPreview.mutateAsync(values)).rejects.toThrow('selectEvidence');
    });

    act(() => {
      result.current.setSelected(['ev-1']);
      result.current.setSelectionRevision(3);
    });
    await act(async () => {
      await result.current.splitPreview.mutateAsync(values);
    });
    expect(mocks.api.previewSplitEntity).toHaveBeenCalledWith('e-1', {
      evidence_ids: ['ev-1'],
      expected_revision: 3,
      new_entity: { type: 'person', name: 'Ada Two', reason: 'owner_review' },
      reason: 'owner_review',
    });

    await act(async () => {
      await result.current.split.mutateAsync();
    });
    expect(mocks.api.splitEntity).toHaveBeenCalledWith('e-1', expect.objectContaining({ evidence_ids: ['ev-1'] }), 'csrf-1');
    expect(mocks.replace).toHaveBeenCalledWith('/knowledge/entities/e-9');
    await waitFor(() => expect(result.current.selected).toEqual([]));
  });

  it('adds an alias with the confirmed flag and resets the alias form', async () => {
    mocks.api.addEntityAlias.mockResolvedValue(entity({ revision: 4 }));
    const { result } = await loaded();

    await act(async () => {
      await result.current.addAlias.mutateAsync({ alias: 'A. Lovelace', reason: 'owner_review' });
    });

    expect(mocks.api.addEntityAlias).toHaveBeenCalledWith(
      'e-1',
      { alias: 'A. Lovelace', confirmed: true, reason: 'owner_review' },
      'csrf-1',
    );
    await waitFor(() => expect(result.current.aliasForm.getValues('alias')).toBe(''));
  });
});
