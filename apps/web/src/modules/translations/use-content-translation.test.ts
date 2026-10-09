import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, renderHook } from '@testing-library/react';
import { createElement, type ReactNode } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { chunk, MAX_POLL_MS, POLL_MS, useContentTranslation } from './use-content-translation';

const api = vi.hoisted(() => ({
  fetchTranslationSettings: vi.fn(), submitTranslationBatch: vi.fn(), readTranslationBatch: vi.fn(),
}));
const ws = vi.hoisted(() => ({ id: 'w1', generation: 1 }));
vi.mock('./api', () => api);
vi.mock('@/core/app-shell/workspace-shell', () => ({ useWorkspaceSession: () => ({ csrfToken: 'c' }) }));
vi.mock('@/core/workspace-context', () => ({ useWorkspace: () => ({ selection: { id: ws.id } }) }));
vi.mock('@/core/api', () => ({ workspaceGeneration: () => ws.generation }));

const wrapper = ({ children }: { children: ReactNode }) =>
  createElement(QueryClientProvider, { client: new QueryClient({ defaultOptions: { queries: { retry: false } } }) }, children);
const ids = (n: number) => Array.from({ length: n }, (_, i) => ({ id: `s${i}`, revision: 'r' }));
const settle = () => act(async () => { await vi.advanceTimersByTimeAsync(0); });
const accepted = (items: Array<{ resource_id: string; status: string }>, batch = 'b1') => ({
  batch_id: batch, items: items.map((i) => ({ resource_type: 'news_story', ...i })),
});

describe('useContentTranslation', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    ws.id = 'w1'; ws.generation = 1;
    (Object.values(api) as Array<{ mockReset(): void }>).forEach((fn) => fn.mockReset());
    api.fetchTranslationSettings.mockResolvedValue({ enabled: true, target_language: 'vi', configuration_revision: 3 });
  });
  afterEach(() => vi.useRealTimers());

  it('chunks references by 25', () => {
    expect(chunk(ids(60)).map((g) => g.length)).toEqual([25, 25, 10]);
  });

  it('requests only the supplied page ids, in chunks of at most 25', async () => {
    api.submitTranslationBatch.mockImplementation(async (items: Array<{ resource_id: string }>) =>
      accepted(items.map((i) => ({ resource_id: i.resource_id, status: 'unchanged' }))));
    renderHook(() => useContentTranslation('news_story', ids(30)), { wrapper });
    await settle(); await settle();
    const sizes = api.submitTranslationBatch.mock.calls.map((c: unknown[][]) => c[0].length).sort();
    expect(sizes).toEqual([5, 25]);
    expect(api.submitTranslationBatch.mock.calls.flatMap((c: unknown[][]) => c[0]).every((i: { resource_id: string }) => /^s\d+$/.test(i.resource_id))).toBe(true);
  });

  it('drops a late result after a workspace switch', async () => {
    let resolve!: (value: unknown) => void;
    api.submitTranslationBatch.mockReturnValue(new Promise((r) => { resolve = r; }));
    const { result } = renderHook(() => useContentTranslation('news_story', ids(1)), { wrapper });
    await settle();
    ws.generation = 2;
    await act(async () => {
      resolve({ ...accepted([{ resource_id: 's0', status: 'ready' }]), items: [{ resource_type: 'news_story', resource_id: 's0', status: 'ready', translation: { title: 'x' } }] });
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(result.current.results.size).toBe(0);
  });

  it('stops polling after 60 s and does not resubmit', async () => {
    api.submitTranslationBatch.mockResolvedValue(accepted([{ resource_id: 's0', status: 'pending' }]));
    api.readTranslationBatch.mockResolvedValue({ batch_id: 'b1', items: [{ resource_type: 'news_story', resource_id: 's0', status: 'pending', original_revision: 'r' }] });
    renderHook(() => useContentTranslation('news_story', ids(1)), { wrapper });
    await settle();
    await act(async () => { await vi.advanceTimersByTimeAsync(MAX_POLL_MS + POLL_MS * 3); });
    const polls = api.readTranslationBatch.mock.calls.length;
    expect(polls).toBeLessThanOrEqual(MAX_POLL_MS / POLL_MS);
    await act(async () => { await vi.advanceTimersByTimeAsync(POLL_MS * 5); });
    expect(api.readTranslationBatch.mock.calls.length).toBe(polls);
    expect(api.submitTranslationBatch).toHaveBeenCalledTimes(1);
  });
});
