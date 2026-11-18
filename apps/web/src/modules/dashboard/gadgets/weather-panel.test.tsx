import { render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { NextIntlClientProvider } from 'next-intl';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { getCatalog } from '@locales';
import { WeatherPanel, nearestReading } from './weather-panel';

const api = vi.hoisted(() => ({ listWorldObservationWindow: vi.fn(), listAllSources: vi.fn() }));
vi.mock('@/modules/sources', async (original) => ({
  ...(await original<typeof import('@/modules/sources')>()),
  listAllSources: api.listAllSources,
}));
vi.mock('@/modules/observations/api', () => ({ listWorldObservationWindow: api.listWorldObservationWindow }));
vi.mock('./use-effective-lookback', () => ({
  useSourceInfo: () => ({ data: { s1: { provider: 'open_meteo', name: 'Hanoi' }, s2: { provider: 'open_meteo', name: 'Hue' } } }),
}));
vi.mock('@/core/query-provider', () => ({ useDisplayPreferences: () => ({ locale: 'en-us', timezone: 'UTC' }) }));
vi.mock('recharts', async (original) => ({
  ...(await original<typeof import('recharts')>()),
  ResponsiveContainer: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
}));

const hour = 3_600_000;

// Pin Date to midday UTC so 'today' low/high never straddles a day boundary when the suite runs near midnight.
beforeEach(() => {
  vi.useFakeTimers({ toFake: ['Date'] });
  vi.setSystemTime(new Date('2026-06-15T12:00:00Z'));
});
afterEach(() => vi.useRealTimers());

/** Builds one hourly Open-Meteo reading `offsetHours` from now. */
const reading = (id: string, metric: string, offsetHours: number, value: number, unit: string) => ({
  id,
  source_id: 's1',
  provider: 'open_meteo',
  metric,
  value,
  unit,
  quality: 'provider_reported',
  timezone: 'UTC',
  observed_at: new Date(Date.now() + offsetHours * hour).toISOString(),
  collected_at: new Date().toISOString(),
});

describe('WeatherPanel', () => {
  beforeEach(() => vi.clearAllMocks());

  it('queries from 24 hours back so the timeline shows observed past', async () => {
    api.listWorldObservationWindow.mockResolvedValue({ truncated: false, items: [] });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const instance = { id: 'w1', title: '', definition: { source_ids: ['s1'], scope: { metrics: [] } } } as never;
    render(
      <QueryClientProvider client={client}>
        <NextIntlClientProvider locale="en-US" messages={{ dashboard: getCatalog('en-us', 'dashboard') }}>
          <WeatherPanel instance={instance} />
        </NextIntlClientProvider>
      </QueryClientProvider>,
    );
    await screen.findByText(getCatalog('en-us', 'dashboard').observation.empty);
    const { from, to } = api.listWorldObservationWindow.mock.calls[0][0] as { from: Date; to: Date };
    expect(Math.round((to.getTime() - from.getTime()) / hour)).toBe(96);
  });

  it('on a failed read shows last good data age, next retry and no reading when nothing was cached', async () => {
    api.listWorldObservationWindow.mockRejectedValue(new Error('timeout'));
    api.listAllSources.mockResolvedValue({
      items: [{ id: 's1', last_success_at: new Date(Date.now() - 2 * hour).toISOString(), next_due_at: '2026-06-15T13:20:00Z' }],
      next_cursor: null,
    });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const instance = { id: 'w9', title: '', definition: { source_ids: ['s1'], scope: { metrics: [] } } } as never;
    render(
      <QueryClientProvider client={client}>
        <NextIntlClientProvider locale="en-US" messages={{ dashboard: getCatalog('en-us', 'dashboard') }}>
          <WeatherPanel instance={instance} />
        </NextIntlClientProvider>
      </QueryClientProvider>,
    );
    const line = await screen.findByTestId('weather-stale-line');
    expect(line.textContent).toBe('last good data 2h ago · next retry 13:20:00');
    expect(screen.queryByTestId('weather-stale-reading')).toBeNull();
  });

  it('on an empty read from a failing source shows the stale line; a healthy source shows none', async () => {
    api.listWorldObservationWindow.mockResolvedValue({ truncated: false, items: [] });
    const lastSuccess = new Date(Date.now() - 2 * hour).toISOString();
    api.listAllSources.mockResolvedValue({
      items: [
        { id: 's1', last_success_at: lastSuccess, last_error_at: new Date().toISOString(), next_due_at: '2026-06-15T13:20:00Z' },
        { id: 's2', last_success_at: lastSuccess, last_error_at: null, next_due_at: '2026-06-15T13:20:00Z' },
      ],
      next_cursor: null,
    });
    const renderFor = (id: string) => {
      const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
      const instance = { id: `w-${id}`, title: '', definition: { source_ids: [id], scope: { metrics: [] } } } as never;
      return render(
        <QueryClientProvider client={client}>
          <NextIntlClientProvider locale="en-US" messages={{ dashboard: getCatalog('en-us', 'dashboard') }}>
            <WeatherPanel instance={instance} />
          </NextIntlClientProvider>
        </QueryClientProvider>,
      );
    };
    const failing = renderFor('s1');
    expect((await screen.findByTestId('weather-stale-line')).textContent).toBe('last good data 2h ago · next retry 13:20:00');
    failing.unmount();
    renderFor('s2');
    await screen.findByText(getCatalog('en-us', 'dashboard').observation.empty);
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(screen.queryByTestId('weather-stale-line')).toBeNull();
  });

  it('keeps the last good temperature as a muted stale reading when a refresh fails', async () => {
    api.listWorldObservationWindow.mockResolvedValueOnce({
      truncated: false,
      items: [reading('a', 'temperature_2m', 0.2, 16.2, '°C')],
    });
    api.listAllSources.mockResolvedValue({ items: [], next_cursor: null });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const instance = { id: 'w8', title: '', definition: { source_ids: ['s1'], scope: { metrics: [] } } } as never;
    render(
      <QueryClientProvider client={client}>
        <NextIntlClientProvider locale="en-US" messages={{ dashboard: getCatalog('en-us', 'dashboard') }}>
          <WeatherPanel instance={instance} />
        </NextIntlClientProvider>
      </QueryClientProvider>,
    );
    await screen.findByTestId('weather-hero');
    api.listWorldObservationWindow.mockRejectedValue(new Error('timeout'));
    await client.refetchQueries({ queryKey: ['world-observations'] });
    const reading16 = await screen.findByTestId('weather-stale-reading');
    expect(reading16.textContent).toBe('16.2°C · stale');
    expect(screen.queryByTestId('weather-stale-line')).toBeNull();
  });

  it('hero shows the temperature reading nearest to now with its unit', async () => {
    api.listWorldObservationWindow.mockResolvedValue({
      truncated: false,
      items: [
        reading('a', 'temperature_2m', -20, 11, '°C'),
        reading('b', 'temperature_2m', 0.2, 27.4, '°C'),
        reading('c', 'temperature_2m', 40, 35, '°C'),
        reading('d', 'wind_speed_10m', 0, 3.5, 'm/s'),
      ],
    });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const instance = { id: 'w2', title: '', definition: { source_ids: ['s1'], scope: { metrics: [] } } } as never;
    render(
      <QueryClientProvider client={client}>
        <NextIntlClientProvider locale="en-US" messages={{ dashboard: getCatalog('en-us', 'dashboard') }}>
          <WeatherPanel instance={instance} />
        </NextIntlClientProvider>
      </QueryClientProvider>,
    );
    const hero = await screen.findByTestId('weather-hero');
    expect(hero.textContent).toContain('27.4');
    expect(hero.textContent).toContain('°C');
    expect(screen.getByText(/3\.5 m\/s/)).toBeTruthy();
    expect(screen.getByRole('link', { name: getCatalog('en-us', 'dashboard').openMeteoAttribution })).toBeTruthy();
  });

  it('shows the compact low/high strip for today', async () => {
    api.listWorldObservationWindow.mockResolvedValue({
      truncated: false,
      items: [reading('a', 'temperature_2m', 0, 23, '°C'), reading('b', 'temperature_2m', 0.1, 31, '°C')],
    });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const instance = { id: 'w3', title: '', definition: { source_ids: ['s1'], scope: { metrics: [] } } } as never;
    render(
      <QueryClientProvider client={client}>
        <NextIntlClientProvider locale="en-US" messages={{ dashboard: getCatalog('en-us', 'dashboard') }}>
          <WeatherPanel instance={instance} />
        </NextIntlClientProvider>
      </QueryClientProvider>,
    );
    const strip = await screen.findByTestId('weather-minmax');
    expect(strip.textContent).toBe('Low 23°C · High 31°C');
  });
});

describe('WeatherPanel terminal skin', () => {
  afterEach(() => document.documentElement.removeAttribute('data-skin'));

  it('renders the figure, one summary line and the same readings as the default panel', async () => {
    document.documentElement.setAttribute('data-skin', 'terminal');
    api.listWorldObservationWindow.mockResolvedValue({
      truncated: false,
      items: [
        reading('a', 'temperature_2m', 0, 18.5, '°C'),
        reading('b', 'temperature_2m', 1, 22, '°C'),
        reading('c', 'temperature_2m', -1, 17, '°C'),
        reading('d', 'wind_speed_10m', 0, 9, 'km/h'),
        reading('e', 'precipitation', 0, 20, '%'),
      ],
    });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const instance = { id: 'w9', title: '', definition: { source_ids: ['s1'], scope: { metrics: [] } } } as never;
    render(
      <QueryClientProvider client={client}>
        <NextIntlClientProvider locale="en-US" messages={{ dashboard: getCatalog('en-us', 'dashboard') }}>
          <WeatherPanel instance={instance} />
        </NextIntlClientProvider>
      </QueryClientProvider>,
    );
    expect((await screen.findByTestId('weather-hero')).textContent).toBe('18.5°C');
    expect(screen.getByTestId('weather-minmax').textContent).toBe('low 17° · high 22° · rain 20% · wind 9 km/h');
    expect(screen.getByRole('link', { name: getCatalog('en-us', 'dashboard').openMeteoAttribution })).toBeTruthy();
  });
});

describe('nearestReading', () => {
  it('picks the smallest time distance', () => {
    expect(nearestReading([{ ts: 0 }, { ts: 90 }, { ts: 200 }], 100)).toEqual({ ts: 90 });
    expect(nearestReading([], 100)).toBeUndefined();
  });
});

describe('WeatherPanel with several sources', () => {
  it('renders one compact row per city and queries each source on its own', async () => {
    api.listWorldObservationWindow.mockImplementation(async (query: { sourceIds: string[] }) => ({
      truncated: false,
      items: [
        {
          ...reading(`t-${query.sourceIds[0]}`, 'temperature_2m', 0, query.sourceIds[0] === 's1' ? 20 : 30, '°C'),
          source_id: query.sourceIds[0],
        },
      ],
    }));
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const instance = { id: 'w2', title: '', definition: { source_ids: ['s1', 's2'], scope: { metrics: [] } } } as never;
    render(
      <QueryClientProvider client={client}>
        <NextIntlClientProvider locale="en-US" messages={{ dashboard: getCatalog('en-us', 'dashboard') }}>
          <WeatherPanel instance={instance} />
        </NextIntlClientProvider>
      </QueryClientProvider>,
    );
    await waitFor(() => expect(screen.getAllByTestId('weather-city-temp').map((item) => item.textContent)).toEqual(['20°C', '30°C']));
    expect(api.listWorldObservationWindow).toHaveBeenCalledTimes(2);
  });
});
