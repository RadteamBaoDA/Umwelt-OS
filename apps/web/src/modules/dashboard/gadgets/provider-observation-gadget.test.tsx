import { render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { NextIntlClientProvider } from 'next-intl';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { dashboardMessages } from '@/core/messages/dashboard';
import { ProviderObservationGadget, latestPerSeries } from './provider-observation-gadget';

const api = vi.hoisted(() => ({ listWorldObservationWindow: vi.fn(), getConnectorConfiguration: vi.fn() }));
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

function view() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}><NextIntlClientProvider locale="en-US" messages={{ dashboard: dashboardMessages['en-us'] }}><ProviderObservationGadget instance={instance} /></NextIntlClientProvider></QueryClientProvider>);
}

describe('ProviderObservationGadget', () => {
  beforeEach(() => { vi.clearAllMocks(); api.getConnectorConfiguration.mockResolvedValue({ configuration: { schedule_interval_minutes: 30 } }); });

  it('shows Fear & Greed with Alternative.me attribution and separate times', async () => {
    api.listWorldObservationWindow.mockResolvedValue({ truncated: false, items: [{ ...base, id: 'o1', provider: 'alternative_me', metric: 'fear_greed_index', value: 42, unit: 'index', observed_at: '2026-10-09T00:00:00Z', published_at: '2026-10-09T00:05:00Z' }] });
    view();
    expect(await screen.findByText(/Alternative\.me/)).toBeTruthy();
    expect(screen.getByText(/Observed/)).toBeTruthy();
    expect(screen.getByText(/Published/)).toBeTruthy();
    expect(screen.getByText(/Collected/)).toBeTruthy();
  });

  it('shows FX reference date and GDP period', async () => {
    api.listWorldObservationWindow.mockResolvedValue({ truncated: false, items: [
      { ...base, id: 'o1', provider: 'frankfurter', metric: 'fx_reference_rate', symbol: 'USD/VND', value: 25000, observed_at: '2026-10-08T00:00:00Z', reference_date: '2026-10-08' },
      { ...base, id: 'o2', provider: 'world_bank', metric: 'gdp_current_usd', value: 4e11, observed_at: '2024-01-01T00:00:00Z', period: '2024' },
    ] });
    view();
    expect(await screen.findByText('Reference date 2026-10-08')).toBeTruthy();
    expect(screen.getByText('Period 2024')).toBeTruthy();
  });

  it('flags stale data after two polling intervals', async () => {
    api.listWorldObservationWindow.mockResolvedValue({ truncated: false, items: [{ ...base, id: 'o1', provider: 'binance', metric: 'btc_price', value: 1, collected_at: new Date(Date.now() - 3 * 3600_000).toISOString(), observed_at: '2026-10-09T00:00:00Z' }] });
    view();
    await waitFor(() => expect(screen.getByText(/Stale/)).toBeTruthy());
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
});
