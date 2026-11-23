import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, renderHook } from '@testing-library/react';
import { createElement, type ReactNode } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
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

  it('translateOne submits only the requested item', async () => {
    const { result } = renderHook(() => useItemTranslation('document', targets(3)), { wrapper });
    await settle();
    act(() => {
      result.current.translateOne({ id: 'd1', revision: 'r' });
    });
    await settle();
    expect(api.submitTranslationBatch).toHaveBeenCalledTimes(1);
    expect(api.submitTranslationBatch.mock.calls[0][0]).toEqual([expect.objectContaining({ resource_id: 'd1' })]);
  });
});
