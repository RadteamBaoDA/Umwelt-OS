'use client';

import { useTranslations } from 'next-intl';
import { useEffect, useRef, useState } from 'react';
import { Checkbox } from '@/components/ui/checkbox';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import type { ConnectorConfig } from './api';

export type NativeProvider = 'youtube' | 'arxiv' | 'huggingface' | 'github' | 'github_releases' | 'telegram' | 'alpha_vantage' | 'open_meteo';

/** Renders fixed native-provider scope controls; GitHub resource flags and bounded history horizon remain in the owning revision-fenced draft. */
export function ProviderScope({
  provider,
  configuration,
  disabled,
  resetEpoch,
  onChange,
}: {
  provider: NativeProvider;
  configuration: ConnectorConfig;
  disabled: boolean;
  resetEpoch: number;
  onChange: <K extends keyof ConnectorConfig>(key: K, value: ConnectorConfig[K]) => void;
}) {
  const t = useTranslations('sources');
  const parsedTelegramIds = configuration.telegram_chat_ids ?? [];
  const parsedTelegramSignature = JSON.stringify(parsedTelegramIds);
  const telegramTextFromConfig = parsedTelegramIds.join(', ');
  const [telegramText, setTelegramText] = useState(() => parsedTelegramIds.join(', '));
  const lastEmittedSignature = useRef(parsedTelegramSignature);
  const lastResetEpoch = useRef(resetEpoch);
  useEffect(() => {
    // A reset is authoritative even when parsed values match; otherwise ignore local owner echoes that would erase typed separators.
    if (resetEpoch !== lastResetEpoch.current) {
      lastResetEpoch.current = resetEpoch;
      lastEmittedSignature.current = parsedTelegramSignature;
      setTelegramText(telegramTextFromConfig);
    } else if (parsedTelegramSignature !== lastEmittedSignature.current) {
      lastEmittedSignature.current = parsedTelegramSignature;
      setTelegramText(telegramTextFromConfig);
    }
  }, [parsedTelegramSignature, resetEpoch, telegramTextFromConfig]);
  /** Pairs a scope field with its translated label and constrained native input. */
  const field = (key: keyof ConnectorConfig, label: string, value: string, update: (value: string) => void, props: { maxLength?: number; pattern?: string } = {}) => (
    <div className="field" key={key}>
      <Label htmlFor={`source-scope-${key}`}>{label}</Label>
      <Input id={`source-scope-${key}`} value={value} disabled={disabled} required onChange={(event) => update(event.target.value)} {...props} />
    </div>
  );

  if (provider === 'youtube') return <>{field('youtube_channel_id', t('youtubeChannelId'), configuration.youtube_channel_id ?? '', (value) => onChange('youtube_channel_id', value), { maxLength: 24, pattern: 'UC[A-Za-z0-9_-]{22}' })}</>;
  if (provider === 'arxiv') return <>{field('arxiv_category', t('arxivCategory'), configuration.arxiv_category ?? '', (value) => onChange('arxiv_category', value), { maxLength: 64, pattern: '[A-Za-z][A-Za-z0-9.-]{0,63}' })}</>;
  if (provider === 'huggingface') return <>{field('huggingface_author', t('huggingfaceAuthor'), configuration.huggingface_author ?? '', (value) => onChange('huggingface_author', value), { maxLength: 96, pattern: '[A-Za-z0-9][A-Za-z0-9_-]{0,95}' })}</>;
  if (provider === 'github' || provider === 'github_releases') return <>
    {field('github_owner', t('githubOwner'), configuration.github_owner ?? '', (value) => onChange('github_owner', value), { maxLength: 39, pattern: '[A-Za-z0-9][A-Za-z0-9-]{0,38}' })}
    {field('github_repository', t('githubRepository'), configuration.github_repository ?? '', (value) => onChange('github_repository', value), { maxLength: 100, pattern: '[A-Za-z0-9_.-]{1,100}' })}
    {provider === 'github' && <>
      {(['include_issues', 'include_pulls', 'include_commits', 'include_releases'] as const).map((key) => <div className="field field-inline" key={key}>
        <Label htmlFor={`source-scope-${key}`}>{t(({ include_issues: 'githubIssues', include_pulls: 'githubPulls', include_commits: 'githubCommits', include_releases: 'githubReleases' } as const)[key])}</Label>
        <Checkbox id={`source-scope-${key}`} disabled={disabled} checked={configuration[key] ?? (key === 'include_commits' || key === 'include_releases')} onCheckedChange={(checked) => onChange(key, checked === true)} />
      </div>)}
      <div className="field">
        <Label htmlFor="source-scope-github_history_days">{t('githubHistoryDays')}</Label>
        <Input id="source-scope-github_history_days" type="number" min={1} max={365} value={configuration.github_history_days ?? 90} disabled={disabled} onChange={(event) => onChange('github_history_days', Number(event.target.value))} />
      </div>
    </>}
  </>;
  if (provider === 'alpha_vantage') return <>
    {field('market_symbols', t('marketSymbols'), (configuration.market_symbols ?? []).join(', '), (value) => onChange('market_symbols', value.split(/[\s,]+/).filter(Boolean).slice(0, 5)), { maxLength: 110 })}
    {field('market_currency', t('marketCurrency'), configuration.market_currency ?? '', (value) => onChange('market_currency', value.toUpperCase()), { maxLength: 3, pattern: '[A-Z]{3}' })}
    {field('market_exchange_timezone', t('marketExchangeTimezone'), configuration.market_exchange_timezone ?? '', (value) => onChange('market_exchange_timezone', value), { maxLength: 64 })}
  </>;
  if (provider === 'open_meteo') return <>
    {field('weather_latitude', t('weatherLatitude'), String(configuration.weather_latitude ?? ''), (value) => onChange('weather_latitude', value === '' ? undefined : Number(value)), { maxLength: 24, pattern: '-?[0-9]{1,2}(\\.[0-9]+)?' })}
    {field('weather_longitude', t('weatherLongitude'), String(configuration.weather_longitude ?? ''), (value) => onChange('weather_longitude', value === '' ? undefined : Number(value)), { maxLength: 24, pattern: '-?[0-9]{1,3}(\\.[0-9]+)?' })}
    {field('weather_timezone', t('weatherTimezone'), configuration.weather_timezone ?? '', (value) => onChange('weather_timezone', value), { maxLength: 64 })}
    {field('weather_metrics', t('weatherMetrics'), (configuration.weather_metrics ?? []).join(', '), (value) => onChange('weather_metrics', value.split(/[\s,]+/).filter(Boolean).slice(0, 4)), { maxLength: 200 })}
  </>;
  return <div className="field">
    <Label htmlFor="source-scope-telegram_chat_ids">{t('telegramChannelIds')}</Label>
    <Input id="source-scope-telegram_chat_ids" maxLength={2200} disabled={disabled} required value={telegramText} aria-describedby="source-scope-telegram-help" onChange={(event) => {
      const rawText = event.target.value;
      setTelegramText(rawText);
      const values = rawText.split(/[\s,]+/).filter(Boolean);
      // Keep raw separators and every entered value for continued typing and authoritative server bound/format rejection.
      lastEmittedSignature.current = JSON.stringify(values);
      onChange('telegram_chat_ids', values);
    }} />
    <small id="source-scope-telegram-help" className="muted">{t('telegramChannelHelp')}</small>
  </div>;
}
