import { render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { NextIntlClientProvider } from 'next-intl';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { dashboardMessages } from '@/core/messages/dashboard';
import { ProviderObservationGadget, latestPerSeries } from './provider-observation-gadget';

const freshness = vi.hoisted(() => ({ report: vi.fn() }));
vi.mock('../gadget-freshness', () => ({ useReportGadgetFreshness: freshness.report }));
const api = vi.hoisted(() => ({ listWorldObservationWindow: vi.fn(), getConnectorConfiguration: vi.fn(), getMetricCatalog: vi.fn(), listGadgetSources: vi.fn() }));
vi.mock('../api', async (original) => ({ ...(await original<typeof import('../api')>()), getMetricCatalog: api.getMetricCatalog, listGadgetSources: api.listGadgetSources }));
vi.mock('@/modules/observations/api', () => ({ listWorldObservationWindow: api.listWorldObservationWindow }));
vi.mock('@/modules/sources/api', () => ({ getConnectorConfiguration: api.getConnectorConfiguration }));
vi.mock('@/core/query-provider', () => ({ useDisplayPreferences: () => ({ locale: 'en-us', timezone: 'UTC' }) }));

const base = {
  source_id: 's1', external_id: 'e', revision: 1, symbol: null, region: null, latitude: null, longitude: null,
  currency: null, timezone: null, quality: 'provider_reported', missing_reason: null, provider_delay_seconds: null,
  document_id: 'd', document_version_id: 'v', document_version_number: 1,
  published_at: null, collected_at: new Date().toISOString(), reference_date: null, period: null, unit: 'u',
};
const instance = { id: 'g1', title: '', definition: { source_ids: ['s1'], scope: { metrics: [] } } } as never;

function view(gadget = instance) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}><NextIntlClientProvider locale="en-US" messages={{ dashboard: dashboardMessages['en-us'] }}><ProviderObservationGadget instance={gadget} /></NextIntlClientProvider></QueryClientProvider>);
}

describe('ProviderObservationGadget', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.getConnectorConfiguration.mockResolvedValue({ configuration: { schedule_interval_minutes: 30 } });
    api.getMetricCatalog.mockResolvedValue({ world_bank: { default_lookback_days: 3650 }, noaa_kp: { default_lookback_days: null }, binance: { default_lookback_days: 3 } });
    api.listGadgetSources.mockResolvedValue({ items: [{ id: 's1', provider: 'noaa_kp' }], next_cursor: null });
  });

  it('queries a legacy World Bank Metric gadget without a stored lookback for about ten years', async () => {
    api.listGadgetSources.mockResolvedValue({ items: [{ id: 's1', provider: 'world_bank' }], next_cursor: null });
    api.listWorldObservationWindow.mockResolvedValue({ truncated: false, items: [] });
    view({ id: 'g3', title: '', definition: { source_ids: ['s1'], scope: { metrics: [], lookback_days: null } } } as never);
    await waitFor(() => expect(api.listWorldObservationWindow).toHaveBeenCalled());
    const { from, to } = api.listWorldObservationWindow.mock.calls[0][0];
    expect(Math.round((to.getTime() - from.getTime()) / 86_400_000)).toBe(3651);
  });

  it('keeps a saved Metric gadget on a fast provider at 30 days instead of narrowing to the 3-day chart default', async () => {
    api.listGadgetSources.mockResolvedValue({ items: [{ id: 's1', provider: 'binance' }], next_cursor: null });
    api.listWorldObservationWindow.mockResolvedValue({ truncated: false, items: [] });
    view({ id: 'g4', title: '', definition: { source_ids: ['s1'], scope: { metrics: [], lookback_days: null } } } as never);
    await waitFor(() => expect(api.listWorldObservationWindow).toHaveBeenCalled());
    const { from, to } = api.listWorldObservationWindow.mock.calls[0][0];
    expect(Math.round((to.getTime() - from.getTime()) / 86_400_000)).toBe(31);
  });

  it('reads revision history over at most two pages so each series has points for delta and sparkline', async () => {
    api.listWorldObservationWindow.mockResolvedValue({ truncated: false, items: [] });
    view();
    await waitFor(() => expect(api.listWorldObservationWindow).toHaveBeenCalled());
    expect(api.listWorldObservationWindow.mock.calls[0][0].maxPages).toBe(2);
    expect(api.listWorldObservationWindow.mock.calls[0][0].history).toBe(true);
  });

  it('pages the source list to find the bound provider beyond the first page', async () => {
    api.listGadgetSources.mockImplementation(async (_limit: number, cursor?: string) => cursor
      ? { items: [{ id: 's1', provider: 'world_bank' }], next_cursor: null }
      : { items: [{ id: 'other', provider: 'noaa_kp' }], next_cursor: 'c2' });
    api.listWorldObservationWindow.mockResolvedValue({ truncated: false, items: [] });
    view({ id: 'g4', title: '', definition: { source_ids: ['s1'], scope: { metrics: [] } } } as never);
    await waitFor(() => expect(api.listWorldObservationWindow).toHaveBeenCalled());
    const { from, to } = api.listWorldObservationWindow.mock.calls[0][0];
    expect(Math.round((to.getTime() - from.getTime()) / 86_400_000)).toBe(3651);
  });

  it('shows Fear & Greed with Alternative.me attribution in the frame footer and times in the value tooltip', async () => {
    api.listWorldObservationWindow.mockResolvedValue({ truncated: false, items: [{ ...base, id: 'o1', provider: 'alternative_me', metric: 'fear_greed_index', value: 42, unit: 'index', observed_at: '2026-10-09T00:00:00Z', published_at: '2026-10-09T00:05:00Z' }] });
    view();
    const row = await screen.findByTestId('observation-row');
    expect(row.title).toMatch(/Observed/);
    expect(row.title).toMatch(/Published/);
    expect(row.title).toMatch(/Collected/);
    expect(freshness.report).toHaveBeenLastCalledWith(expect.any(String), 'Data: Alternative.me');
  });

  it('single series with history renders figure, delta vs previous observation and a sparkline', async () => {
    const at = (day: number, value: number, id: string) => ({ ...base, id, provider: 'binance', metric: 'btc_price', value, unit: 'USD', observed_at: `2026-10-0${day}T00:00:00Z` });
    api.listWorldObservationWindow.mockResolvedValue({ truncated: false, items: [at(3, 110, 'c'), at(2, 100, 'b'), at(1, 90, 'a')] });
    view();
    const pill = await screen.findByTestId('delta-pill');
    expect(pill.textContent).toContain('+10.0%');
    expect(screen.getByTestId('sparkline')).toBeTruthy();
  });

  it('multiple series render one SeriesRow each with its own delta', async () => {
    const row = (id: string, symbol: string, day: number, value: number) => ({ ...base, id, provider: 'binance', metric: 'price', symbol, value, unit: 'USD', observed_at: `2026-10-0${day}T00:00:00Z` });
    api.listWorldObservationWindow.mockResolvedValue({ truncated: false, items: [row('a2', 'AAA', 2, 50), row('a1', 'AAA', 1, 100), row('b2', 'BBB', 2, 10), row('b1', 'BBB', 1, 10)] });
    view();
    await screen.findAllByTestId('observation-row');
    const pills = screen.getAllByTestId('delta-pill').map((p) => p.textContent);
    expect(pills.some((text) => text?.includes('50.0%'))).toBe(true);
    expect(pills.some((text) => text?.includes('0.0%') && !text.includes('50'))).toBe(true);
  });

  it('keeps precision for small values and compacts large ones in the KPI figure', async () => {
    api.listWorldObservationWindow.mockResolvedValue({ truncated: false, items: [{ ...base, id: 'o1', provider: 'binance', metric: 'price', value: 0.0067, unit: 'u', observed_at: '2026-10-09T00:00:00Z' }] });
    const first = view();
    expect((await screen.findByTestId('observation-row')).textContent).toContain('0.0067');
    first.unmount();
    api.listWorldObservationWindow.mockResolvedValue({ truncated: false, items: [{ ...base, id: 'o2', provider: 'world_bank', metric: 'gdp_current_usd', value: 2.5e13, unit: 'u', observed_at: '2026-10-09T00:00:00Z' }] });
    view({ id: 'g9', title: '', definition: { source_ids: ['s1'], scope: { metrics: [] } } } as never);
    expect((await screen.findByTestId('observation-row')).textContent).toMatch(/25T/);
  });

  it('formats multi-series rows with the same small-value precision', async () => {
    const row = (id: string, symbol: string, value: number) => ({ ...base, id, provider: 'binance', metric: 'price', symbol, value, unit: 'u', observed_at: '2026-10-09T00:00:00Z' });
    api.listWorldObservationWindow.mockResolvedValue({ truncated: false, items: [row('a', 'AAA', 0.0067), row('b', 'BBB', 2.5e13)] });
    view();
    const rows = await screen.findAllByTestId('observation-row');
    expect(rows[0].textContent).toContain('0.0067');
    expect(rows[1].textContent).toMatch(/25T/);
  });

  it('shows FX reference date and GDP period', async () => {
    api.listWorldObservationWindow.mockResolvedValue({ truncated: false, items: [
      { ...base, id: 'o1', provider: 'frankfurter', metric: 'fx_reference_rate', symbol: 'USD/VND', value: 25000, observed_at: '2026-10-08T00:00:00Z', reference_date: '2026-10-08' },
      { ...base, id: 'o2', provider: 'world_bank', metric: 'gdp_current_usd', value: 4e11, observed_at: '2024-01-01T00:00:00Z', period: '2024' },
    ] });
    view();
    expect(await screen.findByText('Reference date Oct 8, 2026')).toBeTruthy();
    expect(screen.getByText('Period 2024')).toBeTruthy();
  });

  it('flags stale data after two polling intervals', async () => {
    api.listWorldObservationWindow.mockResolvedValue({ truncated: false, items: [{ ...base, id: 'o1', provider: 'binance', metric: 'btc_price', value: 1, collected_at: new Date(Date.now() - 3 * 3600_000).toISOString(), observed_at: '2026-10-09T00:00:00Z' }] });
    view();
    await waitFor(() => expect(screen.getByText(/Stale/)).toBeTruthy());
  });

  it('keeps the maximum 3660-day lookback within the API window limit', async () => {
    api.listWorldObservationWindow.mockResolvedValue({ truncated: false, items: [] });
    view({ id: 'g2', title: '', definition: { source_ids: ['s1'], scope: { metrics: [], lookback_days: 3660 } } } as never);
    await waitFor(() => expect(api.listWorldObservationWindow).toHaveBeenCalled());
    const { from, to } = api.listWorldObservationWindow.mock.calls[0][0] as { from: Date; to: Date };
    expect(to.getTime() - from.getTime()).toBe(3660 * 86_400_000);
  });

  it('shows an error when no value was ever loaded', async () => {
    api.listWorldObservationWindow.mockRejectedValue(new Error('boom'));
    view();
    expect(await screen.findByRole('alert')).toBeTruthy();
  });
});

describe('latestPerSeries', () => {
  it('keeps the newest observation per series', () => {
    const rows = [
      { ...base, id: 'a', provider: 'usgs', metric: 'm', observed_at: '2026-01-01T00:00:00Z' },
      { ...base, id: 'b', provider: 'usgs', metric: 'm', observed_at: '2026-01-02T00:00:00Z' },
    ] as never[];
    expect(latestPerSeries(rows).map((r: { id: string }) => r.id)).toEqual(['b']);
  });

  it('keeps the exact provider value in the accessible label', async () => {
    api.listWorldObservationWindow.mockResolvedValue({ truncated: false, items: [{ ...base, id: 'o1', provider: 'frankfurter', metric: 'fx_reference_rate', symbol: 'USD/VND', value: 25831.15, unit: 'VND per USD', observed_at: '2026-10-08T00:00:00Z' }] });
    view();
    expect(await screen.findByText('USD/VND')).toBeTruthy();
    // The visible text is locale-formatted (compact); the exact provider value stays in the accessible label.
    expect(screen.getByLabelText('25831.15 VND per USD')).toBeTruthy();
  });

  it('merges rows of several sources and prefixes labels with the source name', async () => {
    api.listGadgetSources.mockResolvedValue({ items: [{ id: 's1', provider: 'noaa_kp', name: 'Kp A' }, { id: 's2', provider: 'noaa_kp', name: 'Kp B' }], next_cursor: null });
    api.listWorldObservationWindow.mockImplementation(async (query: { sourceIds: string[] }) => ({ truncated: false, items: [
      { ...base, id: `r-${query.sourceIds[0]}`, source_id: query.sourceIds[0], provider: 'noaa_kp', metric: 'kp_index', value: 3, observed_at: '2026-10-09T00:00:00Z' },
    ] }));
    view({ id: 'g5', title: '', definition: { source_ids: ['s1', 's2'], scope: { metrics: [] } } } as never);
    expect(await screen.findByText(/^Kp A · /)).toBeTruthy();
    expect(screen.getByText(/^Kp B · /)).toBeTruthy();
    expect(screen.getAllByTestId('observation-row')).toHaveLength(2);
  });
});
