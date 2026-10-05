'use client';

import { useMemo, useState } from 'react';
import { useLocale, useTranslations } from 'next-intl';
import { Button } from '@/components/ui/button';
import type { ObservedRun } from './api';

/** Filters the bounded newest-run window and reveals only the public run metadata DTO. */
export function RunTable({ runs, isLoading, isError, retry }: {
  runs: ObservedRun[]; isLoading: boolean; isError: boolean; retry: () => void;
}) {
  const t = useTranslations('observability');
  const locale = useLocale() === 'vi-vi' ? 'vi-VN' : 'en-US';
  const [query, setQuery] = useState('');
  const [failuresOnly, setFailuresOnly] = useState(false);
  const [visibleCount, setVisibleCount] = useState(20);
  const [expanded, setExpanded] = useState<string | null>(null);
  const filtered = useMemo(() => runs.filter((run) => {
    const failed = ['failed', 'error', 'blocked'].includes(run.status);
    return (!failuresOnly || failed) && `${run.id} ${run.status} ${run.error_code ?? ''}`.toLowerCase().includes(query.toLowerCase());
  }), [runs, query, failuresOnly]);
  if (isLoading) return <p className="muted" role="status">{t('loading')}</p>;
  if (isError) return <div role="alert"><p>{t('runsUnavailable')}</p><Button type="button" className="secondary mt-2" onClick={retry}>{t('retry')}</Button></div>;
  const visible = filtered.slice(0, visibleCount);
  return <div className="space-y-3">
    <div className="flex flex-wrap items-center gap-3">
      <label className="flex-1">{t('searchRuns')}<input className="mt-1 w-full rounded-md border border-input bg-background px-3 py-2" value={query} onChange={(event) => { setQuery(event.target.value); setVisibleCount(20); }} /></label>
      <label className="flex items-center gap-2"><input type="checkbox" checked={failuresOnly} onChange={(event) => { setFailuresOnly(event.target.checked); setVisibleCount(20); }} />{t('failuresOnly')}</label>
    </div>
    {visible.length ? <div className="space-y-2">{visible.map((run) => <article key={`${run.kind}:${run.id}`} className="rounded-md border border-border p-3">
      <div className="flex flex-wrap items-start justify-between gap-2"><div><strong>{run.kind}</strong><span className="muted"> · {run.status}</span><p className="font-mono text-xs">{run.id}</p>{run.error_code && <p className="text-sm text-destructive">{t('errorCode')}: {run.error_code}</p>}</div>
        <div className="flex gap-2"><Button type="button" className="secondary" aria-expanded={expanded === run.id} onClick={() => setExpanded(expanded === run.id ? null : run.id)}>{expanded === run.id ? t('hideDetails') : t('details')}</Button>{detailHref(run) && <a className="text-sm underline" href={detailHref(run)!} target="_blank" rel="noreferrer">{t('openRun')}</a>}</div></div>
      {expanded === run.id && <dl className="mt-3 grid gap-2 border-t border-border pt-3 text-sm sm:grid-cols-2"><Info label={t('created')} value={new Date(run.created_at).toLocaleString(locale)} /><Info label={t('updated')} value={new Date(run.updated_at).toLocaleString(locale)} /><Info label={t('duration')} value={run.duration_ms === null ? t('unknown') : `${Math.round(run.duration_ms)} ms`} /><Info label={t('model')} value={run.usage?.model_identity ?? t('unknown')} /><Info label={t('inputTokens')} value={run.usage?.tokens_in ?? t('unknown')} /><Info label={t('outputTokens')} value={run.usage?.tokens_out ?? t('unknown')} /><Info label={t('requestId')} value={run.trace.request_id ?? t('unknown')} /><Info label={t('toolCallId')} value={run.trace.tool_call_id ?? t('unknown')} />{run.trace.ingestion_run_id && <InfoLink label={t('evidenceRunId')} value={run.trace.ingestion_run_id} href={`/api/v1/ingestion/runs/${encodeURIComponent(run.trace.ingestion_run_id)}`} />}{run.trace.agent_run_id && <InfoLink label={t('evidenceRunId')} value={run.trace.agent_run_id} href={`/api/v1/agent-runs/${encodeURIComponent(run.trace.agent_run_id)}`} />}</dl>}
    </article>)}</div> : <p className="muted">{t('noRuns')}</p>}
    {visibleCount < filtered.length && <Button type="button" className="secondary" onClick={() => setVisibleCount((count) => count + 20)}>{t('loadMore', { count: Math.min(20, filtered.length - visibleCount) })}</Button>}
    <p className="muted text-xs">{t('windowNote', { shown: visible.length, total: runs.length })}</p>
  </div>;
}

/** Render a labeled run-detail value; callers turn missing metadata into the localized unknown label. */
function Info({ label, value }: { label: string; value: string | number }) {
  return <div><dt className="muted">{label}</dt><dd className="break-all">{value}</dd></div>;
}

/** Render a run metadata identifier as a separate-tab link without sending referrer data. */
function InfoLink({ label, value, href }: { label: string; value: string; href: string }) {
  return <div><dt className="muted">{label}</dt><dd className="break-all"><a href={href} target="_blank" rel="noreferrer" className="underline">{value}</a></dd></div>;
}

/** The operations endpoint supplies one owner-protected metadata-only detail projection. */
function detailHref(run: ObservedRun): string | null {
  return `/api/v1/system/runs/${run.kind}/${encodeURIComponent(run.id)}`;
}
