import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { NextIntlClientProvider } from 'next-intl';
import { sourcesMessages } from '@/core/messages/sources';
import type { ConnectorConfig } from './api';

vi.mock('@/core/query-provider', () => ({ useDisplayPreferences: () => ({ locale: 'en-us', timezone: 'UTC' }) }));

import { ProviderScope } from './provider-scope';

const base: ConnectorConfig = { timeout_seconds: 30, timezone: 'UTC', schedule_interval_minutes: 30, history_mode: 'returned_snapshot' };

/** Installs a fake Geolocation API whose single request resolves with the given outcome. */
function fakeGeolocation(outcome: { latitude: number; longitude: number } | { code: number }) {
  const getCurrentPosition = vi.fn((ok: PositionCallback, fail: PositionErrorCallback) => {
    if ('code' in outcome) fail({ code: outcome.code, PERMISSION_DENIED: 1, POSITION_UNAVAILABLE: 2, TIMEOUT: 3, message: '' } as GeolocationPositionError);
    else ok({ coords: outcome } as GeolocationPosition);
  });
  Object.defineProperty(navigator, 'geolocation', { value: { getCurrentPosition }, configurable: true });
}

const view = (provider: 'open_meteo' | 'openweathermap', onChange = vi.fn()) => {
  render(<NextIntlClientProvider locale="en-US" messages={{ sources: sourcesMessages['en-us'] }}>
    <ProviderScope provider={provider} configuration={base} disabled={false} resetEpoch={0} onChange={onChange} />
  </NextIntlClientProvider>);
  return onChange;
};

describe('Use my location', () => {
  afterEach(cleanup);

  it('fills rounded Open-Meteo coordinates and the browser timezone', () => {
    fakeGeolocation({ latitude: 10.823099123, longitude: 106.629664777 });
    const onChange = view('open_meteo');
    fireEvent.click(screen.getByRole('button', { name: 'Use my location' }));
    expect(onChange).toHaveBeenCalledWith('weather_latitude', 10.8231);
    expect(onChange).toHaveBeenCalledWith('weather_longitude', 106.6297);
    expect(onChange).toHaveBeenCalledWith('weather_timezone', Intl.DateTimeFormat().resolvedOptions().timeZone);
  });

  it('fills OpenWeatherMap coordinates', () => {
    fakeGeolocation({ latitude: 21.02851, longitude: 105.85421 });
    const onChange = view('openweathermap');
    fireEvent.click(screen.getByRole('button', { name: 'Use my location' }));
    expect(onChange).toHaveBeenCalledWith('owm_latitude', 21.0285);
    expect(onChange).toHaveBeenCalledWith('owm_longitude', 105.8542);
  });

  it('keeps typed values and explains a denied permission', () => {
    fakeGeolocation({ code: 1 });
    const onChange = view('openweathermap');
    fireEvent.click(screen.getByRole('button', { name: 'Use my location' }));
    expect(onChange).not.toHaveBeenCalled();
    expect(screen.getByRole('status').textContent).toMatch(/denied/);
  });
});
