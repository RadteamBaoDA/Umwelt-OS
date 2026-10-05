'use client';

import { useInfiniteQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslations } from 'next-intl';
import { Button } from '@/components/ui/button';
import { useWorkspaceSession } from '@/core/app-shell/workspace-shell';
import { useDisplayPreferences } from '@/core/query-provider';
import { AppLocaleId, normalizeFormattingLocale } from '@/core/i18n';
import { connectorKeys, getSourceIngestion, retryRun, Source, sourceKeys } from './api';

/** Formats an ISO timestamp with the supplied application locale and time zone. */
function formatDate(value: string, locale: AppLocaleId, timezone: string): string {
  return new Intl.DateTimeFormat(normalizeFormattingLocale(locale), {
    dateStyle: 'medium', timeStyle: 'short', timeZone: timezone,
  }).format(new Date(value));
}

/** Maps a source or run status to the matching translation message key. */
function statusKey(status: string): 'runQueued' | 'runRunning' | 'runSucceeded' | 'runNeedsOcr' | 'runFailed' {
  return status === 'running' ? 'runRunning' : status === 'succeeded' ? 'runSucceeded' : status === 'needs_ocr' ? 'runNeedsOcr' : status === 'failed' ? 'runFailed' : 'runQueued';
}

/** Renders bounded ingestion run history, preserving owner-reported stages, errors, and result counts for the selected source. */
export function SyncHistory({ source }: { source: Source }) {
  const t = useTranslations('sources');
  const display = useDisplayPreferences();
  const { csrfToken } = useWorkspaceSession();
  const queryClient = useQueryClient();
  const history = useInfiniteQuery({
    queryKey: connectorKeys.ingestion(source.id),
    initialPageParam: undefined as string | undefined,
    queryFn: ({ pageParam }) => getSourceIngestion(source.id, pageParam),
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    refetchInterval: (query) => query.state.data?.pages.some((page) => page.current_run) ? 1500 : false,
  });
  const retry = useMutation({
    mutationFn: ({ runId, stageKey }: { runId: string; stageKey: string }) => retryRun(runId, stageKey, csrfToken),
    onSuccess: (receipt) => {
      void queryClient.invalidateQueries({ queryKey: ['ingestion-run', receipt.run_id] });
      void queryClient.invalidateQueries({ queryKey: connectorKeys.ingestion(source.id) });
      void queryClient.invalidateQueries({ queryKey: sourceKeys.detail(source.id) });
    },
  });
  if (history.isPending) return <p className="muted">{t('loading')}</p>;
  if (history.isError) return <p className="error" role="alert">{t('actionFailed')}</p>;
  const pages = history.data.pages;
  const current = pages[0]?.current_run ?? null;
  const runs = pages.flatMap((page) => page.items).filter((run) => run.run_id !== current?.run_id);
  /** Renders one ingestion-run row using its status and whether it is the current run. */
  const renderRun = (run: NonNullable<typeof current>, isCurrent: boolean) => {
    const hasActiveStage = run.stages.some((stage) => ['pending', 'queued', 'running', 'retrying'].includes(stage.status));
    return <article className="source-run" key={run.run_id}>
      <div className="source-run-heading"><strong>{isCurrent ? t('currentRun') : formatDate(run.created_at, display.locale, display.timezone)}</strong><span>{t(statusKey(run.status))}</span></div>
      {run.error_code && <p className="error">{t('runError')}: {run.error_code}</p>}
      {run.stages.map((stage) => <p className="source-stage" key={stage.stage_key}>
        <span>{stage.stage_key === 'normalize' ? t('normalizeStage') : stage.stage_key}</span>
        <span>{t(statusKey(stage.status))}{stage.error_code ? ` · ${stage.error_code}` : ''}</span>
        {stage.result_count !== null && <small>{t(stage.stage_key === 'normalize' || stage.stage_key === 'parse_file' ? 'chunkCount' : 'resultCount')}: {stage.result_count}</small>}
        {stage.stage_key === 'normalize' && <small>{t('normalizedCount')}: {stage.normalized_count} · {t('duplicateCount')}: {stage.duplicate_count} · {t('skippedCount')}: {stage.skipped_count} · {t('failedCount')}: {stage.failed_count} · {t('pendingCount')}: {stage.pending_count}</small>}
        {stage.status === 'failed' && run.status === 'failed' && source.status === 'active' && !hasActiveStage && <Button className="secondary" disabled={retry.isPending} onClick={() => retry.mutate({ runId: run.run_id, stageKey: stage.stage_key })}>{t('retryRun')}</Button>}
      </p>)}
    </article>;
  };
  return <section className="source-history" aria-label={t('recentRuns')}>
    <h3>{t('recentRuns')}</h3>
    {current && renderRun(current, true)}
    {runs.map((run) => renderRun(run, false))}
    {!current && runs.length === 0 && <p className="muted">{t('noRuns')}</p>}
    {['youtube', 'arxiv', 'huggingface', 'github_releases', 'telegram'].includes(source.provider ?? '') && <p className="muted">{t('providerHistoryCaveat')}</p>}
    <p className="muted">{t('embeddedStatus')}</p>
    {history.hasNextPage && <Button className="secondary" disabled={history.isFetchingNextPage} onClick={() => history.fetchNextPage()}>{t('loadMore')}</Button>}
    {(retry.error || history.isFetchNextPageError) && <p className="error" role="alert">{t('actionFailed')}</p>}
  </section>;
}
