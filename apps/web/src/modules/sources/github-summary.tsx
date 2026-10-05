'use client';

import { useQuery } from '@tanstack/react-query';
import { useTranslations } from 'next-intl';
import { formatDateTime } from '@/core/i18n';
import { useDisplayPreferences } from '@/core/query-provider';
import { getGitHubSummary } from './api';

/**
 * Shows collected GitHub record counts for one saved source in Settings / Data sources.
 *
 * Counts come from the owner-only summary route (mapped canonical records); the
 * "not verified live" notice is shown whenever the API does not claim live verification.
 */
export function GitHubSummary({ sourceId }: { sourceId: string }) {
  const t = useTranslations('github');
  const display = useDisplayPreferences();
  const query = useQuery({
    queryKey: ['github-summary', sourceId],
    queryFn: ({ signal }) => getGitHubSummary(sourceId, signal),
    refetchInterval: 15_000,
  });
  const data = query.data;
  const counts = data?.resource_counts;
  const rows: [string, number][] = counts
    ? [
        [t('repositories'), counts.repositories],
        [t('issues'), counts.issues],
        [t('pullRequests'), counts.pull_requests],
        [t('commits'), counts.commits],
        [t('releases'), counts.releases],
      ]
    : [];
  return (
    <section aria-label={t('summaryTitle')} className="space-y-2">
      <h4 className="text-sm font-semibold text-foreground">{t('summaryTitle')}</h4>
      {query.isLoading && <p role="status" className="text-xs text-muted-foreground">{t('summaryLoading')}</p>}
      {query.isError && <p role="alert" className="text-xs text-destructive">{t('summaryError')}</p>}
      {data && rows.every(([, value]) => value === 0) && (
        <p className="text-xs text-muted-foreground">{t('summaryEmpty')}</p>
      )}
      {data && rows.some(([, value]) => value > 0) && (
        <dl className="grid grid-cols-2 gap-2 sm:grid-cols-5">
          {rows.map(([label, value]) => (
            <div key={label} className="rounded-md border border-border bg-card p-2">
              <dt className="text-[11px] text-muted-foreground">{label}</dt>
              <dd className="text-base font-semibold text-foreground">{value}</dd>
            </div>
          ))}
        </dl>
      )}
      {data?.last_event_at && (
        <p className="text-xs text-muted-foreground">
          {t('lastRecord')}: {formatDateTime(data.last_event_at, display.locale, display.timezone)}
        </p>
      )}
      {data && !data.live_verified && <p className="text-xs text-muted-foreground">{t('liveNotVerified')}</p>}
    </section>
  );
}
