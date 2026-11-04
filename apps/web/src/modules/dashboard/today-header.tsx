'use client';

import { useQuery } from '@tanstack/react-query';
import { useTranslations } from 'next-intl';
import { useState } from 'react';
import { fetchOwnerPreferences, OWNER_PREFERENCES_KEY } from '@/core/preferences';
import { useDisplayPreferences } from '@/core/query-provider';
import { normalizeFormattingLocale } from '@/core/i18n';
import { listWorldObservationWindow } from '@/modules/observations/public-api';
import { fetchTopics } from '@/modules/news/api';
import { fetchStories } from '@/modules/news/story-api';
import { LocalWeather } from './local-weather';

/** Per-device last-visit marker. ponytail: localStorage only; sync to the server when multi-device matters. */
export const LAST_VISIT_KEY = 'umwelt.dashboard.lastVisit';

/** Maps an hour (0-23, owner timezone) to a greeting message key; night hours greet as evening. */
export function greetingKey(hour: number): 'greetingMorning' | 'greetingAfternoon' | 'greetingEvening' {
  if (hour >= 5 && hour < 12) return 'greetingMorning';
  if (hour >= 12 && hour < 18) return 'greetingAfternoon';
  return 'greetingEvening';
}

/** Per-tab-session pin of the "since" baseline, so re-mounts (StrictMode, navigation back) keep the same comparison point. */
export const SINCE_BASE_KEY = 'umwelt.dashboard.sinceBase';

/**
 * Returns the baseline timestamp (ms) for "since you last looked": null on a first visit or unavailable storage.
 * The baseline is pinned in sessionStorage on first call, so later calls in the same browser session return the same
 * value even though the per-device last-visit marker is advanced to `now` on that first call. Storage errors are
 * swallowed so a blocked store only disables the line.
 *
 * @param now Epoch ms recorded as the new last visit when no baseline is pinned yet.
 */
export function consumeLastVisit(now: number): number | null {
  try {
    let raw = window.sessionStorage.getItem(SINCE_BASE_KEY);
    if (raw === null) {
      raw = window.localStorage.getItem(LAST_VISIT_KEY) ?? '';
      window.localStorage.setItem(LAST_VISIT_KEY, new Date(now).toISOString());
      window.sessionStorage.setItem(SINCE_BASE_KEY, raw);
    }
    const parsed = raw ? Date.parse(raw) : NaN;
    return Number.isFinite(parsed) ? parsed : null;
  } catch {
    return null;
  }
}

/**
 * Today greeting (time of day, optional owner name, local date) and a "since you last looked" line with new
 * story counts for the owner's top active topics. Counts come from the first story page per topic, so they cap at
 * the page size. Read-only; hidden counts when there is no prior visit, no topics or nothing new.
 * After the date: local weather (browser location) as text with an info button that opens the full card; without it,
 * an Open-Meteo source's latest temperature (source name as the place); no source, no data or an error adds nothing.
 * @param name Owner display name override; defaults to the cached owner preference `display_name`.
 * @param sourceIds Source ids scoping story queries; counts are skipped when empty.
 * @param weatherSource Active Open-Meteo source (id, name used as place) for the weather line; omitted = no line.
 */
export function TodayHeader({ name: nameProp, sourceIds, weatherSource }: { name?: string; sourceIds: string[]; weatherSource?: { id: string; name: string } }) {
  const t = useTranslations('dashboard');
  // Read-only subscription to the preferences already loaded by the shell (disabled: never fetches here; queryFn only backs shared refetches).
  const prefs = useQuery({ queryKey: OWNER_PREFERENCES_KEY, queryFn: ({ signal }) => fetchOwnerPreferences(signal), enabled: false });
  const name = nameProp ?? prefs.data?.display_name ?? prefs.data?.resolved_display_name ?? undefined;
  const { locale, timezone } = useDisplayPreferences();
  const [{ now, lastVisit }] = useState(() => {
    const at = Date.now();
    return { now: new Date(at), lastVisit: consumeLastVisit(at) };
  });
  const hour = Number(new Intl.DateTimeFormat('en-US', { hour: 'numeric', hourCycle: 'h23', timeZone: timezone }).format(now));
  const greeting = t(greetingKey(hour));
  const date = new Intl.DateTimeFormat(normalizeFormattingLocale(locale), { dateStyle: 'full', timeZone: timezone }).format(now);
  const counts = useQuery({
    queryKey: ['today-since', lastVisit, sourceIds.join(',')],
    enabled: lastVisit !== null && sourceIds.length > 0,
    queryFn: async ({ signal }) => {
      const topics = (await fetchTopics({ isActive: true, limit: 3, signal })).items;
      const rows = await Promise.all(topics.map(async (topic) => {
        const page = await fetchStories(sourceIds, { limit: 25, topicId: topic.id });
        return { name: topic.name, count: page.items.filter((s) => Date.parse(s.observed_at) > lastVisit!).length };
      }));
      return rows.filter((row) => row.count > 0);
    },
  });
  const weather = useQuery({
    queryKey: ['today-weather', weatherSource?.id],
    enabled: Boolean(weatherSource),
    staleTime: 60_000,
    retry: false,
    queryFn: async ({ signal }) => {
      const nowMs = Date.now();
      const page = await listWorldObservationWindow({ sourceIds: [weatherSource!.id], metrics: ['temperature_2m'], from: new Date(nowMs - 3 * 3_600_000), to: new Date(nowMs + 3_600_000), maxPages: 1 }, signal);
      // Nearest-to-now reading with a real value; forecast rows inside the window count as "now".
      const rows = page.items.filter((i) => i.provider === 'open_meteo' && i.metric === 'temperature_2m' && i.value !== null && i.quality !== 'missing');
      const distance = (iso: string) => Math.abs(Date.parse(iso) - nowMs);
      const best = rows.reduce<(typeof rows)[number] | null>((acc, row) => (!acc || distance(row.observed_at) < distance(acc.observed_at) ? row : acc), null);
      return best ? best.value : null;
    },
  });
  const temperature = weather.data ?? null;
  const weatherText = temperature === null ? '' : (() => {
    // Celsius with no unit style noise: "Hanoi 24°" with a place, "24°C" without.
    const degrees = new Intl.NumberFormat(normalizeFormattingLocale(locale), { maximumFractionDigits: 0 }).format(temperature);
    return weatherSource?.name ? `${weatherSource.name} ${degrees}°` : `${degrees}°C`;
  })();
  const summary = (counts.data ?? []).map((row) => `${row.name} ${row.count}`).join(' · ');
  return (
    <div className="flex flex-col gap-0.5">
      <p className="m-0 text-base font-semibold text-foreground">
        {name ? t('greetingNamed', { greeting, name }) : greeting}
        <span className="font-normal text-muted-foreground"> · {date}</span>
        <LocalWeather fallback={weatherText ? <span className="tabular-nums">{weatherText}</span> : null} />
      </p>
      {summary && <p className="m-0 text-sm text-muted-foreground">{t('sinceLastLooked', { summary })}</p>}
    </div>
  );
}
