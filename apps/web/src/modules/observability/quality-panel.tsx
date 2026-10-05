'use client';

import { useQuery } from '@tanstack/react-query';
import { useTranslations } from 'next-intl';
import { Button } from '@/components/ui/button';
import { getQuality, getQueueSummary, getSystemHealth } from './api';

/** Loads independent bounded SQL and health summaries so one unavailable profile never hides UI errors. */
export function QualityPanel() {
  const t = useTranslations('observability');
  const quality = useQuery({ queryKey: ['observability', 'quality'], queryFn: getQuality, refetchInterval: 60_000 });
  const queue = useQuery({ queryKey: ['observability', 'queue'], queryFn: getQueueSummary, refetchInterval: 15_000 });
  const health = useQuery({ queryKey: ['system-health'], queryFn: getSystemHealth, refetchInterval: 15_000 });
  const worker = health.data?.components.worker?.status ?? t('loading');
  return <section className="space-y-4" aria-labelledby="quality-heading">
    <h3 id="quality-heading" className="text-lg font-semibold">{t('quality')}</h3>
    {quality.isPending ? <p className="muted" role="status">{t('loading')}</p> : quality.isError ? <div role="alert"><p>{t('qualityUnavailable')}</p><Button type="button" className="secondary mt-2" onClick={() => void quality.refetch()}>{t('retry')}</Button></div> : <dl className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
      <Metric label={t('documents')} value={quality.data.document_count} />
      <Metric label={t('duplicateRate')} value={`${(quality.data.duplicate_rate * 100).toFixed(1)}%`} />
      <Metric label={t('unresolvedEntities')} value={quality.data.unresolved_entities} />
      <Metric label={t('failedIngestion')} value={quality.data.failed_ingestion} />
      <Metric label={t('failedExtraction')} value={quality.data.failed_extraction} />
      <Metric label={t('staleSources')} value={quality.data.stale_sources} />
      <Metric label={t('orphanChunks')} value={quality.data.orphan_chunks} />
      <Metric label={t('graphLag')} value={quality.data.graph_sync_lag_seconds === null ? t('unknown') : `${quality.data.graph_sync_lag_seconds}s`} />
    </dl>}
    <div className="grid gap-3 sm:grid-cols-2">
      <article className="rounded-md border border-border p-4"><h4 className="font-medium">{t('workerHealth')}</h4>{health.isError ? <div role="alert"><p>{t('healthUnavailable')}</p><Button type="button" className="secondary mt-2" onClick={() => void health.refetch()}>{t('retry')}</Button></div> : <p className="muted">{worker}</p>}</article>
      <article className="rounded-md border border-border p-4"><h4 className="font-medium">{t('queue')}</h4>
        {queue.isPending ? <p className="muted">{t('loading')}</p> : queue.isError ? <div role="alert"><p>{t('queueUnavailable')}</p><Button type="button" className="secondary mt-2" onClick={() => void queue.refetch()}>{t('retry')}</Button></div> : <>
          <p className="muted">{t('ingestionStages')}: {summary(queue.data.ingestion_stages)}</p>
          <p className="muted">{t('retryEligible')}: {queue.data.retryable_ingestion_stages}</p>
          <p className="muted">{t('eventDelivery')}: {summary(queue.data.event_delivery)}</p>
          <p className="muted">{t('provisioning')}: {summary(queue.data.connector_provisioning)}</p>
          <p className="muted">{t('payloadsOmitted')}</p>
        </>}
      </article>
    </div>
  </section>;
}

/** Render one quality label and its already-formatted value, including explicit unknown states. */
function Metric({ label, value }: { label: string; value: string | number }) {
  return <div className="rounded-md border border-border p-3"><dt className="muted text-sm">{label}</dt><dd className="mt-1 text-xl font-semibold">{value}</dd></div>;
}

/** Keeps queue summaries content-free and deterministic for small state maps. */
function summary(values: Record<string, number>) {
  const entries = Object.entries(values);
  return entries.length ? entries.map(([state, count]) => `${state}: ${count}`).join(' · ') : '0';
}
