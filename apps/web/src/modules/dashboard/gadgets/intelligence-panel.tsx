'use client';

import { useQuery } from '@tanstack/react-query';
import { useEffect, useState } from 'react';
import { useTranslations } from 'next-intl';
import { formatDateTime } from '@/core/i18n';
import { useDisplayPreferences } from '@/core/query-provider';
import { getCiiAvailability, getCorrelations } from '../intelligence-api';
import type { GadgetInstance } from '../api';

/** Display bounded, evidence-backed co-occurrence and the explicit unavailable CII method state. */
export function IntelligencePanel({ instance }: { instance: GadgetInstance }) {
  const t = useTranslations('dashboard');
  const display = useDisplayPreferences();
  const scope = instance.definition.scope;
  const sourceIds = instance.definition.source_ids ?? [];
  const regions = scope?.regions ?? [];
  const countries = scope?.cii_country_codes ?? [];
  const days = Math.max(1, Math.min(scope?.lookback_days ?? 30, 30));
  const [pageState, setPageState] = useState({ key: '', count: 20 });
  const scopeKey = `${instance.id}:${sourceIds.join(',')}:${regions.join(',')}:${days}`;
  // The page size resets whenever the scope changes.
  const visibleGroups = pageState.key === scopeKey ? pageState.count : 20;
  const setVisibleGroups = (update: (count: number) => number) => setPageState({ key: scopeKey, count: update(visibleGroups) });
  const to = new Date();
  const from = new Date(to.getTime() - days * 86_400_000);
  const correlation = useQuery({
    queryKey: ['intelligence-correlations', instance.id, sourceIds, regions, days],
    queryFn: ({ signal }) => getCorrelations({ sourceIds, regions, from, to }, signal),
    enabled: regions.length > 0,
    staleTime: 60_000,
  });
  const cii = useQuery({
    queryKey: ['intelligence-cii-v8', instance.id, countries],
    queryFn: ({ signal }) => getCiiAvailability(countries, signal),
    staleTime: 5 * 60_000,
  });

  return <section className="flex h-full min-h-0 flex-col gap-3 overflow-y-auto bg-card p-3 text-card-foreground">
    <header className="border-b border-border pb-2">
      <h2 className="text-sm font-semibold">{instance.title || t('intelligencePanelTitle')}</h2>
      <p className="mt-1 text-xs text-muted-foreground">{t('intelligenceRange', { days })}</p>
    </header>
    <section aria-labelledby={`correlation-${instance.id}`} className="space-y-2">
      <h3 id={`correlation-${instance.id}`} className="text-xs font-semibold">{t('correlationTitle')}</h3>
      {regions.length === 0 && <p role="status" className="text-xs text-muted-foreground">{t('correlationRegionRequired')}</p>}
      {correlation.isPending && <p role="status" className="text-xs text-muted-foreground">{t('observationLoading')}</p>}
      {correlation.isError && <p role="alert" className="text-xs text-destructive">{t('intelligenceLoadFailed')}</p>}
      {correlation.data && <>
        <p className="text-xs text-muted-foreground">{t('correlationMethod', { method: correlation.data.method_version })} · {t('correlationInterpretation')}</p>
        {correlation.data.missing_domains.length > 0 && <p role="status" className="text-xs text-muted-foreground">{t('correlationMissingDomains', { domains: correlation.data.missing_domains.join(', ') })}</p>}
        <ul aria-label={t('correlationCoverageLabel')} className="space-y-1 text-xs text-muted-foreground">
          {Object.entries(correlation.data.coverage).map(([domain, coverage]) => <li key={domain}>
            {t('correlationCoverageLine', { domain, count: coverage.signal_count, truncated: coverage.truncated ? t('correlationYes') : t('correlationNo'), omitted: coverage.omitted_source_count })}
          </li>)}
        </ul>
        {correlation.data.uncertainty_reasons.length > 0 && <p role="status" className="text-xs text-muted-foreground">{t('correlationUncertainty', { reasons: correlation.data.uncertainty_reasons.join(', ') })}</p>}
        {correlation.data.groups.length === 0 ? <p className="text-xs text-muted-foreground">{t('correlationEmpty')}</p> : <ul className="divide-y divide-border">
          {correlation.data.groups.slice(0, visibleGroups).map((group) => <li key={`${group.region}:${group.window_start}`} className="py-2 text-xs">
            <div className="flex items-center justify-between gap-2"><span className="font-medium">{group.region}</span><time dateTime={group.window_start} className="text-muted-foreground">{formatDateTime(group.window_start, display.locale, display.timezone)}</time></div>
            <p className="mt-1 text-muted-foreground">{t('correlationSignals', { count: group.signal_count, domains: group.domains_present.join(', ') })}</p>
            <p className="mt-1 break-all font-mono text-[10px] text-muted-foreground">{t('correlationEvidence')} {group.event_evidence_ids.slice(0, 8).join(', ') || group.observation_ids.slice(0, 8).join(', ') || group.document_ids.slice(0, 8).join(', ') || t('correlationNoEvidenceIds')}</p>
            {(group.omitted_document_ids + group.omitted_document_version_ids + group.omitted_event_evidence_ids) > 0 && <p role="status" className="mt-1 text-muted-foreground">{t('correlationSupportOmitted', { documents: group.omitted_document_ids, versions: group.omitted_document_version_ids, evidence: group.omitted_event_evidence_ids })}</p>}
          </li>)}
        </ul>}
        {visibleGroups < correlation.data.groups.length && <button type="button" onClick={() => setVisibleGroups((count) => Math.min(correlation.data?.groups.length ?? count, count + 20))} className="text-xs text-primary underline">{t('correlationMoreGroups', { count: correlation.data.groups.length - visibleGroups })}</button>}
      </>}
    </section>
    <section aria-labelledby={`cii-${instance.id}`} className="space-y-1 border-t border-border pt-3">
      <h3 id={`cii-${instance.id}`} className="text-xs font-semibold">{t('ciiPanelTitle')}</h3>
      {cii.isPending && <p role="status" className="text-xs text-muted-foreground">{t('observationLoading')}</p>}
      {cii.isError && <p role="alert" className="text-xs text-destructive">{t('intelligenceLoadFailed')}</p>}
      {cii.data && <p role="status" className="text-xs text-muted-foreground">{t('ciiUnavailableReason', { reason: cii.data.availability })} {cii.data.requested_countries.length > 0 ? t('ciiRequestedCountries', { countries: cii.data.requested_countries.join(', ') }) : t('ciiNoCountries')}</p>}
    </section>
  </section>;
}
