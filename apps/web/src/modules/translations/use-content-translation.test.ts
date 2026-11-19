import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, renderHook } from '@testing-library/react';
import { createElement, type ReactNode } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { chunk, MAX_POLL_MS, POLL_MS, useContentTranslation } from './use-content-translation';

const api = vi.hoisted(() => ({
  fetchTranslationSettings: vi.fn(),
  submitTranslationBatch: vi.fn(),
  readTranslationBatch: vi.fn(),
}));
const ws = vi.hoisted(() => ({ id: 'w1', generation: 1 }));
const core = vi.hoisted(() => {
  class ApiError extends Error {
    constructor(
      readonly status: number,
      message: string,
    ) {
      super(message);
    }
  }
  return { ApiError };
});
vi.mock('./api', () => api);
vi.mock('@/core/app-shell/workspace-session', () => ({ useWorkspaceSession: () => ({ csrfToken: 'c' }) }));
vi.mock('@/core/workspace-context', () => ({ useWorkspace: () => ({ selection: { id: ws.id } }) }));
vi.mock('@/core/api', () => ({ workspaceGeneration: () => ws.generation, ApiError: core.ApiError }));

const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
const wrapper = ({ children }: { children: ReactNode }) => createElement(QueryClientProvider, { client }, children);
const ids = (n: number) => Array.from({ length: n }, (_, i) => ({ id: `s${i}`, revision: 'r' }));
const settle = () =>
  act(async () => {
    await vi.advanceTimersByTimeAsync(0);
  });
const accepted = (items: Array<{ resource_id: string; status: string }>, batch = 'b1') => ({
  batch_id: batch,
  items: items.map((i) => ({ resource_type: 'news_story', ...i })),
});

describe('useContentTranslation', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    client.clear();
    ws.id = 'w1';
    ws.generation = 1;
    (Object.values(api) as Array<{ mockReset(): void }>).forEach((fn) => fn.mockReset());
    api.fetchTranslationSettings.mockResolvedValue({ enabled: true, target_language: 'vi', configuration_revision: 3 });
  });
  afterEach(() => vi.useRealTimers());

  it('chunks references by 25', () => {
    expect(chunk(ids(60)).map((g) => g.length)).toEqual([25, 25, 10]);
  });

  it('does not auto-submit on mount and only translates when requested, in chunks of at most 25', async () => {
    api.submitTranslationBatch.mockImplementation(async (items: Array<{ resource_id: string }>) =>
      accepted(items.map((i) => ({ resource_id: i.resource_id, status: 'unchanged' }))),
    );
    const { result } = renderHook(() => useContentTranslation('news_story', ids(30)), { wrapper });
    await settle();
    await settle();
    expect(api.submitTranslationBatch).not.toHaveBeenCalled();

    act(() => {
      result.current.translate();
    });
    await settle();
    await settle();
    const sizes = api.submitTranslationBatch.mock.calls.map((c: unknown[][]) => c[0].length).sort((a: number, b: number) => a - b);
    expect(sizes).toEqual([5, 25]);
    expect(
      api.submitTranslationBatch.mock.calls
        .flatMap((c: unknown[][]) => c[0] as Array<{ resource_id: string }>)
        .every((i: { resource_id: string }) => /^s\d+$/.test(i.resource_id)),
    ).toBe(true);
  });

  it('hydrates DB cache hits (status=ready without payload) and cached blocked error codes immediately via readTranslationBatch', async () => {
    api.submitTranslationBatch.mockResolvedValue(
      accepted([
        { resource_id: 's0', status: 'ready' },
        { resource_id: 's1', status: 'blocked' },
      ]),
    );
    api.readTranslationBatch.mockResolvedValue({
      batch_id: 'b1',
      target_language: 'vi',
      items: [
        {
          resource_type: 'news_story',
          resource_id: 's0',
          status: 'ready',
          translation: { title: 'Tieu de da dich' },
          original_revision: 'r',
          error_code: null,
        },
        {
          resource_type: 'news_story',
          resource_id: 's1',
          status: 'blocked',
          translation: null,
          original_revision: 'r',
          error_code: 'model_capability_missing',
        },
      ],
    });
    const { result } = renderHook(() => useContentTranslation('news_story', ids(2)), { wrapper });
    await settle();
    act(() => {
      result.current.translate();
    });
    await settle();
    await settle();
    expect(api.readTranslationBatch).toHaveBeenCalledTimes(1);
    expect(api.readTranslationBatch).toHaveBeenCalledWith('b1', expect.any(AbortSignal));
    expect(result.current.results.get('s0')?.translation?.title).toBe('Tieu de da dich');
    expect(result.current.issues.get('s1')?.errorCode).toBe('model_capability_missing');
  });

  it('drops a late result after a workspace switch', async () => {
    let resolve!: (value: unknown) => void;
    api.submitTranslationBatch.mockReturnValue(
      new Promise((r) => {
        resolve = r;
      }),
    );
    const { result } = renderHook(() => useContentTranslation('news_story', ids(1)), { wrapper });
    await settle();
    act(() => {
      result.current.translate();
    });
    ws.generation = 2;
    await act(async () => {
      resolve({
        ...accepted([{ resource_id: 's0', status: 'ready' }]),
        items: [{ resource_type: 'news_story', resource_id: 's0', status: 'ready', translation: { title: 'x' } }],
      });
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(result.current.results.size).toBe(0);
  });

  it('stops polling after 60 s and does not resubmit', async () => {
    api.submitTranslationBatch.mockResolvedValue(accepted([{ resource_id: 's0', status: 'pending' }]));
    api.readTranslationBatch.mockResolvedValue({
      batch_id: 'b1',
      items: [{ resource_type: 'news_story', resource_id: 's0', status: 'pending', original_revision: 'r' }],
    });
    const { result } = renderHook(() => useContentTranslation('news_story', ids(1)), { wrapper });
    await settle();
    act(() => {
      result.current.translate();
    });
    await settle();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(MAX_POLL_MS + POLL_MS * 3);
    });
    const polls = api.readTranslationBatch.mock.calls.length;
    expect(polls).toBeLessThanOrEqual(MAX_POLL_MS / POLL_MS);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_MS * 5);
    });
    expect(api.readTranslationBatch.mock.calls.length).toBe(polls);
    expect(api.submitTranslationBatch).toHaveBeenCalledTimes(1);
  });

  it('shares one cache between two consumers and exposes blocked reasons', async () => {
    api.submitTranslationBatch.mockResolvedValue({
      batch_id: null,
      items: [
        { resource_type: 'news_story', resource_id: 's0', status: 'ready', translation: { title: 'x' }, original_revision: 'r' },
        { resource_type: 'news_story', resource_id: 's1', status: 'blocked', error_code: 'privacy_blocked' },
      ],
    });
    const first = renderHook(() => useContentTranslation('news_story', ids(2)), { wrapper });
    await settle();
    act(() => {
      first.result.current.translate();
    });
    await settle();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(50);
    });
    const second = renderHook(() => useContentTranslation('news_story', ids(2)), { wrapper });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(50);
    });
    expect(api.submitTranslationBatch).toHaveBeenCalledTimes(1);
    for (const hook of [first, second]) {
      expect(hook.result.current.results.get('s0')?.translation?.title).toBe('x');
      expect(hook.result.current.issues.get('s1')?.errorCode).toBe('privacy_blocked');
    }
  });

  it('clears a batch that stays pending past the deadline so the button is not stuck', async () => {
    api.submitTranslationBatch.mockResolvedValue(accepted([{ resource_id: 's0', status: 'pending' }]));
    api.readTranslationBatch.mockResolvedValue({
      batch_id: 'b1',
      items: [{ resource_type: 'news_story', resource_id: 's0', status: 'pending', original_revision: 'r' }],
    });
    const { result } = renderHook(() => useContentTranslation('news_story', ids(1)), { wrapper });
    await settle();
    act(() => {
      result.current.translate();
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(MAX_POLL_MS + POLL_MS * 3);
    });
    expect(result.current.pendingIds.size).toBe(0);
  });

  it('drops a pending cache entry on unmount so a remounted hook is not stuck', async () => {
    api.submitTranslationBatch.mockResolvedValue(accepted([{ resource_id: 's0', status: 'pending' }]));
    api.readTranslationBatch.mockResolvedValue({
      batch_id: 'b1',
      items: [{ resource_type: 'news_story', resource_id: 's0', status: 'pending', original_revision: 'r' }],
    });
    const first = renderHook(() => useContentTranslation('news_story', ids(1)), { wrapper });
    await settle();
    act(() => {
      first.result.current.translate();
    });
    await settle();
    first.unmount();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_MS * 2);
    });
    const second = renderHook(() => useContentTranslation('news_story', ids(1)), { wrapper });
    await settle();
    expect(second.result.current.pendingIds.size).toBe(0);
  });

  it('records rate_limited for every target when the submit is rejected with 429', async () => {
    api.submitTranslationBatch.mockRejectedValue(new core.ApiError(429, 'slow down'));
    const { result } = renderHook(() => useContentTranslation('news_story', ids(2)), { wrapper });
    await settle();
    act(() => {
      result.current.translate();
    });
    await settle();
    await settle();
    expect(result.current.issues.get('s0')?.errorCode).toBe('rate_limited');
    expect(result.current.issues.get('s0')?.status).toBe('failed');
    expect(result.current.pendingIds.size).toBe(0);
  });

  it('records stale_request for every group item when the submit is rejected with 409', async () => {
    api.submitTranslationBatch.mockRejectedValue(new core.ApiError(409, 'stale'));
    const { result } = renderHook(() => useContentTranslation('news_story', ids(2)), { wrapper });
    await settle();
    act(() => {
      result.current.translate();
    });
    await settle();
    await settle();
    expect(result.current.issues.get('s0')?.errorCode).toBe('stale_request');
    expect(result.current.issues.get('s1')?.errorCode).toBe('stale_request');
  });

  it('marks an accepted unchanged item as unchanged', async () => {
    api.submitTranslationBatch.mockResolvedValue(accepted([{ resource_id: 's0', status: 'unchanged' }], null as never));
    const { result } = renderHook(() => useContentTranslation('news_story', ids(1)), { wrapper });
    await settle();
    act(() => {
      result.current.translate();
    });
    await settle();
    await settle();
    expect(result.current.unchanged.has('s0')).toBe(true);
  });
});
