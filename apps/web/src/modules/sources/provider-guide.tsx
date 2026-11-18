'use client';

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslations } from 'next-intl';
import Image from 'next/image';
import { useState } from 'react';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { Label } from '@/components/ui/label';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { formatDateTime } from '@/core/i18n';
import { safeHttpUrl } from '@/core/safe-url';
import { useDisplayPreferences } from '@/core/query-provider';
import {
  acknowledgeProviderTerms,
  connectorKeys,
  getProviderTerms,
  type ConnectorCatalogEntry,
  type DeclaredUse,
  type Source,
} from './api';
import { isStale, periodKind } from './freshness';
import { guideStepKeys } from './guide-keys';

/** One term/definition row of the guide's description list. */
function GuideRow({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="grid gap-1 sm:grid-cols-[10rem_1fr]">
      <dt className="font-medium">{label}</dt>
      <dd className="text-muted-foreground">{children}</dd>
    </div>
  );
}

/** Show provider credit and terms; TMDB also requires its linked logo and non-endorsement notice. */
function GuideAttribution({ entry }: { entry: ConnectorCatalogEntry }) {
  const t = useTranslations('sources');
  const isTmdb = entry.provider_id === 'tmdb_movies' || entry.provider_id === 'tmdb_tv';
  if (isTmdb) {
    return (
      <div className="grid justify-items-start gap-1">
        <a
          className="inline-flex w-fit rounded-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          href="https://www.themoviedb.org"
          target="_blank"
          rel="noopener noreferrer"
          aria-label={t('tmdb.logoLink')}
        >
          <Image src="/brand/tmdb-logo.svg" alt={t('tmdb.logoAlt')} width={160} height={12} className="h-3 w-auto" />
        </a>
        <span>{entry.attribution}</span>
        <span className="font-medium">{t('tmdb.attributionNotice')}</span>
        {entry.terms_url ? (
          <a
            className="text-foreground underline underline-offset-2"
            href={safeHttpUrl(entry.terms_url)}
            target="_blank"
            rel="noopener noreferrer"
          >
            {t('provider.terms')}
          </a>
        ) : null}
      </div>
    );
  }
  return (
    <>
      {entry.attribution}
      {entry.terms_url ? (
        <>
          {' '}
          <a
            className="text-foreground underline underline-offset-2"
            href={safeHttpUrl(entry.terms_url)}
            target="_blank"
            rel="noopener noreferrer"
          >
            {t('provider.terms')}
          </a>
        </>
      ) : null}
    </>
  );
}

/** Rows describing this specific source's last success (with stale badge) and last error; empty without a source. */
function GuideSourceRows({ source, stale }: { source: Source | null | undefined; stale: boolean }) {
  const t = useTranslations('sources');
  const display = useDisplayPreferences();
  if (!source) return null;
  const fmt = (value: string) => formatDateTime(value, display.locale, display.timezone);
  return (
    <>
      <GuideRow label={t('guide.lastSuccess')}>
        {source.last_success_at ? (
          <>
            {fmt(source.last_success_at)}
            {stale && (
              <span role="status" className="ml-2 rounded-md border border-border px-1.5 py-0.5 text-xs text-destructive">
                {t('stale')}
              </span>
            )}
          </>
        ) : (
          t('never')
        )}
      </GuideRow>
      {source.collection_error_code && (
        <GuideRow label={t('guide.lastError')}>
          <span role="alert" className="text-destructive">
            {source.collection_error_code}
            {source.last_success_at ? ` · ${t('guide.showingLastGood')}` : ''}
          </span>
        </GuideRow>
      )}
    </>
  );
}

/** Catalog facts of the guide: data hosts, period, key, cadence, quotas, attribution and runtime verification. */
function GuideCatalogRows({ entry, quotas, keyed }: { entry: ConnectorCatalogEntry; quotas: string[]; keyed: boolean }) {
  const t = useTranslations('sources');
  const interval = entry.default_interval_minutes ?? null;
  const period = periodKind(entry.provider_id);
  return (
    <>
      <GuideRow label={t('guide.data')}>{[...(entry.hosts ?? []), ...(entry.endpoints ?? [])].join(' · ') || t('guide.none')}</GuideRow>
      {period && (
        <GuideRow label={t('guide.period')}>{t(period === 'referenceDate' ? 'guide.referenceDate' : 'guide.annualPeriod')}</GuideRow>
      )}
      <GuideRow label={t('guide.key')}>{keyed ? t('guide.keyRequired') : t('guide.keyNone')}</GuideRow>
      {interval !== null && <GuideRow label={t('guide.cadence')}>{t('every.minutes', { minutes: interval })}</GuideRow>}
      {quotas.length > 0 && <GuideRow label={t('guide.quotaLabel')}>{quotas.join('; ')}</GuideRow>}
      {entry.attribution && (
        <GuideRow label={t('guide.attribution')}>
          <GuideAttribution entry={entry} />
        </GuideRow>
      )}
      <GuideRow label={t('guide.verified')}>{entry.runtime_verified ? t('guide.verifiedYes') : t('guide.verifiedNo')}</GuideRow>
    </>
  );
}

/** Example configuration and sample output blocks with their shared "illustrative" note. */
function GuideExamples({ entry }: { entry: ConnectorCatalogEntry }) {
  const t = useTranslations('sources');
  return (
    <>
      {entry.example_config && (
        <div className="grid gap-1">
          <strong>{t('guide.exampleConfig')}</strong>
          <pre className="overflow-x-auto rounded-md border border-border bg-muted p-2 text-xs" data-testid="guide-example-config">
            {JSON.stringify(entry.example_config, null, 2)}
          </pre>
        </div>
      )}
      {entry.sample_output && (
        <div className="grid gap-1">
          <strong>{t('guide.sampleOutput')}</strong>
          <pre className="overflow-x-auto rounded-md border border-border bg-muted p-2 text-xs" data-testid="guide-sample-output">
            {JSON.stringify(entry.sample_output, null, 2)}
          </pre>
        </div>
      )}
      {(entry.example_config || entry.sample_output) && <small className="text-muted-foreground">{t('guide.exampleNote')}</small>}
    </>
  );
}

/** Setup guide for a catalog provider. Shows only catalog facts: no invented quotas, terms or sample output. */
export function ProviderGuide({
  entry,
  source,
  now,
  onTest,
  testBusy,
  testPassed,
}: {
  entry: ConnectorCatalogEntry;
  source?: Source | null;
  now?: Date;
  /** Runs the existing owner validate action; it never calls the provider. Omitted until the source exists. */
  onTest?: () => void;
  testBusy?: boolean;
  testPassed?: boolean;
}) {
  const t = useTranslations('sources');
  if (!entry.terms_url && !entry.attribution) return null;
  const stale = source ? isStale(source.last_success_at, entry.default_interval_minutes ?? null, now) : false;
  const keyed = (entry.key_fields ?? []).length > 0;
  const quotas = (entry.quota_policies ?? []).map((q) => {
    const pacificDay = q.window === 'day_pacific';
    const window = pacificDay ? t('youtubeApi.pacificDay') : q.window;
    const unit = pacificDay ? t('youtubeApi.units') : q.unit;
    return q.limit_units === null ? t('guide.quotaUnknown', { window }) : t('guide.quota', { limit: q.limit_units, unit, window });
  });
  const steps = guideStepKeys(entry.provider_id, keyed);
  return (
    <section aria-label={t('guide.title')} className="grid gap-2 rounded-lg border border-border bg-surface p-3 text-sm">
      <h4 className="font-semibold">{t('guide.title')}</h4>
      {steps.length > 0 && (
        <div className="grid gap-1" data-testid="guide-steps">
          <strong>{t('guideSteps.title')}</strong>
          <ol className="ml-5 list-decimal text-muted-foreground">
            {steps.map((key) => (
              <li key={key}>{t(key)}</li>
            ))}
          </ol>
        </div>
      )}
      <dl className="grid gap-2">
        <GuideCatalogRows entry={entry} quotas={quotas} keyed={keyed} />
        <GuideSourceRows source={source} stale={stale} />
        <GuideRow label={t('guide.recovery')}>{t('guide.recoveryBody')}</GuideRow>
      </dl>
      <GuideExamples entry={entry} />
      <div className="flex flex-wrap items-center gap-2">
        <Button type="button" variant="outline" disabled={!onTest || testBusy} onClick={onTest}>
          {t('guide.test')}
        </Button>
        <small className="text-muted-foreground">{onTest ? t('guide.testNote') : t('guide.testNeedsSource')}</small>
        {testPassed && <small role="status">{t('guide.testPassed')}</small>}
      </div>
    </section>
  );
}

const TERMS_DECISION: Record<string, 'terms.eligibleYes' | 'terms.notAcknowledged' | 'terms.useIncompatible' | 'terms.operatorReview'> = {
  ok: 'terms.eligibleYes',
  terms_not_acknowledged: 'terms.notAcknowledged',
  use_incompatible: 'terms.useIncompatible',
  operator_review_required: 'terms.operatorReview',
};

/** Terms state for one source: the loaded decision, the declared-use draft, the read checkbox and the acknowledge mutation. */
function useProviderTerms(sourceId: string, entry: ConnectorCatalogEntry, csrfToken: string) {
  const queryClient = useQueryClient();
  const [use, setUse] = useState<DeclaredUse | ''>('');
  const [read, setRead] = useState(false);
  const termsQuery = useQuery({ queryKey: connectorKeys.terms(sourceId), queryFn: ({ signal }) => getProviderTerms(sourceId, signal) });
  const version = entry.terms_checked_on ?? '';
  const current = termsQuery.data ?? null;
  const declared = use || (current?.declared_use as DeclaredUse | undefined) || '';
  const save = useMutation({
    mutationFn: () => acknowledgeProviderTerms(sourceId, declared as DeclaredUse, version, csrfToken),
    onSuccess: (result) => {
      queryClient.setQueryData(connectorKeys.terms(sourceId), result);
      void queryClient.invalidateQueries({ queryKey: connectorKeys.activation(sourceId) });
    },
  });
  const decision = current ? (TERMS_DECISION[current.decision] ?? 'terms.notAcknowledged') : 'terms.notAcknowledged';
  return { termsQuery, current, version, declared, setUse, read, setRead, save, decision };
}

/** Owner declaration of deployment use and acknowledgement of the current catalog terms version. Activation stays blocked until the server reports the terms as accepted. */
export function ProviderTerms({
  sourceId,
  entry,
  csrfToken,
  disabled,
}: {
  sourceId: string;
  entry: ConnectorCatalogEntry;
  csrfToken: string;
  disabled?: boolean;
}) {
  const t = useTranslations('sources');
  const { termsQuery, current, version, declared, setUse, read, setRead, save, decision } = useProviderTerms(sourceId, entry, csrfToken);
  return (
    <section aria-label={t('terms.title')} className="grid gap-2 rounded-lg border border-border p-3 text-sm">
      <h4 className="font-semibold">{t('terms.title')}</h4>
      {!current?.eligible && (
        <p role="status" className="text-destructive">
          {t('terms.required')}
        </p>
      )}
      {termsQuery.isError ? (
        <p role="alert" className="text-destructive">
          {t('terms.loadFailed')}
        </p>
      ) : (
        <p>{t(decision)}</p>
      )}
      <p className="text-muted-foreground">
        {t('terms.version')}: {version || t('guide.none')} ·{' '}
        <a
          className="text-foreground underline underline-offset-2"
          href={safeHttpUrl(entry.terms_url)}
          target="_blank"
          rel="noopener noreferrer"
        >
          {t('provider.terms')}
        </a>
      </p>
      <div className="field">
        <Label htmlFor="terms-declared-use">{t('terms.declaredUse')}</Label>
        <Select value={declared} onValueChange={(value) => setUse(value as DeclaredUse)} disabled={disabled}>
          <SelectTrigger id="terms-declared-use">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="personal">{t('terms.usePersonal')}</SelectItem>
            <SelectItem value="noncommercial">{t('terms.useNoncommercial')}</SelectItem>
            <SelectItem value="commercial">{t('terms.useCommercial')}</SelectItem>
            <SelectItem value="unknown">{t('terms.useUnknown')}</SelectItem>
          </SelectContent>
        </Select>
      </div>
      <div className="flex items-center gap-3">
        <Checkbox id="terms-read" checked={read} disabled={disabled} onCheckedChange={(checked) => setRead(checked === true)} />
        <Label htmlFor="terms-read">{t('terms.acknowledge')}</Label>
      </div>
      <div className="flex items-center gap-2">
        <Button type="button" disabled={disabled || save.isPending || !declared || !read || !version} onClick={() => save.mutate()}>
          {save.isPending ? t('terms.saving') : t('terms.save')}
        </Button>
        {save.isSuccess && <small role="status">{t('terms.saved')}</small>}
        {save.isError && (
          <small role="alert" className="text-destructive">
            {t('actionFailed')}
          </small>
        )}
      </div>
    </section>
  );
}
