import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen } from '@testing-library/react';
import { NextIntlClientProvider } from 'next-intl';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { messages } from '@/core/messages';

const mocks = vi.hoisted(() => ({ get: vi.fn() }));
vi.mock('./api', () => ({ getLocalWeather: mocks.get }));
vi.mock('@/core/query-provider', () => ({ useDisplayPreferences: () => ({ locale: 'en-us', timezone: 'UTC' }) }));

import { LocalWeather } from './local-weather';

/** Installs fake permission and geolocation APIs and returns the geolocation spy. */
function stubBrowser(state: PermissionState, position?: { latitude: number; longitude: number }) {
  const getCurrentPosition = vi.fn((ok: PositionCallback, fail: PositionErrorCallback) => {
    if (position) ok({ coords: position } as GeolocationPosition);
    else fail({} as GeolocationPositionError);
  });
  Object.defineProperty(navigator, 'geolocation', { value: { getCurrentPosition }, configurable: true });
  Object.defineProperty(navigator, 'permissions', { value: { query: vi.fn().mockResolvedValue({ state }) }, configurable: true });
  return getCurrentPosition;
}

/** Renders the component with English messages and a fresh query cache. */
function renderIt() {
  return render(
    <QueryClientProvider client={new QueryClient()}>
      <NextIntlClientProvider locale="en-US" messages={messages['en-us']}>
        <LocalWeather fallback={<span>FALLBACK</span>} />
      </NextIntlClientProvider>
    </QueryClientProvider>,
  );
}

describe('LocalWeather', () => {
  beforeEach(() => mocks.get.mockReset());
  afterEach(() => vi.restoreAllMocks());

  it('loads silently when granted and shows rounded place and temperature', async () => {
    const geo = stubBrowser('granted', { latitude: 15.87654, longitude: 108.33219 });
    mocks.get.mockResolvedValue({
      temperature_c: 24.4,
      weather_code: 3,
      place: 'Hoi An, Quang Nam',
      observed_at: '2026-11-04T06:00:00Z',
      attribution: [],
      humidity_pct: 55,
      wind_kmh: 6.2,
      precipitation_probability_pct: 5,
      is_day: true,
      hourly: [0, 1, 2, 3].map((h) => ({
        at: `2026-11-04T0${h * 3}:00:00Z`,
        temperature_c: 20 + h,
        precipitation_probability_pct: 10,
        wind_kmh: 5,
      })),
    });
    renderIt();
    // Inline summary first; the full card only after opening the info popover.
    expect(await screen.findByText(/Hoi An, Quang Nam/)).toBeTruthy();
    expect(screen.getByText('24°C')).toBeTruthy();
    expect(screen.getByText(/Cloudy/)).toBeTruthy();
    expect(screen.queryByText('Humidity: 55%')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Weather details' }));
    // WeatherCard loads through next/dynamic; under a full-suite run its import can exceed the 1 s default.
    expect(await screen.findByText('Humidity: 55%', undefined, { timeout: 5000 })).toBeTruthy();
    expect(screen.getByText('Chance of rain: 5%')).toBeTruthy();
    expect(screen.getByText('Wind: 6 km/h')).toBeTruthy();
    expect(screen.getByText(/Wednesday/)).toBeTruthy();
    expect(screen.getByRole('tab', { name: 'Rain chance' })).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: '°F' }));
    expect(await screen.findByText('76°F')).toBeTruthy();
    expect(localStorage.getItem('umwelt.weather.unit')).toBe('f');
    localStorage.removeItem('umwelt.weather.unit');
    expect(geo).toHaveBeenCalledOnce();
    expect(mocks.get.mock.calls[0].slice(0, 3)).toEqual([15.88, 108.33, 'en']);
    expect(screen.queryByText('FALLBACK')).toBeNull();
  });

  it('shows the button without prompting, then prompts on click', async () => {
    const geo = stubBrowser('prompt', { latitude: 1, longitude: 2 });
    mocks.get.mockResolvedValue({ temperature_c: 30, weather_code: 0, place: null, observed_at: 'x', attribution: [] });
    renderIt();
    const button = await screen.findByRole('button', { name: 'Show local weather' });
    expect(geo).not.toHaveBeenCalled();
    expect(button.getAttribute('title')).toMatch(/Open-Meteo/);
    fireEvent.click(button);
    expect(await screen.findByText('30°C')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Weather details' }));
    expect(await screen.findByText('Clear')).toBeTruthy();
    expect(screen.queryByText(/Humidity/)).toBeNull();
    expect(screen.queryByRole('tablist')).toBeNull();
  });

  it('renders only the fallback when denied or when the prompt is refused', async () => {
    stubBrowser('denied');
    const { unmount } = renderIt();
    expect(await screen.findByText('FALLBACK')).toBeTruthy();
    expect(screen.queryByRole('button')).toBeNull();
    unmount();
    stubBrowser('prompt');
    renderIt();
    fireEvent.click(await screen.findByRole('button', { name: 'Show local weather' }));
    await vi.waitFor(() => expect(screen.queryByRole('button')).toBeNull());
    expect(screen.getByText('FALLBACK')).toBeTruthy();
  });
});
