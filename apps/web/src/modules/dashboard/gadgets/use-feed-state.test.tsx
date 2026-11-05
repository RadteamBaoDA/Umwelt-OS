import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, renderHook, waitFor } from '@testing-library/react';
import type { ReactNode } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { useAutoFetchWhenEmpty, useFeedFilters, useFeedModeration, useFeedQueries } from './use-feed-state';

const mocks = vi.hoisted(() => ({ listProjections: vi.fn(), setInteraction: vi.fn(), listSources: vi.fn() }));
const session = { csrfToken: 'csrf-1' };
vi.mock('@/core/app-shell/workspace-shell', () => ({ useWorkspaceSession: () => session }));
vi.mock('@/modules/knowledge', () => ({
  documentKeys: { all: ['documents'] },
  listGadgetDocumentProjections: mocks.listProjections,
  setGadgetDocumentInteraction: mocks.setInteraction,
}));
vi.mock('../api', () => ({ dashboardKeys: { sources: ['dash-sources'] }, listGadgetSources: mocks.listSources }));

function makeWrapper() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>;
  return { client, wrapper };
}
const identity = (n: number) => ({ sourceId: 's1', documentId: `d${n}`, documentVersionId: `v${n}`, versionNumber: n });

describe('useFeedFilters', () => {
  beforeEach(() => {
    vi.useFakeTimers({ toFake: ['Date'] });
    vi.setSystemTime(new Date('2026-06-15T12:00:00Z'));
  });
  afterEach(() => vi.useRealTimers());

  it('starts unfiltered', () => {
    const { result } = renderHook(() => useFeedFilters());
    expect(result.current).toMatchObject({
      onlyUnread: false,
      selectMode: false,
      searchText: '',
      sourceFilter: 'all',
      language: 'any',
      timeWindow: 'any',
      windowSince: undefined,
    });
  });

  it('freezes the window lower bound when a window is chosen and clears it for "any"', () => {
    const { result } = renderHook(() => useFeedFilters());
    act(() => result.current.setTimeWindow('24h'));
    expect(result.current.timeWindow).toBe('24h');
    expect(result.current.windowSince).toBe('2026-06-14T12:00:00.000Z');
    vi.setSystemTime(new Date('2026-06-16T12:00:00Z'));
    expect(result.current.windowSince).toBe('2026-06-14T12:00:00.000Z');
    act(() => result.current.setTimeWindow('any'));
    expect(result.current.windowSince).toBeUndefined();
  });
});

describe('useFeedQueries', () => {
  beforeEach(() => vi.clearAllMocks());

  it('returns an empty page without calling the API when there are no sources', async () => {
    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => useFeedQueries({ sourceIds: [], channelIds: [], language: 'any', windowSince: undefined }), {
      wrapper,
    });
    await waitFor(() => expect(result.current.docsQuery.isSuccess).toBe(true));
    expect(mocks.listProjections).not.toHaveBeenCalled();
    expect(result.current.sourcesQuery.fetchStatus).toBe('idle');
  });

  it('forwards the language and window filters and pages by cursor; lists sources only for several', async () => {
    mocks.listProjections
      .mockResolvedValueOnce({ items: [{ id: 'a' }], next_cursor: 'c2' })
      .mockResolvedValueOnce({ items: [{ id: 'b' }], next_cursor: null });
    mocks.listSources.mockResolvedValue({ items: [], next_cursor: null });
    const { wrapper } = makeWrapper();
    const { result } = renderHook(
      () => useFeedQueries({ sourceIds: ['s1', 's2'], channelIds: ['c1'], language: 'vi', windowSince: '2026-01-01T00:00:00Z' }),
      { wrapper },
    );
    await waitFor(() => expect(result.current.docsQuery.isSuccess).toBe(true));
    expect(mocks.listProjections).toHaveBeenCalledWith(['s1', 's2'], ['c1'], undefined, { language: 'vi', since: '2026-01-01T00:00:00Z' });
    expect(result.current.docsQuery.hasNextPage).toBe(true);
    await act(async () => {
      await result.current.docsQuery.fetchNextPage();
    });
    expect(mocks.listProjections).toHaveBeenLastCalledWith(['s1', 's2'], ['c1'], 'c2', expect.any(Object));
    await waitFor(() => expect(result.current.docsQuery.hasNextPage).toBe(false));
    expect(result.current.docsQuery.data?.pages).toHaveLength(2);
    await waitFor(() => expect(mocks.listSources).toHaveBeenCalled());
  });

  it('omits the language filter for "any"', async () => {
    mocks.listProjections.mockResolvedValue({ items: [], next_cursor: null });
    const { wrapper } = makeWrapper();
    renderHook(() => useFeedQueries({ sourceIds: ['s1'], channelIds: [], language: 'any', windowSince: undefined }), { wrapper });
    await waitFor(() => expect(mocks.listProjections).toHaveBeenCalled());
    expect(mocks.listProjections.mock.calls[0][3]).toEqual({ language: undefined, since: undefined });
  });
});

describe('useFeedModeration', () => {
  const refs = () => ({ undoRef: { current: null }, rootRef: { current: document.createElement('div') } });
  beforeEach(() => vi.clearAllMocks());

  it('hides items optimistically, sends a dismiss mutation and exposes them for undo', async () => {
    mocks.setInteraction.mockResolvedValue({});
    const { wrapper, client } = makeWrapper();
    const invalidate = vi.spyOn(client, 'invalidateQueries');
    const { result } = renderHook(() => useFeedModeration('inst-1', refs()), { wrapper });
    act(() => result.current.setHidden([identity(1), identity(2)], true));
    expect([...result.current.hiddenIds]).toEqual(['v1', 'v2']);
    expect(result.current.lastHidden).toHaveLength(2);
    await waitFor(() => expect(mocks.setInteraction).toHaveBeenCalledTimes(2));
    expect(mocks.setInteraction).toHaveBeenCalledWith('d1', 1, { read: undefined, bookmarked: undefined, dismissed: true }, 'csrf-1');
    await waitFor(() => expect(invalidate).toHaveBeenCalledWith({ queryKey: ['documents'] }));
  });

  it('undo clears hidden ids and last-hidden and focuses the list root', async () => {
    mocks.setInteraction.mockResolvedValue({});
    const rootRef = { current: document.createElement('div') };
    document.body.append(rootRef.current);
    rootRef.current.tabIndex = -1;
    const { result } = renderHook(() => useFeedModeration('inst-1', { undoRef: { current: null }, rootRef }), {
      wrapper: makeWrapper().wrapper,
    });
    act(() => result.current.setHidden([identity(1)], true));
    act(() => result.current.setHidden([identity(1)], false));
    expect(result.current.hiddenIds.size).toBe(0);
    expect(result.current.lastHidden).toEqual([]);
    expect(document.activeElement).toBe(rootRef.current);
    await waitFor(() =>
      expect(mocks.setInteraction).toHaveBeenLastCalledWith('d1', 1, expect.objectContaining({ dismissed: false }), 'csrf-1'),
    );
    rootRef.current.remove();
  });

  it('a failed hide restores the item and flags hideFailed', async () => {
    mocks.setInteraction.mockRejectedValue(new Error('nope'));
    const { result } = renderHook(() => useFeedModeration('inst-1', refs()), { wrapper: makeWrapper().wrapper });
    act(() => result.current.setHidden([identity(1)], true));
    await waitFor(() => expect(result.current.hideFailed).toBe(true));
    expect(result.current.hiddenIds.size).toBe(0);
  });

  it('a failed undo re-hides the item, offers a retry and flags undoFailed', async () => {
    mocks.setInteraction.mockRejectedValue(new Error('nope'));
    const { result } = renderHook(() => useFeedModeration('inst-1', refs()), { wrapper: makeWrapper().wrapper });
    act(() => result.current.setHidden([identity(1)], false));
    await waitFor(() => expect(result.current.undoFailed).toBe(true));
    expect([...result.current.hiddenIds]).toEqual(['v1']);
    expect(result.current.lastHidden.map((i) => i.documentVersionId)).toEqual(['v1']);
  });
});

describe('useAutoFetchWhenEmpty', () => {
  const docsQuery = (overrides: Record<string, unknown> = {}) =>
    ({ hasNextPage: true, isFetching: false, isError: false, fetchNextPage: vi.fn(), ...overrides }) as never;
  const args = (q: unknown, overrides: Record<string, unknown> = {}) => ({
    docsQuery: q as never,
    displayedCount: 0,
    filtering: false,
    hasInitialItems: false,
    serverScope: 'a',
    ...overrides,
  });

  it('fetches the next page while nothing is displayed, at most three times per scope', () => {
    const q = docsQuery();
    const { rerender } = renderHook((props) => useAutoFetchWhenEmpty(props), { initialProps: args(q) });
    for (let i = 0; i < 5; i += 1) rerender(args(docsQuery({ fetchNextPage: (q as { fetchNextPage: () => void }).fetchNextPage })));
    expect((q as { fetchNextPage: ReturnType<typeof vi.fn> }).fetchNextPage).toHaveBeenCalledTimes(3);
  });

  it('does nothing when items are shown, filtering, initial items exist, or no more pages', () => {
    for (const override of [{ displayedCount: 2 }, { filtering: true }, { hasInitialItems: true }]) {
      const q = docsQuery();
      renderHook(() => useAutoFetchWhenEmpty(args(q, override)));
      expect((q as { fetchNextPage: ReturnType<typeof vi.fn> }).fetchNextPage).not.toHaveBeenCalled();
    }
    for (const override of [{ hasNextPage: false }, { isFetching: true }, { isError: true }]) {
      const q = docsQuery(override);
      renderHook(() => useAutoFetchWhenEmpty(args(q)));
      expect((q as { fetchNextPage: ReturnType<typeof vi.fn> }).fetchNextPage).not.toHaveBeenCalled();
    }
  });

  it('resets the budget when the server scope changes', () => {
    const fetchNextPage = vi.fn();
    const { rerender } = renderHook((props) => useAutoFetchWhenEmpty(props), { initialProps: args(docsQuery({ fetchNextPage })) });
    for (let i = 0; i < 4; i += 1) rerender(args(docsQuery({ fetchNextPage })));
    expect(fetchNextPage).toHaveBeenCalledTimes(3);
    rerender(args(docsQuery({ fetchNextPage }), { serverScope: 'b' }));
    expect(fetchNextPage).toHaveBeenCalledTimes(4);
  });
});
