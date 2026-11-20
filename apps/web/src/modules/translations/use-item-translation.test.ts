import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, renderHook } from '@testing-library/react';
import { createElement, type ReactNode } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { POLL_MS } from './use-content-translation';
import { useItemTranslation } from './use-item-translation';

const api = vi.hoisted(() => ({
  fetchTranslationSettings: vi.fn(),
  submitTranslationBatch: vi.fn(),
  readTranslationBatch: vi.fn(),
}));
vi.mock('./api', () => api);
vi.mock('@/core/app-shell/workspace-session', () => ({ useWorkspaceSession: () => ({ csrfToken: 'c' }) }));
vi.mock('@/core/workspace-context', () => ({ useWorkspace: () => ({ selection: { id: 'w1' } }) }));
vi.mock('@/core/api', () => ({ workspaceGeneration: () => 1, ApiError: class extends Error {} }));

const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
const wrapper = ({ children }: { children: ReactNode }) => createElement(QueryClientProvider, { client }, children);
const targets = (n: number) => Array.from({ length: n }, (_, i) => ({ id: `d${i}`, revision: 'r' }));
const settle = () =>
  act(async () => {
    await vi.advanceTimersByTimeAsync(0);
  });

describe('useItemTranslation', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    client.clear();
    (Object.values(api) as Array<{ mockReset(): void }>).forEach((fn) => fn.mockReset());
    api.fetchTranslationSettings.mockResolvedValue({ enabled: true, target_language: 'vi', configuration_revision: 1 });
    api.submitTranslationBatch.mockImplementation(async (items: Array<{ resource_id: string }>) => ({
      batch_id: null,
      items: items.map((i) => ({ resource_type: 'document', resource_id: i.resource_id, status: 'unchanged' })),
      settings: { enabled: true, target_language: 'vi', configuration_revision: 1 },
    }));
  });
  afterEach(() => vi.useRealTimers());

  it('translateAll submits only the first 25 untranslated targets per click', async () => {
    const { result } = renderHook(() => useItemTranslation('document', targets(30)), { wrapper });
    await settle();
    await settle();
    expect(result.current.enabled).toBe(true);
    act(() => {
      result.current.translateAll();
    });
    await settle();
    await settle();
    expect(api.submitTranslationBatch).toHaveBeenCalledTimes(1);
    expect(api.submitTranslationBatch.mock.calls[0][0]).toHaveLength(25);
  });

  it('translateAll reports run progress that increments as results arrive and clears when the run completes', async () => {
    api.submitTranslationBatch.mockImplementation(async (items: Array<{ resource_id: string }>) => ({
      batch_id: 'b1',
      settings: { enabled: true, target_language: 'vi', configuration_revision: 1 },
      items: items.map((i) => ({ resource_type: 'document', resource_id: i.resource_id, status: 'pending' })),
    }));
    const { result } = renderHook(() => useItemTranslation('document', targets(30)), { wrapper });
    await settle();
    await settle();
    act(() => {
      result.current.translateAll();
    });
    await settle();
    expect(result.current.progress).toEqual({ done: 0, total: 25 });
    api.readTranslationBatch.mockResolvedValue({
      items: targets(25).map((t, i) => ({
        resource_type: 'document',
        resource_id: t.id,
        status: i < 4 ? 'unchanged' : 'pending',
        original_revision: 'r',
      })),
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_MS);
    });
    expect(result.current.progress).toEqual({ done: 4, total: 25 });
    api.readTranslationBatch.mockResolvedValue({
      items: targets(25).map((t) => ({ resource_type: 'document', resource_id: t.id, status: 'unchanged', original_revision: 'r' })),
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_MS);
    });
    expect(result.current.progress).toBeNull();
  });
});
