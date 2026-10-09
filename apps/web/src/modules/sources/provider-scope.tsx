'use client';

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslations } from 'next-intl';
import { useEffect, useRef, useState } from 'react';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { formatDateTime } from '@/core/i18n';
import { useDisplayPreferences } from '@/core/query-provider';
import { acknowledgeProviderTerms, connectorKeys, getProviderTerms, type ConnectorCatalogEntry, type ConnectorConfig, type DeclaredUse, type Source } from './api';
import { isStale, periodKind } from './freshness';

export type NativeProvider = 'youtube' | 'arxiv' | 'huggingface' | 'github' | 'github_releases' | 'telegram' | 'alpha_vantage' | 'open_meteo' | 'google_news';

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

  if (provider === 'google_news') return <>
    {field('news_query', t('newsQuery'), configuration.news_query ?? '', (value) => onChange('news_query', value), { maxLength: 200 })}
    <div className="field">
      <Label htmlFor="source-scope-news_site">{t('newsSite')}</Label>
      <Select value={configuration.news_site ?? 'any'} onValueChange={(value) => onChange('news_site', value as ConnectorConfig['news_site'])} disabled={disabled}>
        <SelectTrigger id="source-scope-news_site"><SelectValue /></SelectTrigger>
        <SelectContent>
          <SelectItem value="any">{t('newsSiteAny')}</SelectItem>
          {(['reuters.com', 'apnews.com', 'bbc.com', 'vnexpress.net'] as const).map((site) => <SelectItem key={site} value={site}>{site}</SelectItem>)}
        </SelectContent>
      </Select>
    </div>
    <div className="field">
      <Label htmlFor="source-scope-news_locale">{t('newsLocale')}</Label>
      <Select value={configuration.news_locale ?? 'en-US'} onValueChange={(value) => onChange('news_locale', value as ConnectorConfig['news_locale'])} disabled={disabled}>
        <SelectTrigger id="source-scope-news_locale"><SelectValue /></SelectTrigger>
        <SelectContent>
          <SelectItem value="en-US">{t('newsLocaleEn')}</SelectItem>
          <SelectItem value="vi-VN">{t('newsLocaleVi')}</SelectItem>
        </SelectContent>
      </Select>
    </div>
    <small className="muted">{t('newsHelp')}</small>
  </>;
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

/** Setup guide for a catalog provider. Shows only catalog facts: no invented quotas, terms or sample output. */
export function ProviderGuide({ entry, source, now, onTest, testBusy, testPassed }: {
  entry: ConnectorCatalogEntry; source?: Source | null; now?: Date;
  /** Runs the existing owner validate action; it never calls the provider. Omitted until the source exists. */
  onTest?: () => void; testBusy?: boolean; testPassed?: boolean;
}) {
  const t = useTranslations('sources');
  const display = useDisplayPreferences();
  if (!entry.terms_url && !entry.attribution) return null;
  const fmt = (value: string) => formatDateTime(value, display.locale, display.timezone);
  const interval = entry.default_interval_minutes ?? null;
  const stale = source ? isStale(source.last_success_at, interval, now) : false;
  const period = periodKind(entry.provider_id);
  const keyed = (entry.key_fields ?? []).length > 0;
  const quotas = (entry.quota_policies ?? []).map((q) => (q.limit_units === null ? t('guideQuotaUnknown', { window: q.window }) : t('guideQuota', { limit: q.limit_units, unit: q.unit, window: q.window })));
  const row = (label: string, body: React.ReactNode) => <div className="grid gap-1 sm:grid-cols-[10rem_1fr]"><dt className="font-medium">{label}</dt><dd className="text-muted-foreground">{body}</dd></div>;
  return <section aria-label={t('guideTitle')} className="grid gap-2 rounded-lg border border-border bg-surface p-3 text-sm">
    <h4 className="font-semibold">{t('guideTitle')}</h4>
    <dl className="grid gap-2">
      {row(t('guideData'), [...(entry.hosts ?? []), ...(entry.endpoints ?? [])].join(' · ') || t('guideNone'))}
      {period && row(t('guidePeriod'), t(period === 'referenceDate' ? 'guideReferenceDate' : 'guideAnnualPeriod'))}
      {row(t('guideKey'), keyed ? t('guideKeyRequired') : t('guideKeyNone'))}
      {interval !== null && row(t('guideCadence'), t('everyMinutes', { minutes: interval }))}
      {quotas.length > 0 && row(t('guideQuotaLabel'), quotas.join('; '))}
      {entry.attribution && row(t('guideAttribution'), <>{entry.attribution}{entry.terms_url ? <> <a className="text-accent underline" href={entry.terms_url} target="_blank" rel="noopener noreferrer">{t('providerTerms')}</a></> : null}</>)}
      {row(t('guideVerified'), entry.runtime_verified ? t('guideVerifiedYes') : t('guideVerifiedNo'))}
      {source && row(t('guideLastSuccess'), source.last_success_at ? <>{fmt(source.last_success_at)}{stale && <span role="status" className="ml-2 rounded-md border border-border px-1.5 py-0.5 text-xs text-danger">{t('stale')}</span>}</> : t('never'))}
      {source?.collection_error_code && row(t('guideLastError'), <span role="alert" className="text-danger">{source.collection_error_code}{source.last_success_at ? ` · ${t('guideShowingLastGood')}` : ''}</span>)}
      {row(t('guideRecovery'), t('guideRecoveryBody'))}
    </dl>
    {entry.example_config && <div className="grid gap-1"><strong>{t('guideExampleConfig')}</strong>
      <pre className="overflow-x-auto rounded-md border border-border bg-bg p-2 text-xs" data-testid="guide-example-config">{JSON.stringify(entry.example_config, null, 2)}</pre></div>}
    {entry.sample_output && <div className="grid gap-1"><strong>{t('guideSampleOutput')}</strong>
      <pre className="overflow-x-auto rounded-md border border-border bg-bg p-2 text-xs" data-testid="guide-sample-output">{JSON.stringify(entry.sample_output, null, 2)}</pre></div>}
    {(entry.example_config || entry.sample_output) && <small className="text-muted-foreground">{t('guideExampleNote')}</small>}
    <div className="flex flex-wrap items-center gap-2">
      <Button type="button" className="secondary" disabled={!onTest || testBusy} onClick={onTest}>{t('guideTest')}</Button>
      <small className="text-muted-foreground">{onTest ? t('guideTestNote') : t('guideTestNeedsSource')}</small>
      {testPassed && <small role="status">{t('guideTestPassed')}</small>}
    </div>
  </section>;
}

const TERMS_DECISION: Record<string, 'termsEligibleYes' | 'termsNotAcknowledged' | 'termsUseIncompatible' | 'termsOperatorReview'> = {
  ok: 'termsEligibleYes', terms_not_acknowledged: 'termsNotAcknowledged', use_incompatible: 'termsUseIncompatible', operator_review_required: 'termsOperatorReview',
};

/** Owner declaration of deployment use and acknowledgement of the current catalog terms version. Activation stays blocked until the server reports the terms as accepted. */
export function ProviderTerms({ sourceId, entry, csrfToken, disabled }: { sourceId: string; entry: ConnectorCatalogEntry; csrfToken: string; disabled?: boolean }) {
  const t = useTranslations('sources');
  const queryClient = useQueryClient();
  const [use, setUse] = useState<DeclaredUse | ''>('');
  const [read, setRead] = useState(false);
  const termsQuery = useQuery({ queryKey: connectorKeys.terms(sourceId), queryFn: ({ signal }) => getProviderTerms(sourceId, signal) });
  const version = entry.terms_checked_on ?? '';
  const save = useMutation({
    mutationFn: () => acknowledgeProviderTerms(sourceId, declared as DeclaredUse, version, csrfToken),
    onSuccess: (result) => {
      queryClient.setQueryData(connectorKeys.terms(sourceId), result);
      void queryClient.invalidateQueries({ queryKey: connectorKeys.activation(sourceId) });
    },
  });
  const current = termsQuery.data ?? null;
  const decision = current ? TERMS_DECISION[current.decision] ?? 'termsNotAcknowledged' : 'termsNotAcknowledged';
  const declared = use || (current?.declared_use as DeclaredUse | undefined) || '';
  return <section aria-label={t('termsTitle')} className="grid gap-2 rounded-lg border border-border p-3 text-sm">
    <h4 className="font-semibold">{t('termsTitle')}</h4>
    {!current?.eligible && <p role="status" className="text-danger">{t('termsRequired')}</p>}
    {termsQuery.isError ? <p role="alert" className="text-danger">{t('termsLoadFailed')}</p> : <p>{t(decision)}</p>}
    <p className="text-muted-foreground">{t('termsVersion')}: {version || t('guideNone')} · <a className="text-accent underline" href={entry.terms_url ?? undefined} target="_blank" rel="noopener noreferrer">{t('providerTerms')}</a></p>
    <div className="field"><Label htmlFor="terms-declared-use">{t('termsDeclaredUse')}</Label>
      <Select value={declared} onValueChange={(value) => setUse(value as DeclaredUse)} disabled={disabled}>
        <SelectTrigger id="terms-declared-use"><SelectValue /></SelectTrigger>
        <SelectContent>
          <SelectItem value="personal">{t('termsUsePersonal')}</SelectItem><SelectItem value="noncommercial">{t('termsUseNoncommercial')}</SelectItem>
          <SelectItem value="commercial">{t('termsUseCommercial')}</SelectItem><SelectItem value="unknown">{t('termsUseUnknown')}</SelectItem>
        </SelectContent>
      </Select></div>
    <div className="field field-inline"><Checkbox id="terms-read" checked={read} disabled={disabled} onCheckedChange={(checked) => setRead(checked === true)} /><Label htmlFor="terms-read">{t('termsAcknowledge')}</Label></div>
    <div className="flex items-center gap-2">
      <Button type="button" disabled={disabled || save.isPending || !declared || !read || !version} onClick={() => save.mutate()}>{save.isPending ? t('termsSaving') : t('termsSave')}</Button>
      {save.isSuccess && <small role="status">{t('termsSaved')}</small>}
      {save.isError && <small role="alert" className="text-danger">{t('actionFailed')}</small>}
    </div>
  </section>;
}
