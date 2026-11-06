'use client';

import { useTranslations } from 'next-intl';
import { MapPin } from 'lucide-react';
import { useState } from 'react';
import { Bar, BarChart, LabelList, Line, LineChart, ResponsiveContainer, XAxis, YAxis } from 'recharts';
import { Tabs, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { normalizeFormattingLocale } from '@/core/i18n';
import { useDisplayPreferences } from '@/core/query-provider';
import type { LocalWeather } from './api';
import { toUnit, useWeatherUnit, weatherCondition, WEATHER_UNIT_KEY } from './weather-model';

export { WEATHER_UNIT_KEY, useWeatherUnit };
import { BareButton } from '@/components/bare-button';
import { getDateTimeFormat, getNumberFormat } from '@/modules/shared';

type Metric = 'temperature' | 'rain' | 'wind';

/** Hours between chart points: 24 forecast hours shown as 8 readable columns. */
const STEP_HOURS = 3;

/**
 * Google-style local weather card: place, condition icon, temperature with a °C/°F switch (persisted per device),
 * rain chance / humidity / wind, observation time in the owner's timezone, condition text, and a 24-hour forecast
 * chart (every 3 hours) with Temperature / Rain chance / Wind tabs. Readings the provider omitted are hidden; the
 * chart block is hidden when fewer than two hourly points exist. Frameless: the caller (header popover) draws the surface.
 * @param data Reading from the local-weather proxy.
 */
export function WeatherCard({ data }: { data: LocalWeather }) {
  const t = useTranslations('dashboard');
  const { locale, timezone } = useDisplayPreferences();
  const [unit, chooseUnit] = useWeatherUnit();
  const [metric, setMetric] = useState<Metric>('temperature');
  const formatLocale = normalizeFormattingLocale(locale);
  const whole = getNumberFormat(formatLocale, { maximumFractionDigits: 0 });
  const { key, Icon } = weatherCondition(data.weather_code, data.is_day);
  const observed = Date.parse(data.observed_at);
  const when = Number.isFinite(observed)
    ? getDateTimeFormat(formatLocale, { weekday: 'long', hour: '2-digit', minute: '2-digit', timeZone: timezone }).format(observed)
    : null;
  const hourFormat = getDateTimeFormat(formatLocale, { hour: '2-digit', minute: '2-digit', timeZone: timezone });

  // Each detail line renders only when the provider returned that reading.
  const details = [
    data.precipitation_probability_pct != null ? t('weatherRainChance', { value: whole.format(data.precipitation_probability_pct) }) : null,
    data.humidity_pct != null ? t('weatherHumidity', { value: whole.format(data.humidity_pct) }) : null,
    data.wind_kmh != null ? t('weatherWind', { value: whole.format(data.wind_kmh) }) : null,
  ].filter((line): line is string => line !== null);

  const rows = (data.hourly ?? []).filter((_, index) => index % STEP_HOURS === 0).map((point) => ({
    hour: hourFormat.format(Date.parse(point.at)),
    temperature: point.temperature_c == null ? null : Math.round(toUnit(point.temperature_c, unit)),
    rain: point.precipitation_probability_pct == null ? null : Math.round(point.precipitation_probability_pct),
    wind: point.wind_kmh == null ? null : Math.round(point.wind_kmh),
  }));
  const suffix = metric === 'temperature' ? '°' : metric === 'rain' ? '%' : '';
  const label = (value: unknown) => (typeof value === 'number' ? `${whole.format(value)}${suffix}` : '');
  const axis = <XAxis dataKey="hour" tick={{ fontSize: 11, fill: 'var(--muted-foreground)' }} tickLine={false} axisLine={false} interval={0} />;
  const labels = <LabelList dataKey={metric} position="top" formatter={label} style={{ fontSize: 11, fill: 'var(--foreground)' }} />;
  const chartLabel = metric === 'temperature' ? t('weatherTabTemperature') : metric === 'rain' ? t('weatherTabRain') : t('weatherTabWind');

  return (
    <section aria-label={t('localWeatherTitle')} className="flex w-[28rem] max-w-full min-w-0 flex-col gap-3 px-4 py-3">
      {data.place && (
        <p className="m-0 flex min-w-0 items-center gap-1.5 text-sm font-semibold text-foreground">
          <MapPin className="size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
          <span className="truncate">{data.place}</span>
        </p>
      )}
      <div className="flex flex-wrap items-center gap-x-5 gap-y-2">
        <div className="flex items-center gap-3">
          <Icon className="size-10 shrink-0 text-data" aria-hidden="true" />
          <p className="m-0 text-4xl font-semibold leading-none tracking-tight text-foreground tabular-nums">{whole.format(toUnit(data.temperature_c, unit))}</p>
          <div className="flex items-start self-start text-sm" role="group" aria-label={t('weatherUnit')}>
            {(['c', 'f'] as const).map((option, index) => (
              <span key={option} className="flex items-center">
                {index > 0 && <span className="px-0.5 text-muted-foreground" aria-hidden="true">|</span>}
                <BareButton type="button" aria-pressed={unit === option} onClick={() => chooseUnit(option)}
                  className="cursor-pointer rounded-sm border-0 bg-transparent p-0.5 text-muted-foreground hover:text-foreground aria-pressed:font-semibold aria-pressed:text-foreground focus-visible:outline-2 focus-visible:outline-ring">
                  {option === 'c' ? '°C' : '°F'}
                </BareButton>
              </span>
            ))}
          </div>
        </div>
        {details.length > 0 && (
          <ul className="m-0 list-none p-0 text-xs leading-5 text-muted-foreground tabular-nums">
            {details.map((line) => <li key={line}>{line}</li>)}
          </ul>
        )}
        <div className="ml-auto text-right">
          <p className="m-0 text-base font-medium text-foreground">{t(key)}</p>
          {when && <p className="m-0 text-xs text-muted-foreground first-letter:uppercase">{when}</p>}
        </div>
      </div>
      {rows.length >= 2 && (
        <div className="flex flex-col gap-1">
          <Tabs value={metric} onValueChange={(value) => setMetric(value as Metric)}>
            <TabsList aria-label={t('weatherForecastTabs')} className="h-auto justify-start gap-1 rounded-none bg-transparent p-0">
              {(['temperature', 'rain', 'wind'] as const).map((tab) => (
                <TabsTrigger key={tab} value={tab}
                  className="min-h-8 rounded-none border-b-2 border-transparent px-2 text-xs text-muted-foreground data-[state=active]:border-primary data-[state=active]:bg-transparent data-[state=active]:text-foreground data-[state=active]:shadow-none">
                  {tab === 'temperature' ? t('weatherTabTemperature') : tab === 'rain' ? t('weatherTabRain') : t('weatherTabWind')}
                </TabsTrigger>
              ))}
            </TabsList>
          </Tabs>
          <div className="h-28" role="img" aria-label={t('weatherNext24hChart', { metric: chartLabel })}>
            <ResponsiveContainer width="100%" height="100%">
              {metric === 'rain'
                ? <BarChart data={rows} margin={{ top: 18, right: 8, bottom: 0, left: 8 }}>
                    {axis}
                    <YAxis hide domain={[0, 100]} />
                    <Bar dataKey="rain" fill="var(--chart-1)" fillOpacity={0.6} radius={[2, 2, 0, 0]} isAnimationActive={false}>{labels}</Bar>
                  </BarChart>
                : <LineChart data={rows} margin={{ top: 18, right: 12, bottom: 0, left: 12 }}>
                    {axis}
                    <YAxis hide domain={['dataMin - 2', 'dataMax + 2']} />
                    <Line dataKey={metric} stroke="var(--chart-1)" strokeWidth={2} dot={false} isAnimationActive={false} connectNulls>{labels}</Line>
                  </LineChart>}
            </ResponsiveContainer>
          </div>
        </div>
      )}
    </section>
  );
}
