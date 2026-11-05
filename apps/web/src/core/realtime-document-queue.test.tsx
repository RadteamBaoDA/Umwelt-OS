import { QueryClient } from '@tanstack/react-query';
import { act, renderHook } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { useDocumentUpdateQueue } from '@/core/realtime-provider';

vi.mock('next-intl', () => ({ useTranslations: () => (key: string) => key }));
vi.mock('@/core/query-provider', () => ({ useDisplayPreferences: () => ({ authGeneration: 1, isCurrentGeneration: () => true }) }));

/** Mounts the queue hook with a client whose document invalidation is controlled by the test. */
function setup(invalidate: () => Promise<void> = async () => {}) {
  const client = new QueryClient();
  vi.spyOn(client, 'invalidateQueries').mockImplementation(invalidate as never);
  return renderHook(() => useDocumentUpdateQueue(client, 1, () => true));
}

describe('useDocumentUpdateQueue', () => {
  it('counts distinct ids and does not require a full refresh', () => {
    const { result } = setup();
    act(() => { result.current.queueDocumentUpdate('a'); result.current.queueDocumentUpdate('b'); result.current.queueDocumentUpdate('a'); });
    expect(result.current.newDocumentCount).toBe(2);
    expect(result.current.documentRefreshRequired).toBe(false);
  });

  it('degrades to a full refresh when an event has no id', () => {
    const { result } = setup();
    act(() => { result.current.queueDocumentUpdate('a'); result.current.queueDocumentUpdate(null); });
    expect(result.current.newDocumentCount).toBe(0);
    expect(result.current.documentRefreshRequired).toBe(true);
  });

  it('turns the 501st distinct id into a full refresh', () => {
    const { result } = setup();
    act(() => { for (let i = 0; i < 500; i += 1) result.current.queueDocumentUpdate(`d${i}`); });
    expect(result.current.newDocumentCount).toBe(500);
    expect(result.current.documentRefreshRequired).toBe(false);
    act(() => { result.current.queueDocumentUpdate('overflow'); });
    expect(result.current.documentRefreshRequired).toBe(true);
    expect(result.current.newDocumentCount).toBe(0);
  });

  it('consume clears only the revisions its refetch covered', async () => {
    let release: () => void = () => {};
    const { result } = setup(() => new Promise<void>((resolve) => { release = resolve; }));
    act(() => { result.current.queueDocumentUpdate('a'); });
    let pending: Promise<void> = Promise.resolve();
    act(() => { pending = result.current.consumeDocumentUpdates(); });
    // 'b' arrives and 'a' is re-queued with a newer revision while the refetch is in flight.
    act(() => { result.current.queueDocumentUpdate('b'); result.current.queueDocumentUpdate('a'); });
    await act(async () => { release(); await pending; });
    expect(result.current.newDocumentCount).toBe(2);
  });

  it('consume clears a covered full-refresh requirement and records failures', async () => {
    const { result } = setup();
    act(() => { result.current.queueDocumentUpdate(null); });
    await act(async () => { await result.current.consumeDocumentUpdates(); });
    expect(result.current.documentRefreshRequired).toBe(false);
    expect(result.current.documentRefreshFailed).toBe(false);

    const failing = setup(async () => { throw new Error('boom'); });
    await act(async () => { await failing.result.current.consumeDocumentUpdates(); });
    expect(failing.result.current.documentRefreshFailed).toBe(true);
  });

  it('resetDocumentState drops everything', () => {
    const { result } = setup();
    act(() => { result.current.queueDocumentUpdate('a'); result.current.queueDocumentUpdate(null); });
    act(() => { result.current.resetDocumentState(); });
    expect(result.current.newDocumentCount).toBe(0);
    expect(result.current.documentRefreshRequired).toBe(false);
  });
});
