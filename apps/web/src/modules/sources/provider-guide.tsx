'use client';

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslations } from 'next-intl';
import { useState } from 'react';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { Label } from '@/components/ui/label';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { formatDateTime } from '@/core/i18n';
import { safeHttpUrl } from '@/core/safe-url';
import { useDisplayPreferences } from '@/core/query-provider';
import { acknowledgeProviderTerms, connectorKeys, getProviderTerms, type ConnectorCatalogEntry, type DeclaredUse, type Source } from './api';
import { isStale, periodKind } from './freshness';

const GUIDE_MAX_STEPS = 8;

/**
 * Resolves the ordered message keys of a provider's setup steps.
 * Falls back from the provider's own guide to its generic source type (`rss`, `rest`, `web`, `mcp` are their own ids)
 * and finally to the generic manual or keyed guide, so every manually configured source shows steps.
 * @param t Translator for the `sources` namespace (only `has` is used).
 * @param providerId Catalog provider id.
 * @param keyed Whether the provider needs an owner-entered key.
 * @returns Message keys `guideSteps.<id>.s1..sN` in order; empty when no guide exists.
 */
function guideStepKeys(t: { has(key: string): boolean }, providerId: string, keyed: boolean): string[] {
  const id = [providerId, keyed ? 'genericKeyed' : 'generic'].find((candidate) => t.has(`guideSteps.${candidate}.s1`));
  if (!id) return [];
  const keys: string[] = [];
  for (let n = 1; n <= GUIDE_MAX_STEPS && t.has(`guideSteps.${id}.s${n}`); n += 1) keys.push(`guideSteps.${id}.s${n}`);
  return keys;
}

/** One term/definition row of the guide's description list. */
function GuideRow({ label, children }: { label: string; children: React.ReactNode }) {
  return <div className="grid gap-1 sm:grid-cols-[10rem_1fr]"><dt className="font-medium">{label}</dt><dd className="text-muted-foreground">{children}</dd></div>;
}

/** Attribution text plus its provider terms link (when the catalog has a terms URL). */
function GuideAttribution({ entry }: { entry: ConnectorCatalogEntry }) {
  const t = useTranslations('sources');
  return <>{entry.attribution}{entry.terms_url ? <> <a className="text-foreground underline underline-offset-2" href={safeHttpUrl(entry.terms_url)} target="_blank" rel="noopener noreferrer">{t('providerTerms')}</a></> : null}</>;
}

/** Rows describing this specific source's last success (with stale badge) and last error; empty without a source. */
function GuideSourceRows({ source, stale }: { source: Source | null | undefined; stale: boolean }) {
  const t = useTranslations('sources');
  const display = useDisplayPreferences();
  if (!source) return null;
  const fmt = (value: string) => formatDateTime(value, display.locale, display.timezone);
  return <>
    <GuideRow label={t('guideLastSuccess')}>{source.last_success_at ? <>{fmt(source.last_success_at)}{stale && <span role="status" className="ml-2 rounded-md border border-border px-1.5 py-0.5 text-xs text-destructive">{t('stale')}</span>}</> : t('never')}</GuideRow>
    {source.collection_error_code && <GuideRow label={t('guideLastError')}><span role="alert" className="text-destructive">{source.collection_error_code}{source.last_success_at ? ` · ${t('guideShowingLastGood')}` : ''}</span></GuideRow>}
  </>;
}

/** Catalog facts of the guide: data hosts, period, key, cadence, quotas, attribution and runtime verification. */
function GuideCatalogRows({ entry, quotas, keyed }: { entry: ConnectorCatalogEntry; quotas: string[]; keyed: boolean }) {
  const t = useTranslations('sources');
  const interval = entry.default_interval_minutes ?? null;
  const period = periodKind(entry.provider_id);
  return <>
    <GuideRow label={t('guideData')}>{[...(entry.hosts ?? []), ...(entry.endpoints ?? [])].join(' · ') || t('guideNone')}</GuideRow>
    {period && <GuideRow label={t('guidePeriod')}>{t(period === 'referenceDate' ? 'guideReferenceDate' : 'guideAnnualPeriod')}</GuideRow>}
    <GuideRow label={t('guideKey')}>{keyed ? t('guideKeyRequired') : t('guideKeyNone')}</GuideRow>
    {interval !== null && <GuideRow label={t('guideCadence')}>{t('everyMinutes', { minutes: interval })}</GuideRow>}
    {quotas.length > 0 && <GuideRow label={t('guideQuotaLabel')}>{quotas.join('; ')}</GuideRow>}
    {entry.attribution && <GuideRow label={t('guideAttribution')}><GuideAttribution entry={entry} /></GuideRow>}
    <GuideRow label={t('guideVerified')}>{entry.runtime_verified ? t('guideVerifiedYes') : t('guideVerifiedNo')}</GuideRow>
  </>;
}

/** Example configuration and sample output blocks with their shared "illustrative" note. */
function GuideExamples({ entry }: { entry: ConnectorCatalogEntry }) {
  const t = useTranslations('sources');
  return <>
    {entry.example_config && <div className="grid gap-1"><strong>{t('guideExampleConfig')}</strong>
      <pre className="overflow-x-auto rounded-md border border-border bg-muted p-2 text-xs" data-testid="guide-example-config">{JSON.stringify(entry.example_config, null, 2)}</pre></div>}
    {entry.sample_output && <div className="grid gap-1"><strong>{t('guideSampleOutput')}</strong>
      <pre className="overflow-x-auto rounded-md border border-border bg-muted p-2 text-xs" data-testid="guide-sample-output">{JSON.stringify(entry.sample_output, null, 2)}</pre></div>}
    {(entry.example_config || entry.sample_output) && <small className="text-muted-foreground">{t('guideExampleNote')}</small>}
  </>;
}

/** Setup guide for a catalog provider. Shows only catalog facts: no invented quotas, terms or sample output. */
export function ProviderGuide({ entry, source, now, onTest, testBusy, testPassed }: {
  entry: ConnectorCatalogEntry; source?: Source | null; now?: Date;
  /** Runs the existing owner validate action; it never calls the provider. Omitted until the source exists. */
  onTest?: () => void; testBusy?: boolean; testPassed?: boolean;
}) {
  const t = useTranslations('sources');
  if (!entry.terms_url && !entry.attribution) return null;
  const stale = source ? isStale(source.last_success_at, entry.default_interval_minutes ?? null, now) : false;
  const keyed = (entry.key_fields ?? []).length > 0;
  const quotas = (entry.quota_policies ?? []).map((q) => (q.limit_units === null ? t('guideQuotaUnknown', { window: q.window }) : t('guideQuota', { limit: q.limit_units, unit: q.unit, window: q.window })));
  const steps = guideStepKeys(t, entry.provider_id, keyed);
  return <section aria-label={t('guideTitle')} className="grid gap-2 rounded-lg border border-border bg-surface p-3 text-sm">
    <h4 className="font-semibold">{t('guideTitle')}</h4>
    {steps.length > 0 && <div className="grid gap-1" data-testid="guide-steps"><strong>{t('guideStepsTitle')}</strong>
      <ol className="ml-5 list-decimal text-muted-foreground">{steps.map((key) => <li key={key}>{t(key as 'guideTitle')}</li>)}</ol></div>}
    <dl className="grid gap-2">
      <GuideCatalogRows entry={entry} quotas={quotas} keyed={keyed} />
      <GuideSourceRows source={source} stale={stale} />
      <GuideRow label={t('guideRecovery')}>{t('guideRecoveryBody')}</GuideRow>
    </dl>
    <GuideExamples entry={entry} />
    <div className="flex flex-wrap items-center gap-2">
      <Button type="button" variant="outline" disabled={!onTest || testBusy} onClick={onTest}>{t('guideTest')}</Button>
      <small className="text-muted-foreground">{onTest ? t('guideTestNote') : t('guideTestNeedsSource')}</small>
      {testPassed && <small role="status">{t('guideTestPassed')}</small>}
    </div>
  </section>;
}

const TERMS_DECISION: Record<string, 'termsEligibleYes' | 'termsNotAcknowledged' | 'termsUseIncompatible' | 'termsOperatorReview'> = {
  ok: 'termsEligibleYes', terms_not_acknowledged: 'termsNotAcknowledged', use_incompatible: 'termsUseIncompatible', operator_review_required: 'termsOperatorReview',
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
  const decision = current ? TERMS_DECISION[current.decision] ?? 'termsNotAcknowledged' : 'termsNotAcknowledged';
  return { termsQuery, current, version, declared, setUse, read, setRead, save, decision };
}

/** Owner declaration of deployment use and acknowledgement of the current catalog terms version. Activation stays blocked until the server reports the terms as accepted. */
export function ProviderTerms({ sourceId, entry, csrfToken, disabled }: { sourceId: string; entry: ConnectorCatalogEntry; csrfToken: string; disabled?: boolean }) {
  const t = useTranslations('sources');
  const { termsQuery, current, version, declared, setUse, read, setRead, save, decision } = useProviderTerms(sourceId, entry, csrfToken);
  return <section aria-label={t('termsTitle')} className="grid gap-2 rounded-lg border border-border p-3 text-sm">
    <h4 className="font-semibold">{t('termsTitle')}</h4>
    {!current?.eligible && <p role="status" className="text-destructive">{t('termsRequired')}</p>}
    {termsQuery.isError ? <p role="alert" className="text-destructive">{t('termsLoadFailed')}</p> : <p>{t(decision)}</p>}
    <p className="text-muted-foreground">{t('termsVersion')}: {version || t('guideNone')} · <a className="text-foreground underline underline-offset-2" href={safeHttpUrl(entry.terms_url)} target="_blank" rel="noopener noreferrer">{t('providerTerms')}</a></p>
    <div className="field"><Label htmlFor="terms-declared-use">{t('termsDeclaredUse')}</Label>
      <Select value={declared} onValueChange={(value) => setUse(value as DeclaredUse)} disabled={disabled}>
        <SelectTrigger id="terms-declared-use"><SelectValue /></SelectTrigger>
        <SelectContent>
          <SelectItem value="personal">{t('termsUsePersonal')}</SelectItem><SelectItem value="noncommercial">{t('termsUseNoncommercial')}</SelectItem>
          <SelectItem value="commercial">{t('termsUseCommercial')}</SelectItem><SelectItem value="unknown">{t('termsUseUnknown')}</SelectItem>
        </SelectContent>
      </Select></div>
    <div className="flex items-center gap-3"><Checkbox id="terms-read" checked={read} disabled={disabled} onCheckedChange={(checked) => setRead(checked === true)} /><Label htmlFor="terms-read">{t('termsAcknowledge')}</Label></div>
    <div className="flex items-center gap-2">
      <Button type="button" disabled={disabled || save.isPending || !declared || !read || !version} onClick={() => save.mutate()}>{save.isPending ? t('termsSaving') : t('termsSave')}</Button>
      {save.isSuccess && <small role="status">{t('termsSaved')}</small>}
      {save.isError && <small role="alert" className="text-destructive">{t('actionFailed')}</small>}
    </div>
  </section>;
}
