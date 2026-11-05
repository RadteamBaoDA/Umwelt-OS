import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { renderHook, waitFor } from '@testing-library/react';
import type { ReactNode } from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { useEffectiveLookback, useSourceInfo } from './use-effective-lookback';

const api = vi.hoisted(() => ({ getMetricCatalog: vi.fn(), listGadgetSources: vi.fn() }));
vi.mock('../api', () => ({ dashboardKeys: { sources: ['dash-sources'], metricCatalog: ['metric-catalog'] }, ...api }));

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

describe('useSourceInfo', () => {
  beforeEach(() => vi.clearAllMocks());

  it('does not query without ids', () => {
    const { result } = renderHook(() => useSourceInfo([]), { wrapper });
    expect(result.current.fetchStatus).toBe('idle');
    expect(api.listGadgetSources).not.toHaveBeenCalled();
  });

  it('pages through sources until every wanted id is found and ignores others', async () => {
    api.listGadgetSources
      .mockResolvedValueOnce({
        items: [
          { id: 'x', provider: 'noise', name: 'Noise' },
          { id: 's1', provider: 'binance', name: 'BTC' },
        ],
        next_cursor: 'p2',
      })
      .mockResolvedValueOnce({ items: [{ id: 's2', provider: null, name: null }], next_cursor: 'p3' });
    const { result } = renderHook(() => useSourceInfo(['s1', 's2']), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toEqual({ s1: { provider: 'binance', name: 'BTC' }, s2: { provider: null, name: '' } });
    expect(api.listGadgetSources).toHaveBeenCalledTimes(2);
    expect(api.listGadgetSources).toHaveBeenNthCalledWith(2, 100, 'p2', expect.any(AbortSignal));
  });
});

describe('useEffectiveLookback', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.getMetricCatalog.mockResolvedValue({ binance: { default_lookback_days: 3 }, open_meteo: { default_lookback_days: 7 } });
    api.listGadgetSources.mockResolvedValue({
      items: [
        { id: 's1', provider: 'binance', name: 'BTC' },
        { id: 's2', provider: 'open_meteo', name: 'Hanoi' },
      ],
      next_cursor: null,
    });
  });

  it('uses the stored lookback immediately for a single source', () => {
    const { result } = renderHook(() => useEffectiveLookback('s1', 14, 30), { wrapper });
    expect(result.current.lookbackDays).toBe(14);
    expect(result.current.ready).toBe(true);
  });

  it('falls back to the caller default while loading, then to the provider catalog default', async () => {
    const { result } = renderHook(() => useEffectiveLookback('s1', null, 30), { wrapper });
    expect(result.current).toMatchObject({ lookbackDays: 30, ready: false });
    await waitFor(() => expect(result.current.ready).toBe(true));
    expect(result.current.lookbackDays).toBe(3);
    expect(result.current.sources.s1.provider).toBe('binance');
  });

  it('widenToFallback never goes below the fallback', async () => {
    const { result } = renderHook(() => useEffectiveLookback('s1', undefined, 30, true), { wrapper });
    await waitFor(() => expect(result.current.ready).toBe(true));
    expect(result.current.lookbackDays).toBe(30);
  });

  it('several sources wait for the lookup and use the slowest provider default', async () => {
    const { result } = renderHook(() => useEffectiveLookback(['s1', 's2'], 14, 30), { wrapper });
    expect(result.current.ready).toBe(false);
    await waitFor(() => expect(result.current.ready).toBe(true));
    expect(result.current.lookbackDays).toBe(14);
    const unset = renderHook(() => useEffectiveLookback(['s1', 's2'], null, 30), { wrapper });
    await waitFor(() => expect(unset.result.current.ready).toBe(true));
    expect(unset.result.current.lookbackDays).toBe(7);
  });

  it('an empty source id issues no source lookup', () => {
    renderHook(() => useEffectiveLookback('', null, 30), { wrapper });
    expect(api.listGadgetSources).not.toHaveBeenCalled();
  });
});
