'use client';

import { useQuery } from '@tanstack/react-query';
import { useState } from 'react';
import { useTranslations } from 'next-intl';
import { formatDateTime } from '@/core/i18n';
import { useDisplayPreferences } from '@/core/query-provider';
import { getCorrelations, type CorrelationBucket } from '../intelligence-api';
import type { GadgetInstance } from '../api';
import { GadgetNotice, GadgetSkeleton } from './gadget-notice';
import { formatReasonLabel } from './metric-label';
import { BareButton } from '@/components/bare-button';

/** Sources, regions and the 1..30 day window the panel queries (defaults: none, none, 30 days). */
function readCorrelationScope(instance: GadgetInstance) {
  const scope = instance.definition.scope;
  return {
    sourceIds: instance.definition.source_ids ?? [],
    regions: scope?.regions ?? [],
    days: Math.max(1, Math.min(scope?.lookback_days ?? 30, 30)),
  };
}

/** One correlation group: region and window time, signal count, evidence ids (one click away) and omitted-support note. */
function CorrelationGroupItem({ group }: { group: CorrelationBucket }) {
  const t = useTranslations('dashboard');
  const display = useDisplayPreferences();
  const ids = group.event_evidence_ids.length ? group.event_evidence_ids : group.observation_ids.length ? group.observation_ids : group.document_ids;
  return <li className="py-2 text-xs">
    <div className="flex items-center justify-between gap-2"><span className="font-medium">{group.region}</span><time dateTime={group.window_start} className="text-muted-foreground">{formatDateTime(group.window_start, display.locale, display.timezone)}</time></div>
    <p className="mt-1 text-muted-foreground">{t('correlationSignals', { count: group.signal_count, domains: group.domains_present.join(', ') })}</p>
    {/* Raw ids are noise by default; keep them one click away. */}
    {ids.length === 0
      ? <p className="mt-1 text-xs text-muted-foreground">{t('correlationEvidence')} {t('correlationNoEvidenceIds')}</p>
      : <details className="mt-1 text-xs text-muted-foreground"><summary className="cursor-pointer">{t('correlationEvidence').replace(/:\s*$/, '')} ({ids.length})</summary><p className="mt-1 break-all text-xs tabular-nums">{ids.slice(0, 8).join(', ')}</p></details>}
    {(group.omitted_document_ids + group.omitted_document_version_ids + group.omitted_event_evidence_ids) > 0 && <p role="status" className="mt-1 text-muted-foreground">{t('correlationSupportOmitted', { documents: group.omitted_document_ids, versions: group.omitted_document_version_ids, evidence: group.omitted_event_evidence_ids })}</p>}
  </li>;
}

/**
 * Display bounded, evidence-backed co-occurrence, strongest groups (most signals) first, with coverage and uncertainty in a collapsed details block.
 * The CII score section is intentionally not rendered while its
 * method is unavailable: an unavailable method is hidden rather than explained to the user.
 */
export function IntelligencePanel({ instance }: { instance: GadgetInstance }) {
  const t = useTranslations('dashboard');
  const { sourceIds, regions, days } = readCorrelationScope(instance);
  const [pageState, setPageState] = useState({ key: '', count: 20 });
  const scopeKey = `${instance.id}:${sourceIds.join(',')}:${regions.join(',')}:${days}`;
  // The page size resets whenever the scope changes.
  const visibleGroups = pageState.key === scopeKey ? pageState.count : 20;
  /** Applies an update to the visible group count, scoped to the current query key. */
  const setVisibleGroups = (update: (count: number) => number) => setPageState({ key: scopeKey, count: update(visibleGroups) });
  const to = new Date();
  const from = new Date(to.getTime() - days * 86_400_000);
  const correlation = useQuery({
    queryKey: ['intelligence-correlations', instance.id, sourceIds, regions, days],
    queryFn: ({ signal }) => getCorrelations({ sourceIds, regions, from, to }, signal),
    enabled: regions.length > 0,
    staleTime: 60_000,
  });

  return <section className="flex h-full min-h-0 flex-col gap-3 overflow-y-auto bg-card p-3 text-card-foreground">
    <p className="m-0 text-xs text-muted-foreground">{t('intelligenceRange', { days })}</p>
    <section aria-labelledby={`correlation-${instance.id}`} className="flex flex-1 flex-col gap-2">
      <h3 id={`correlation-${instance.id}`} className="text-xs font-semibold">{t('correlationTitle')}</h3>
      {regions.length === 0 && <GadgetNotice kind="setup" message={t('correlationRegionRequired')} action={{ href: '/settings/sources', label: t('gadgetConfigureAction') }} className="px-0 py-1 text-xs" />}
      {regions.length > 0 && correlation.isPending && <GadgetSkeleton variant="list" label={t('observationLoading')} className="px-0" />}
      {correlation.isError && <GadgetNotice kind="error" message={t('intelligenceLoadFailed')} className="px-0 py-1 text-xs" />}
      {correlation.data && <>
        <p className="text-xs text-muted-foreground">{t('correlationInterpretation')}</p>
        {correlation.data.missing_domains.length > 0 && <p role="status" className="text-xs text-muted-foreground">{t('correlationMissingDomains', { domains: correlation.data.missing_domains.join(', ') })}</p>}
        {/* Coverage and uncertainty are method detail: one click away, groups lead. */}
        <details className="text-xs text-muted-foreground">
          <summary className="cursor-pointer">{t('correlationDetails')}</summary>
          <ul aria-label={t('correlationCoverageLabel')} className="mt-1 space-y-1">
            {Object.entries(correlation.data.coverage).map(([domain, coverage]) => <li key={domain}>
              {t('correlationCoverageLine', { domain, count: coverage.signal_count, truncated: coverage.truncated ? t('correlationYes') : t('correlationNo'), omitted: coverage.omitted_source_count })}
            </li>)}
          </ul>
          {correlation.data.uncertainty_reasons.length > 0 && <p role="status" className="mt-1">{t('correlationUncertainty', { reasons: correlation.data.uncertainty_reasons.map((reason) => formatReasonLabel(reason, t)).join(' ') })}</p>}
        </details>
        {correlation.data.groups.length === 0 ? <GadgetNotice kind="empty" message={t('correlationEmpty')} action={{ href: '/settings/sources', label: t('gadgetConfigureAction') }} className="my-auto px-0 py-1 text-xs" /> : <ul className="divide-y divide-border">
          {[...correlation.data.groups].sort((a, b) => b.signal_count - a.signal_count).slice(0, visibleGroups).map((group) => <CorrelationGroupItem key={`${group.region}:${group.window_start}`} group={group} />)}
        </ul>}
        {visibleGroups < correlation.data.groups.length && <BareButton type="button" onClick={() => setVisibleGroups((count) => Math.min(correlation.data?.groups.length ?? count, count + 20))} className="text-xs text-primary underline">{t('correlationMoreGroups', { count: correlation.data.groups.length - visibleGroups })}</BareButton>}
      </>}
    </section>
  </section>;
}
