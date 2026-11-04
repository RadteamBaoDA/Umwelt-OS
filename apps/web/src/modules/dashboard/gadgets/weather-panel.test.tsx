import { render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { NextIntlClientProvider } from 'next-intl';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { dashboardMessages } from '@/core/messages/dashboard';
import { WeatherPanel, nearestReading } from './weather-panel';

const api = vi.hoisted(() => ({ listWorldObservationWindow: vi.fn() }));
vi.mock('@/modules/observations/api', () => ({ listWorldObservationWindow: api.listWorldObservationWindow }));
vi.mock('./use-effective-lookback', () => ({ useSourceInfo: () => ({ data: { s1: { provider: 'open_meteo', name: 'Hanoi' }, s2: { provider: 'open_meteo', name: 'Hue' } } }) }));
vi.mock('@/core/query-provider', () => ({ useDisplayPreferences: () => ({ locale: 'en-us', timezone: 'UTC' }) }));
vi.mock('recharts', async (original) => ({ ...(await original<typeof import('recharts')>()), ResponsiveContainer: ({ children }: { children: React.ReactNode }) => <div>{children}</div> }));

const hour = 3_600_000;
/** Builds one hourly Open-Meteo reading `offsetHours` from now. */
const reading = (id: string, metric: string, offsetHours: number, value: number, unit: string) => ({
  id, source_id: 's1', provider: 'open_meteo', metric, value, unit, quality: 'provider_reported', timezone: 'UTC',
  observed_at: new Date(Date.now() + offsetHours * hour).toISOString(), collected_at: new Date().toISOString(),
});

describe('WeatherPanel', () => {
  beforeEach(() => vi.clearAllMocks());

  it('queries from 24 hours back so the timeline shows observed past', async () => {
    api.listWorldObservationWindow.mockResolvedValue({ truncated: false, items: [] });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const instance = { id: 'w1', title: '', definition: { source_ids: ['s1'], scope: { metrics: [] } } } as never;
    render(<QueryClientProvider client={client}><NextIntlClientProvider locale="en-US" messages={{ dashboard: dashboardMessages['en-us'] }}><WeatherPanel instance={instance} /></NextIntlClientProvider></QueryClientProvider>);
    await screen.findByText(dashboardMessages['en-us'].observationEmpty);
    const { from, to } = api.listWorldObservationWindow.mock.calls[0][0] as { from: Date; to: Date };
    expect(Math.round((to.getTime() - from.getTime()) / hour)).toBe(96);
  });

  it('hero shows the temperature reading nearest to now with its unit', async () => {
    api.listWorldObservationWindow.mockResolvedValue({ truncated: false, items: [
      reading('a', 'temperature_2m', -20, 11, '°C'),
      reading('b', 'temperature_2m', 0.2, 27.4, '°C'),
      reading('c', 'temperature_2m', 40, 35, '°C'),
      reading('d', 'wind_speed_10m', 0, 3.5, 'm/s'),
    ] });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const instance = { id: 'w2', title: '', definition: { source_ids: ['s1'], scope: { metrics: [] } } } as never;
    render(<QueryClientProvider client={client}><NextIntlClientProvider locale="en-US" messages={{ dashboard: dashboardMessages['en-us'] }}><WeatherPanel instance={instance} /></NextIntlClientProvider></QueryClientProvider>);
    const hero = await screen.findByTestId('weather-hero');
    expect(hero.textContent).toContain('27.4');
    expect(hero.textContent).toContain('°C');
    expect(screen.getByText(/3\.5 m\/s/)).toBeTruthy();
    expect(screen.getByRole('link', { name: dashboardMessages['en-us'].openMeteoAttribution })).toBeTruthy();
  });

  it('shows the compact low/high strip for today', async () => {
    api.listWorldObservationWindow.mockResolvedValue({ truncated: false, items: [
      reading('a', 'temperature_2m', 0, 23, '°C'),
      reading('b', 'temperature_2m', 0.1, 31, '°C'),
    ] });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const instance = { id: 'w3', title: '', definition: { source_ids: ['s1'], scope: { metrics: [] } } } as never;
    render(<QueryClientProvider client={client}><NextIntlClientProvider locale="en-US" messages={{ dashboard: dashboardMessages['en-us'] }}><WeatherPanel instance={instance} /></NextIntlClientProvider></QueryClientProvider>);
    const strip = await screen.findByTestId('weather-minmax');
    expect(strip.textContent).toBe('Low 23°C · High 31°C');
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
    api.listWorldObservationWindow.mockImplementation(async (query: { sourceIds: string[] }) => ({ truncated: false, items: [
      { ...reading(`t-${query.sourceIds[0]}`, 'temperature_2m', 0, query.sourceIds[0] === 's1' ? 20 : 30, '°C'), source_id: query.sourceIds[0] },
    ] }));
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const instance = { id: 'w2', title: '', definition: { source_ids: ['s1', 's2'], scope: { metrics: [] } } } as never;
    render(<QueryClientProvider client={client}><NextIntlClientProvider locale="en-US" messages={{ dashboard: dashboardMessages['en-us'] }}><WeatherPanel instance={instance} /></NextIntlClientProvider></QueryClientProvider>);
    await waitFor(() => expect(screen.getAllByTestId('weather-city-temp').map((item) => item.textContent)).toEqual(['20°C', '30°C']));
    expect(api.listWorldObservationWindow).toHaveBeenCalledTimes(2);
  });
});
