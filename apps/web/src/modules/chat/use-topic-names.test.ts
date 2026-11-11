import { createElement, type ReactNode } from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { renderHook as baseRenderHook, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { useTopicNames } from './use-topic-names';

const fetchTopics = vi.hoisted(() => vi.fn());
vi.mock('@/modules/news', () => ({ fetchTopics }));

/** Renders the hook inside a fresh query client so cache never leaks between tests. */
function renderHook<T>(callback: () => T) {
  const client = new QueryClient();
  return baseRenderHook(callback, { wrapper: ({ children }: { children: ReactNode }) => createElement(QueryClientProvider, { client }, children) });
}

beforeEach(() => {
  fetchTopics.mockReset();
});

describe('useTopicNames', () => {
  it('requests active topics with the limit and returns their names', async () => {
    fetchTopics.mockResolvedValue({ items: [{ name: 'AI' }, { name: 'Markets' }] });
    const { result } = renderHook(() => useTopicNames(2));

    expect(result.current).toEqual([]);
    await waitFor(() => expect(result.current).toEqual(['AI', 'Markets']));
    expect(fetchTopics).toHaveBeenCalledWith(expect.objectContaining({ isActive: true, limit: 2, signal: expect.any(AbortSignal) }));
  });

  it('defaults the limit to three', async () => {
    fetchTopics.mockResolvedValue({ items: [] });
    renderHook(() => useTopicNames());
    await waitFor(() => expect(fetchTopics).toHaveBeenCalledWith(expect.objectContaining({ limit: 3 })));
  });

  it('yields an empty list when the request fails', async () => {
    fetchTopics.mockRejectedValue(new Error('offline'));
    const { result } = renderHook(() => useTopicNames());
    await waitFor(() => expect(fetchTopics).toHaveBeenCalled());
    expect(result.current).toEqual([]);
  });

  it('aborts the in-flight request on unmount', () => {
    fetchTopics.mockReturnValue(new Promise(() => {}));
    const { unmount } = renderHook(() => useTopicNames());
    const signal = fetchTopics.mock.calls[0][0].signal as AbortSignal;
    unmount();
    expect(signal.aborted).toBe(true);
  });
});
