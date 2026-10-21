'use client';

import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import { useTranslations } from 'next-intl';
import { Button } from '@/components/ui/button';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import { useWorkspaceSession } from '@/core/app-shell/workspace-shell';
import { useDisplayPreferences } from '@/core/query-provider';
import { AppLocaleId, normalizeFormattingLocale } from '@/core/i18n';
import { isStale } from './freshness';
import { connectorKeys, getSourceIngestion, IngestionRun, IngestionRunStatus, retryRun, Source, sourceKeys } from './api';

/** Formats an ISO timestamp with the supplied application locale and time zone. */
function formatDate(value: string, locale: AppLocaleId, timezone: string): string {
  return new Intl.DateTimeFormat(normalizeFormattingLocale(locale), {
    dateStyle: 'medium', timeStyle: 'short', timeZone: timezone,
  }).format(new Date(value));
}

/** Maps run and stage states to their translated message keys. */
function statusKey(status: string): 'runQueued' | 'runRunning' | 'runSucceeded' | 'runNeedsOcr' | 'runFailed' {
  return status === 'running' ? 'runRunning' : status === 'succeeded' ? 'runSucceeded' : status === 'needs_ocr' ? 'runNeedsOcr' : status === 'failed' ? 'runFailed' : 'runQueued';
}

/** Shows source freshness and a filterable, cursor-paginated run table; retry reuses the ingestion API and refreshes source history. */
export function SyncHistory({ source, intervalMinutes }: { source: Source; intervalMinutes?: number }) {
  const t = useTranslations('sources');
  const display = useDisplayPreferences();
  const { csrfToken } = useWorkspaceSession();
  const queryClient = useQueryClient();
  const [status, setStatus] = useState<'all' | IngestionRunStatus>('all');
  const [cursors, setCursors] = useState<(string | undefined)[]>([undefined]);
  const pageIndex = cursors.length - 1;
  const history = useQuery({
    queryKey: [...connectorKeys.ingestion(source.id), status, cursors[pageIndex]],
    queryFn: ({ signal }) => getSourceIngestion(source.id, cursors[pageIndex], status === 'all' ? undefined : status, signal),
    refetchInterval: (query) => query.state.data?.current_run ? 1500 : false,
  });
  const retry = useMutation({
    mutationFn: ({ runId, stageKey }: { runId: string; stageKey: string }) => retryRun(runId, stageKey, csrfToken),
    onSuccess: (receipt) => {
      void queryClient.invalidateQueries({ queryKey: ['ingestion-run', receipt.run_id] });
      void queryClient.invalidateQueries({ queryKey: connectorKeys.ingestion(source.id) });
      void queryClient.invalidateQueries({ queryKey: sourceKeys.detail(source.id) });
    },
  });

  /** Clears cursor history when a filter changes so the new result set starts at page one. */
  const changeStatus = (value: string) => {
    setStatus(value as 'all' | IngestionRunStatus);
    setCursors([undefined]);
  };
  /** Appends the server-provided opaque cursor and exposes a matching previous-page path. */
  const nextPage = () => {
    const cursor = history.data?.next_cursor;
    if (cursor) setCursors((current) => [...current, cursor]);
  };

  const current = history.data?.current_run;
  const runs = history.data?.items ?? [];
  const displayed = [
    ...(pageIndex === 0 && current && (status === 'all' || current.status === status) ? [current] : []),
    ...runs.filter((run) => run.run_id !== current?.run_id),
  ];
  return <section className="grid gap-4" aria-label={t('recentRuns')}>
    <div className="grid gap-1 text-sm sm:grid-cols-2">
      <p className="m-0 text-muted-foreground">{t('lastSuccess')}: {source.last_success_at ? formatDate(source.last_success_at, display.locale, display.timezone) : t('never')}</p>
      {source.collected_at && <p className="m-0 text-muted-foreground">{t('collectedAt')}: {formatDate(source.collected_at, display.locale, display.timezone)}</p>}
      {source.indexed_at && <p className="m-0 text-muted-foreground">{t('indexedAt')}: {formatDate(source.indexed_at, display.locale, display.timezone)}</p>}
      {source.next_due_at && <p className="m-0 text-muted-foreground">{t('nextDueAt')}: {formatDate(source.next_due_at, display.locale, display.timezone)}</p>}
      {source.retry_at && <p className="m-0 text-muted-foreground">{t('retryAt')}: {formatDate(source.retry_at, display.locale, display.timezone)}</p>}
      {isStale(source.last_success_at, intervalMinutes) && <p className="m-0 text-muted-foreground" role="status">{t('stale')}</p>}
      {source.collection_error_code && <p className="m-0 text-destructive" role="alert">{t('collectionError')}: {source.collection_error_code}</p>}
    </div>
    <label className="flex items-center gap-2 text-sm">
      <span>{t('runFilterStatus')}</span>
      <Select value={status} onValueChange={changeStatus}>
        <SelectTrigger className="w-52"><SelectValue /></SelectTrigger>
        <SelectContent>
          <SelectItem value="all">{t('runFilterAll')}</SelectItem>
          {(['queued', 'running', 'succeeded', 'needs_ocr', 'failed'] as const).map((value) => <SelectItem key={value} value={value}>{t(statusKey(value))}</SelectItem>)}
        </SelectContent>
      </Select>
    </label>
    {history.isPending && <p className="text-muted-foreground" role="status">{t('loading')}</p>}
    {history.isError && <p className="error" role="alert">{t('actionFailed')}</p>}
    {history.data && <>
      <Table>
        <TableHeader><TableRow>
          <TableHead>{t('runColumnId')}</TableHead>
          <TableHead>{t('runColumnStarted')}</TableHead>
          <TableHead>{t('runColumnStatus')}</TableHead>
          <TableHead>{t('runColumnStages')}</TableHead>
          <TableHead>{t('runColumnError')}</TableHead>
        </TableRow></TableHeader>
        <TableBody>
          {displayed.map((run) => <RunRow key={run.run_id} run={run} source={source} t={t} locale={display.locale} timezone={display.timezone} retryPending={retry.isPending} onRetry={(stageKey) => retry.mutate({ runId: run.run_id, stageKey })} />)}
          {displayed.length === 0 && <TableRow><TableCell colSpan={5} className="text-muted-foreground">{t('runNoMatches')}</TableCell></TableRow>}
        </TableBody>
      </Table>
      <div className="flex items-center justify-between gap-3">
        <span className="text-sm text-muted-foreground">{t('runPage', { page: pageIndex + 1 })}</span>
        <div className="flex gap-2">
          <Button type="button" size="sm" variant="outline" disabled={pageIndex === 0 || history.isFetching} onClick={() => setCursors((current) => current.slice(0, -1))}>{t('runPreviousPage')}</Button>
          <Button type="button" size="sm" variant="outline" disabled={!history.data.next_cursor || history.isFetching} onClick={nextPage}>{t('runNextPage')}</Button>
        </div>
      </div>
    </>}
    {['youtube', 'arxiv', 'huggingface', 'github_releases', 'telegram'].includes(source.provider ?? '') && <p className="text-sm text-muted-foreground">{t('providerHistoryCaveat')}</p>}
    <p className="text-sm text-muted-foreground">{t('embeddedStatus')}</p>
    {retry.error && <p className="error" role="alert">{t('actionFailed')}</p>}
  </section>;
}

/** Renders one run and its stage-level counts/errors, with retry only when a failed stage belongs to an active source. */
function RunRow({ run, source, t, locale, timezone, retryPending, onRetry }: {
  run: IngestionRun;
  source: Source;
  t: ReturnType<typeof useTranslations<'sources'>>;
  locale: AppLocaleId;
  timezone: string;
  retryPending: boolean;
  onRetry: (stageKey: string) => void;
}) {
  const hasActiveStage = run.stages.some((stage) => ['pending', 'queued', 'running', 'retrying'].includes(stage.status));
  return <TableRow>
    <TableCell className="font-mono text-xs">{run.run_id}</TableCell>
    <TableCell>{formatDate(run.created_at, locale, timezone)}</TableCell>
    <TableCell>{t(statusKey(run.status))}</TableCell>
    <TableCell className="min-w-64 whitespace-normal">
      <div className="grid gap-2">{run.stages.map((stage) => <details key={stage.stage_key} className="rounded border border-border px-2 py-1">
        <summary className="cursor-pointer">{stage.stage_key === 'normalize' ? t('normalizeStage') : stage.stage_key}: {t(statusKey(stage.status))}</summary>
        <div className="grid gap-1 pt-2 text-xs text-muted-foreground">
          <span>{t('runAttempts', { count: stage.attempts })}{stage.error_code ? ` · ${stage.error_code}` : ''}</span>
          {stage.result_count !== null && <span>{t(stage.stage_key === 'normalize' || stage.stage_key === 'parse_file' ? 'chunkCount' : 'resultCount')}: {stage.result_count}</span>}
          {stage.stage_key === 'normalize' && <span>{t('normalizedCount')}: {stage.normalized_count} · {t('selectedCurrentCount')}: {stage.selected_current_count} · {t('duplicateCount')}: {stage.duplicate_count} · {t('skippedCount')}: {stage.skipped_count} · {t('failedCount')}: {stage.failed_count} · {t('pendingCount')}: {stage.pending_count}</span>}
          {stage.status === 'failed' && run.status === 'failed' && source.status === 'active' && !hasActiveStage && <Button type="button" size="sm" variant="outline" disabled={retryPending} onClick={() => onRetry(stage.stage_key)}>{t('runRetryStage', { stage: stage.stage_key })}</Button>}
        </div>
      </details>)}</div>
    </TableCell>
    <TableCell className="whitespace-normal">{run.error_code ? `${t('runError')}: ${run.error_code}` : '—'}</TableCell>
  </TableRow>;
}
