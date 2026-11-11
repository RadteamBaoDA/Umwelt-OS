import { act, renderHook, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { ReactNode } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { GadgetInstance } from '../api';

const list = vi.hoisted(() => vi.fn());
vi.mock('@/modules/observations', () => ({ listGeospatialObservations: list }));
vi.mock('@/core/query-provider', () => ({ useDisplayPreferences: () => ({ locale: 'en-us', timezone: 'UTC' }) }));
import { useMapObservations } from './map-gadget';

const instance = {
  id: 'map',
  definition: { source_ids: ['s1'], filters: { show_precise_locations: true }, scope: {} },
} as unknown as GadgetInstance;
const scope = { sourceIds: ['s1'], regions: [] as string[], layerIds: [] as string[], precise: true, engine: 'globe' as const, days: 1 };

describe('map observation window', () => {
  afterEach(() => {
    vi.useRealTimers();
    list.mockReset();
  });

  it('keeps one anchor across pages and re-anchors on refetch', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.setSystemTime(new Date('2026-11-11T00:00:00Z'));
    list.mockImplementation(async ({ cursor }: { cursor?: string }) => ({ items: [], next_cursor: cursor ? null : 'c2' }));
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>;
    const { result } = renderHook(() => useMapObservations(instance, scope), { wrapper });
    await waitFor(() => expect(list).toHaveBeenCalledTimes(1));
    await act(() => result.current.observations.fetchNextPage());
    const [first, second] = list.mock.calls.map(([filters]) => filters);
    expect(second.cursor).toBe('c2');
    expect(second.to.getTime()).toBe(first.to.getTime());
    expect(second.from.getTime()).toBe(first.from.getTime());

    vi.setSystemTime(new Date('2026-11-11T01:00:00Z'));
    await act(() => result.current.observations.refetch());
    const [refetchedFirst, refetchedSecond] = list.mock.calls.slice(2).map(([filters]) => filters);
    expect(refetchedFirst.to.toISOString()).toBe('2026-11-11T01:00:00.000Z');
    expect(refetchedSecond.to.getTime()).toBe(refetchedFirst.to.getTime());
  });
});
