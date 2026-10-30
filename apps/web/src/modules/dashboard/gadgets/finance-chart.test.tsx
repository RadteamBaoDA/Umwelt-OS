import { render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { NextIntlClientProvider } from 'next-intl';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { dashboardMessages } from '@/core/messages/dashboard';
import metricCatalog from '@/test/fixtures/metric-catalog.json';
import { FinanceChart } from './finance-chart';

const api = vi.hoisted(() => ({ listWorldObservationWindow: vi.fn(), getMetricCatalog: vi.fn(), listGadgetSources: vi.fn() }));
vi.mock('../api', async (original) => ({ ...(await original<typeof import('../api')>()), getMetricCatalog: api.getMetricCatalog, listGadgetSources: api.listGadgetSources }));
vi.mock('@/modules/observations/api', () => ({ listWorldObservationWindow: api.listWorldObservationWindow }));
vi.mock('./metrics-chart', () => ({ MetricsChart: () => <div data-testid="chart" /> }));
vi.mock('@/core/query-provider', () => ({ useDisplayPreferences: () => ({ locale: 'en-us', timezone: 'UTC' }) }));

const row = { source_id: 's1', metric: 'kp_index', symbol: null, value: 3, unit: 'index', currency: null, observed_at: '2026-10-09T00:00:00Z', collected_at: '2026-10-09T00:00:00Z', provider_delay_seconds: null, timezone: null, provider: 'noaa_kp' };

/** Renders FinanceChart for a bound source with the given scope; the metric catalog and observation window are mocked in `beforeEach`. */
function view(scope: Record<string, unknown>) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const instance = { id: 'g1', title: '', definition: { source_ids: ['s1'], scope } } as never;
  return render(<QueryClientProvider client={client}><NextIntlClientProvider locale="en-US" messages={{ dashboard: dashboardMessages['en-us'] }}><FinanceChart instance={instance} /></NextIntlClientProvider></QueryClientProvider>);
}

describe('FinanceChart', () => {
  beforeEach(() => {
  vi.clearAllMocks();
  api.getMetricCatalog.mockResolvedValue(metricCatalog);
  api.listGadgetSources.mockResolvedValue({ items: [{ id: 's1', provider: 'noaa_kp' }], next_cursor: null });
});

  it('reads observation revision history so a fixed-identity series charts more than one point', async () => {
    api.listWorldObservationWindow.mockResolvedValue({ truncated: false, items: [row] });
    view({ metrics: ['kp_index'] });
    await screen.findByTestId('chart');
    expect(api.listWorldObservationWindow.mock.calls[0][0].history).toBe(true);
  });

  it('renders a symbol-less series from a catalog-known provider', async () => {
    api.listWorldObservationWindow.mockResolvedValue({ truncated: false, items: [row] });
    view({ metrics: ['kp_index'] });
    expect(await screen.findByTestId('chart')).toBeTruthy();
    expect(screen.queryByText(dashboardMessages['en-us'].financeSourceSymbolRequired)).toBeNull();
    expect(api.listWorldObservationWindow.mock.calls[0][0].symbols).toBeUndefined();
  });

  it('shows symbol-required when a symbol-less scope meets mixed symbols', async () => {
    api.listWorldObservationWindow.mockResolvedValue({ truncated: false, items: [{ ...row, metric: 'close', symbol: 'AAPL' }, { ...row, metric: 'close', symbol: 'MSFT' }] });
    view({ metrics: ['close'] });
    expect(await screen.findByText(dashboardMessages['en-us'].financeSourceSymbolRequired)).toBeTruthy();
  });

  it('filters by the configured symbol', async () => {
    api.listWorldObservationWindow.mockResolvedValue({ truncated: false, items: [{ ...row, metric: 'close', symbol: 'AAPL' }, { ...row, metric: 'close', symbol: 'MSFT' }] });
    view({ metrics: ['close'], symbols: ['AAPL'] });
    expect(await screen.findByTestId('chart')).toBeTruthy();
    expect(api.listWorldObservationWindow.mock.calls[0][0].symbols).toEqual(['AAPL']);
  });

  it('treats distinct regions as distinct series instead of merging them into one line', async () => {
    api.listWorldObservationWindow.mockResolvedValue({ truncated: false, items: [{ ...row, provider: 'openaq', metric: 'gdp', region: 'VN' }, { ...row, provider: 'openaq', metric: 'gdp', region: 'US' }] });
    view({ metrics: ['gdp'] });
    expect(await screen.findByText(dashboardMessages['en-us'].financeSourceSymbolRequired)).toBeTruthy();
    expect(screen.queryByTestId('chart')).toBeNull();
  });

  it('renders when every row shares one region', async () => {
    api.listWorldObservationWindow.mockResolvedValue({ truncated: false, items: [{ ...row, metric: 'gdp', region: 'VN' }, { ...row, metric: 'gdp', region: 'VN', observed_at: '2026-10-10T00:00:00Z' }] });
    view({ metrics: ['gdp'] });
    expect(await screen.findByTestId('chart')).toBeTruthy();
  });
  it('plots one region of a region-keyed provider when the scope names it', async () => {
    const wb = { ...row, provider: 'openaq', metric: 'gdp' };
    api.listWorldObservationWindow.mockResolvedValue({ truncated: false, items: [{ ...wb, region: 'Viet Nam' }, { ...wb, region: 'Viet Nam', observed_at: '2026-10-10T00:00:00Z' }, { ...wb, region: 'Japan' }] });
    view({ metrics: ['gdp'], regions: ['Viet Nam'] });
    expect(await screen.findByTestId('chart')).toBeTruthy();
    expect(api.listWorldObservationWindow.mock.calls[0][0].regions).toEqual(['Viet Nam']);
  });

  it('asks to select a series for two regions of a region-keyed provider without a region, and draws the same data once a region is set', async () => {
    const wb = { ...row, provider: 'openaq', metric: 'gdp' };
    api.listWorldObservationWindow.mockResolvedValue({ truncated: false, items: [{ ...wb, region: 'Viet Nam' }, { ...wb, region: 'Japan' }] });
    const first = view({ metrics: ['gdp'] });
    expect(await screen.findByText(dashboardMessages['en-us'].financeSourceSymbolRequired)).toBeTruthy();
    expect(screen.queryByTestId('chart')).toBeNull();
    first.unmount();
    view({ metrics: ['gdp'], regions: ['Japan'] });
    expect(await screen.findByTestId('chart')).toBeTruthy();
    expect(screen.queryByText(dashboardMessages['en-us'].financeSourceSymbolRequired)).toBeNull();
  });

  it('asks to select a series for a point provider (charts_as_series=false) with distinct per-event regions', async () => {
    api.listWorldObservationWindow.mockResolvedValue({ truncated: false, items: [{ ...row, provider: 'usgs', metric: 'magnitude', region: '10 km N of A' }, { ...row, provider: 'usgs', metric: 'magnitude', region: '5 km S of B' }] });
    view({ metrics: ['magnitude'] });
    expect(await screen.findByText(dashboardMessages['en-us'].financeSourceSymbolRequired)).toBeTruthy();
    expect(screen.queryByTestId('chart')).toBeNull();
  });

  it('falls back to symbol and region keys when the catalog request fails', async () => {
    api.getMetricCatalog.mockRejectedValue(new Error('catalog down'));
    api.listWorldObservationWindow.mockResolvedValue({ truncated: false, items: [{ ...row, region: 'a' }, { ...row, region: 'b' }] });
    view({ metrics: ['kp_index'] });
    expect(await screen.findByText(dashboardMessages['en-us'].financeSourceSymbolRequired)).toBeTruthy();
  });

  it('shows the loading state while the catalog is pending', async () => {
    api.getMetricCatalog.mockReturnValue(new Promise(() => undefined));
    api.listWorldObservationWindow.mockResolvedValue({ truncated: false, items: [row] });
    view({ metrics: ['kp_index'] });
    expect(await screen.findByText(dashboardMessages['en-us'].observationLoading)).toBeTruthy();
    expect(screen.queryByTestId('chart')).toBeNull();
  });

  it('shows the mixed-units message for a single region-keyed series with different units', async () => {
    const wb = { ...row, provider: 'openaq', metric: 'gdp', region: 'Viet Nam' };
    api.listWorldObservationWindow.mockResolvedValue({ truncated: false, items: [{ ...wb, unit: 'USD' }, { ...wb, unit: 'VND', observed_at: '2026-10-10T00:00:00Z' }] });
    view({ metrics: ['gdp'], regions: ['Viet Nam'] });
    expect(await screen.findByText(dashboardMessages['en-us'].observationMixedUnits)).toBeTruthy();
  });

  it('does not split a non-region-keyed provider by region', async () => {
    api.listWorldObservationWindow.mockResolvedValue({ truncated: false, items: [{ ...row, region: 'a' }, { ...row, region: 'b' }] });
    view({ metrics: ['kp_index'] });
    expect(await screen.findByTestId('chart')).toBeTruthy();
  });

  it('shows the setup state, not an arbitrary series, for a symbol-keyed provider with no symbol even when only one symbol has rows', async () => {
    api.listGadgetSources.mockResolvedValue({ items: [{ id: 's1', provider: 'binance' }], next_cursor: null });
    api.listWorldObservationWindow.mockResolvedValue({ truncated: false, items: [{ ...row, provider: 'binance', metric: 'close', symbol: 'BTCUSDT' }] });
    view({ metrics: ['close'] });
    expect(await screen.findByText(dashboardMessages['en-us'].financeSourceSymbolRequired)).toBeTruthy();
    expect(screen.queryByTestId('chart')).toBeNull();
  });

  it('shows the setup message for a preset chart with an empty scope that has no close rows', async () => {
    api.listWorldObservationWindow.mockResolvedValue({ truncated: false, items: [] });
    view({});
    expect(await screen.findByText(dashboardMessages['en-us'].financeSourceSymbolRequired)).toBeTruthy();
    expect(screen.queryByTestId('chart')).toBeNull();
  });

  it('still plots close for a legacy Alpha Vantage chart with empty metrics and one symbol', async () => {
    api.listWorldObservationWindow.mockResolvedValue({ truncated: false, items: [{ ...row, provider: 'alpha_vantage', metric: 'close', symbol: 'NVDA' }] });
    view({ metrics: [], symbols: ['NVDA'] });
    expect(await screen.findByTestId('chart')).toBeTruthy();
    expect(api.listWorldObservationWindow.mock.calls[0][0].metrics).toEqual(['close']);
  });

  it('shows no-data, not the series chooser, for a legacy chart with a symbol and no metrics when nothing is returned', async () => {
    api.listWorldObservationWindow.mockResolvedValue({ truncated: false, items: [] });
    view({ metrics: [], symbols: ['NVDA'] });
    expect(await screen.findByText(dashboardMessages['en-us'].observationEmpty)).toBeTruthy();
    expect(screen.queryByText(dashboardMessages['en-us'].financeSourceSymbolRequired)).toBeNull();
  });

  it('queries a World Bank chart without a stored lookback from the catalog default of about ten years', async () => {
    api.listGadgetSources.mockResolvedValue({ items: [{ id: 's1', provider: 'world_bank' }], next_cursor: null });
    api.listWorldObservationWindow.mockResolvedValue({ truncated: false, items: [] });
    view({ metrics: ['gdp_current_usd'] });
    await waitFor(() => expect(api.listWorldObservationWindow).toHaveBeenCalled());
    const { from, to } = api.listWorldObservationWindow.mock.calls[0][0];
    expect(Math.round((to.getTime() - from.getTime()) / 86_400_000)).toBe(3651);
  });

  it('keeps an explicit stored lookback over the provider default', async () => {
    api.listGadgetSources.mockResolvedValue({ items: [{ id: 's1', provider: 'world_bank' }], next_cursor: null });
    api.listWorldObservationWindow.mockResolvedValue({ truncated: false, items: [] });
    view({ metrics: ['gdp_current_usd'], lookback_days: 30 });
    await waitFor(() => expect(api.listWorldObservationWindow).toHaveBeenCalled());
    const { from, to } = api.listWorldObservationWindow.mock.calls[0][0];
    expect(Math.round((to.getTime() - from.getTime()) / 86_400_000)).toBe(31);
  });

  it('starts a chart on a fast provider at its 3-day catalog window and tolerates provider clock skew with a day ahead', async () => {
    api.getMetricCatalog.mockResolvedValue({ binance: { default_lookback_days: 3, region_keyed: false, charts_as_series: true } });
    api.listGadgetSources.mockResolvedValue({ items: [{ id: 's1', provider: 'binance' }], next_cursor: null });
    api.listWorldObservationWindow.mockResolvedValue({ truncated: false, items: [] });
    view({ metrics: ['price'] });
    await waitFor(() => expect(api.listWorldObservationWindow).toHaveBeenCalled());
    const { from, to } = api.listWorldObservationWindow.mock.calls[0][0];
    expect(Math.round((to.getTime() - from.getTime()) / 86_400_000)).toBe(4);
    expect(Math.round((to.getTime() - Date.now()) / 86_400_000)).toBe(1);
  });
});
