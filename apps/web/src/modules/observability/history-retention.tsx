'use client';

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useLocale, useTranslations } from 'next-intl';
import { useState } from 'react';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { useWorkspaceSession } from '@/core/app-shell/workspace-shell';
import { useDisplayPreferences } from '@/core/query-provider';
import { getHistorySettings, saveHistorySettings, type HistorySettings, type HistorySettingsPatch } from './api';
import { isStaleRevision } from './system-status-model';
import { getDateTimeFormat } from '@/modules/shared';

const CHOICES = [1, 7, 30, 90];
const INTERVALS = [0, 1, 5, 15, 30, 60];

/**
 * Self-saving history settings row (retention days and automatic collection interval). Selecting a value PATCHes only
 * the changed field with the loaded revision; on success shows a quiet "Saved" stamp, on 409 (stale revision)
 * refetches the current values and asks the owner to choose again.
 */
export function HistoryRetention(props: { oldestSampleAt: string | null }) {
  const s0 = useHistoryRetentionController(props);

  const vm: HistoryRetentionVm = s0;
  return <section className="flex flex-wrap items-center gap-x-6 gap-y-2 rounded-lg border border-border p-4" aria-labelledby="history-retention-title">
<HistoryRetentionChoice vm={vm} />
<HistoryIntervalAndStatus vm={vm} />
  </section>;
}

type HistoryRetentionVm = ReturnType<typeof useHistoryRetentionController>;

/** Retention period select. */
function HistoryRetentionChoice({ vm }: { vm: HistoryRetentionVm }) {
  const { t, settings, save, current, options } = vm;
  return (
<>
    <h2 id="history-retention-title" className="m-0 text-base font-semibold">{t('retentionRowTitle')}</h2>
    <Select value={current === undefined ? '' : String(current)} disabled={!settings.data || save.isPending} onValueChange={(value) => save.mutate({ retention_days: Number(value) })}>
      <SelectTrigger className="w-32" aria-label={t('retentionRowTitle')}><SelectValue /></SelectTrigger>
      <SelectContent>{options.map((days) => <SelectItem key={days} value={String(days)}>{t('retentionDays', { days })}</SelectItem>)}</SelectContent>
    </Select>
    <h2 id="history-interval-title" className="m-0 text-base font-semibold">{t('intervalTitle')}</h2>
</>
  );
}

/** Sample interval select, save status and the oldest-sample note. */
function HistoryIntervalAndStatus({ vm }: { vm: HistoryRetentionVm }) {
  const { t, locale, display, settings, save, interval, saved, oldestSampleAt } = vm;
  return (
<>
    <Select value={interval === undefined ? '' : String(interval)} disabled={!settings.data || save.isPending} onValueChange={(value) => save.mutate({ sample_interval_minutes: Number(value) as NonNullable<HistorySettingsPatch['sample_interval_minutes']> })}>
      <SelectTrigger className="w-36" aria-label={t('intervalTitle')}><SelectValue /></SelectTrigger>
      <SelectContent>{INTERVALS.map((m) => <SelectItem key={m} value={String(m)}>{m === 0 ? t('intervalOff') : t('intervalEvery', { minutes: m })}</SelectItem>)}</SelectContent>
    </Select>
    <span className="text-sm text-muted-foreground" role="status">
      {save.isPending ? t('retentionSaving') : save.isSuccess && saved ? t('retentionSaved') : ''}
    </span>
    {save.isError && <p role="alert" className="m-0 text-sm text-destructive">{isStaleRevision(save.error) ? t('retentionStale') : t('retentionFailed')}</p>}
    <p className="m-0 text-sm text-muted-foreground sm:ml-auto">{t('oldestSample', { time: oldestSampleAt ? getDateTimeFormat(locale, { dateStyle: 'medium', timeStyle: 'short', timeZone: display.timezone }).format(new Date(oldestSampleAt)) : '—' })}</p>
</>
  );
}

/** Holds the state, queries and derived values of HistoryRetention. */
function useHistoryRetentionController(props: { oldestSampleAt: string | null }) {
  const { oldestSampleAt } = props;
  const t = useTranslations('observability');
  const locale = useLocale() === 'vi-vi' ? 'vi-VN' : 'en-US';
  const display = useDisplayPreferences();
  const { csrfToken } = useWorkspaceSession();
  const cache = useQueryClient();
  const [saved, setSaved] = useState(false);
  const settings = useQuery({ queryKey: ['system-history-settings'], queryFn: getHistorySettings });
  const save = useMutation({
    mutationFn: (patch: HistorySettingsPatch) => saveHistorySettings(csrfToken, settings.data!.revision, patch),
    onMutate: () => setSaved(false),
    onSuccess: (value: HistorySettings) => { cache.setQueryData(['system-history-settings'], value); setSaved(true); },
    // A 409 means another tab saved first: reload the authoritative value so the next choice carries the new revision.
    onError: (error) => { if (isStaleRevision(error)) void settings.refetch(); },
  });
  const current = settings.data?.retention_days;
  const interval = settings.data?.sample_interval_minutes;
  const options = current !== undefined && !CHOICES.includes(current) ? [...CHOICES, current].sort((a, b) => a - b) : CHOICES;

  return { t, locale, display, cache, settings, save, current, interval, options, saved, setSaved, oldestSampleAt, csrfToken };
}
