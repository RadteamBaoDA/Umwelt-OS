'use client';

import { useQuery } from '@tanstack/react-query';
import { useTranslations } from 'next-intl';
import { Info, MapPin } from 'lucide-react';
import { useCallback, useEffect, useRef, useState, type PointerEvent as ReactPointerEvent, type ReactNode } from 'react';
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover';
import { normalizeFormattingLocale } from '@/core/i18n';
import { useDisplayPreferences } from '@/core/query-provider';
import { getLocalWeather, type LocalWeather as LocalWeatherReading } from './api';
import { toUnit, useWeatherUnit, weatherCondition, WeatherCard } from './weather-card';

type Coords = { lat: number; lon: number };
type Phase = 'checking' | 'ask' | 'granted' | 'hidden';
/** Rounds to 2 decimals (~1 km) so only coarse coordinates ever enter memory, query keys or requests. */
const coarse = (value: number) => Math.round(value * 100) / 100;

/**
 * Local weather for the Today header, driven by the browser's geolocation. The loaded reading renders inline as
 * " · place temperature condition" plus an info button; hovering (mouse) or clicking/Enter (touch, keyboard) the button
 * opens the full `WeatherCard` in a popover. Renders inline content: place it inside the header text line.
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

  if (weather.data) return <WeatherSummary data={weather.data} />;
  return (
    <span className="font-normal text-muted-foreground">
      {fallback && <> · {fallback}</>}
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
    </span>
  );
}

/** Delay before a hover-opened popover closes, so the pointer can travel from the button into the card. */
const HOVER_CLOSE_MS = 150;

/**
 * Inline weather summary with an info button that opens the full card. Mouse hover opens/closes it (with a short
 * grace period); click toggles it for touch and keyboard users; Escape and outside clicks close it (Radix).
 * @param data Loaded local-weather reading.
 */
function WeatherSummary({ data }: { data: LocalWeatherReading }) {
  const t = useTranslations('dashboard');
  const { locale } = useDisplayPreferences();
  const [unit] = useWeatherUnit();
  const [open, setOpen] = useState(false);
  const closeTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const degrees = new Intl.NumberFormat(normalizeFormattingLocale(locale), { maximumFractionDigits: 0 }).format(toUnit(data.temperature_c, unit));
  const { key } = weatherCondition(data.weather_code, data.is_day);

  /** Cancels a pending hover close. */
  const keepOpen = () => { if (closeTimer.current) clearTimeout(closeTimer.current); closeTimer.current = null; };
  /** Opens on mouse hover only; touch pointers fall through to the click toggle. */
  const hoverOpen = (event: ReactPointerEvent) => { if (event.pointerType === 'mouse') { keepOpen(); setOpen(true); } };
  /** Schedules a close when the mouse leaves the button or the card. */
  const hoverClose = (event: ReactPointerEvent) => {
    if (event.pointerType !== 'mouse') return;
    keepOpen();
    closeTimer.current = setTimeout(() => setOpen(false), HOVER_CLOSE_MS);
  };
  useEffect(() => keepOpen, []);

  return (
    <span className="font-normal text-muted-foreground">
      {' · '}
      {data.place && <><MapPin className="inline size-3.5 -translate-y-px" aria-hidden="true" /> {data.place} </>}
      <span className="tabular-nums text-foreground">{degrees}°{unit === 'f' ? 'F' : 'C'}</span> {t(key)}
      <Popover open={open} onOpenChange={setOpen}>
        <PopoverTrigger asChild>
          <button type="button" aria-label={t('weatherDetails')} onPointerEnter={hoverOpen} onPointerLeave={hoverClose}
            className="ml-1 inline-flex size-6 cursor-pointer items-center justify-center rounded-full border-0 bg-transparent p-0 align-middle text-muted-foreground hover:bg-raised hover:text-foreground focus-visible:outline-2 focus-visible:outline-ring">
            <Info className="size-4" aria-hidden="true" />
          </button>
        </PopoverTrigger>
        <PopoverContent align="end" className="w-auto p-0" onPointerEnter={keepOpen} onPointerLeave={hoverClose}>
          <WeatherCard data={data} />
        </PopoverContent>
      </Popover>
    </span>
  );
}
