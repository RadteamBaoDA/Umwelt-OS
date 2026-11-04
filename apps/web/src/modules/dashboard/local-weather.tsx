'use client';

import { useQuery } from '@tanstack/react-query';
import { useTranslations } from 'next-intl';
import { useCallback, useEffect, useState, type ReactNode } from 'react';
import { normalizeFormattingLocale } from '@/core/i18n';
import { useDisplayPreferences } from '@/core/query-provider';
import { getLocalWeather } from './api';

type Coords = { lat: number; lon: number };
type Phase = 'checking' | 'ask' | 'granted' | 'hidden';

/** Rounds to 2 decimals (~1 km) so only coarse coordinates ever enter memory, query keys or requests. */
const coarse = (value: number) => Math.round(value * 100) / 100;

/**
 * Local weather fragment for the Today header, driven by the browser's geolocation.
 * Never prompts on load: it loads silently only when permission is already granted, otherwise offers a
 * "Show local weather" button whose click triggers the browser prompt. Denial or errors hide the button and
 * render `fallback`. Coordinates live only in component memory (no storage) and travel only to the backend proxy.
 * @param fallback Rendered whenever there is no local reading (loading, denied, error), e.g. the source-based line.
 */
export function LocalWeather({ fallback = null }: { fallback?: ReactNode }) {
  const t = useTranslations('dashboard');
  const { locale } = useDisplayPreferences();
  const [phase, setPhase] = useState<Phase>('checking');
  const [coords, setCoords] = useState<Coords | null>(null);

  /** Reads one low-accuracy position (30 min cache, 10 s timeout); any failure hides the control. */
  const locate = useCallback(() => {
    navigator.geolocation.getCurrentPosition(
      (pos) => { setCoords({ lat: coarse(pos.coords.latitude), lon: coarse(pos.coords.longitude) }); setPhase('granted'); },
      () => setPhase('hidden'),
      { enableHighAccuracy: false, maximumAge: 30 * 60_000, timeout: 10_000 },
    );
  }, []);

  useEffect(() => {
    // All state updates happen in promise callbacks (never synchronously in the effect body).
    const available = typeof navigator !== 'undefined' && Boolean(navigator.geolocation);
    const status: Promise<{ state: PermissionState } | null> = available && navigator.permissions?.query
      ? navigator.permissions.query({ name: 'geolocation' }).catch(() => null)
      : Promise.resolve(null);
    let live = true;
    void status.then((result) => {
      if (!live) return;
      if (!available) setPhase('hidden');
      else if (result === null || result.state === 'prompt') setPhase('ask'); // no Permissions API: the click still prompts
      else if (result.state === 'granted') { setPhase('granted'); locate(); } else setPhase('hidden');
    });
    return () => { live = false; };
  }, [locate]);

  const apiLocale = locale.toLowerCase().startsWith('vi') ? 'vi' : 'en';
  const weather = useQuery({
    queryKey: ['local-weather', coords?.lat, coords?.lon, apiLocale],
    enabled: coords !== null,
    staleTime: 10 * 60_000,
    retry: false,
    queryFn: ({ signal }) => getLocalWeather(coords!.lat, coords!.lon, apiLocale, signal),
  });

  if (weather.data) {
    const degrees = new Intl.NumberFormat(normalizeFormattingLocale(locale), { maximumFractionDigits: 0 }).format(weather.data.temperature_c);
    return <span className="font-normal text-muted-foreground tabular-nums"> · {weather.data.place ? `${weather.data.place} ` : ''}{degrees}°</span>;
  }
  return (
    <>
      {fallback}
      {phase === 'ask' && (
        <>
          {' · '}
          <button type="button" onClick={locate} title={t('localWeatherPrivacy')} aria-describedby="local-weather-privacy"
            className="cursor-pointer border-0 bg-transparent p-0 text-sm font-normal text-muted-foreground underline underline-offset-2 hover:text-foreground">
            {t('showLocalWeather')}
          </button>
          <span id="local-weather-privacy" className="sr-only">{t('localWeatherPrivacy')}</span>
        </>
      )}
    </>
  );
}
