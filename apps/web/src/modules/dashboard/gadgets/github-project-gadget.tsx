'use client';

import { useQuery } from '@tanstack/react-query';
import { ExternalLink, RotateCw } from 'lucide-react';
import Link from 'next/link';
import { useState } from 'react';
import { useTranslations } from 'next-intl';
import { Button } from '@/components/ui/button';
import { formatDateTime } from '@/core/i18n';
import { useDisplayPreferences } from '@/core/query-provider';
import { getGitHubSummary } from '@/modules/sources/public-api';
import { listTimeline } from '@/modules/timeline/api';
import { useReportGadgetFreshness } from '../gadget-freshness';
import type { GadgetInstance } from '../api';
import { GadgetNotice, GadgetSkeleton } from './gadget-notice';
import { Sparkline } from './sparkline';

/** Returns the provider URL only when it is a plain github.com https link. */
function safeGithubUrl(value: unknown): string | null {
  return typeof value === 'string' && value.startsWith('https://github.com/') ? value : null;
}

const WEEK_MS = 7 * 86_400_000;

/**
 * Buckets event timestamps into the last eight weekly counts (oldest first), ending at `now`.
 *
 * @param timestamps ISO event times; invalid or older-than-eight-weeks values are ignored.
 * @param now Epoch ms treated as the end of the newest week.
 * @returns Eight chart points, or an empty list when no event falls in the window.
 */
function weeklySeries(timestamps: string[], now: number): { timestamp: number; value: number }[] {
  const counts = new Array<number>(8).fill(0);
  for (const iso of timestamps) {
    const age = now - Date.parse(iso);
    if (!Number.isFinite(age) || age < 0 || age >= 8 * WEEK_MS) continue;
    counts[7 - Math.floor(age / WEEK_MS)] += 1;
  }
  return counts.some((c) => c > 0) ? counts.map((value, i) => ({ timestamp: now - (7 - i) * WEEK_MS, value })) : [];
}

/**
 * Project gadget for one configured GitHub source: collected record counts plus recent mapped
 * issue, pull request, commit and release events from the Timeline, each linked to its provider
 * record. Data comes only from the owner APIs; nothing is sampled or fabricated. The bounded root
 * scrolls controls and events rather than clipping them in compact cards.
 */
export function GithubProjectGadget({ instance }: { instance: GadgetInstance }) {
  const t = useTranslations('github');
  const tDash = useTranslations('dashboard');
  const display = useDisplayPreferences();
  const definition = instance.definition;
  const sourceId = definition.source_ids?.[0] ?? '';
  const limit = definition.filters?.limit ?? 10;

  const summary = useQuery({
    queryKey: ['github-summary', sourceId],
    queryFn: ({ signal }) => getGitHubSummary(sourceId, signal),
    enabled: Boolean(sourceId),
    staleTime: 30_000,
  });
  const events = useQuery({
    queryKey: ['dashboard-github-project', instance.id, sourceId],
    queryFn: async () => {
      const page = await listTimeline({
        date_from: '', date_to: '', timezone: display.timezone, source_id: sourceId,
        entity_id: '', precision: 'all', type: 'github_',
      });
      // Keep the whole page: the weekly sparks bucket all of it, the list shows only `limit`.
      return page.items;
    },
    enabled: Boolean(sourceId),
    staleTime: 30_000,
  });

  // Window end for the weekly sparks, fixed at mount so re-renders do not shift buckets.
  const [now] = useState(() => Date.now());
  const allEvents = events.data ?? [];
  // Footer carries attribution and freshness instead of an inner title row.
  useReportGadgetFreshness(
    allEvents.reduce<string | null>((max, e) => (e.observed_at && (!max || e.observed_at > max) ? e.observed_at : max), null),
    'GitHub',
  );

  if (!sourceId) return <GadgetNotice kind="setup" message={t('gadgetNoSource')} action={{ href: '/settings/sources', label: tDash('gadgetConfigureAction') }} />;
  if (summary.error && (summary.error as { status?: number }).status === 404) {
    return <GadgetNotice kind="error" message={t('gadgetNotGithub')} />;
  }
  const counts = summary.data?.resource_counts;
  const items = allEvents.slice(0, limit);
  return (
    <section className="flex h-full min-h-0 flex-col gap-3 overflow-y-auto overflow-x-hidden bg-card p-3 text-card-foreground">
      <div className="flex items-center justify-end gap-1.5">
        <Button
          type="button" variant="ghost" size="icon" aria-label={t('refresh')} className="size-7"
          disabled={events.isFetching}
          onClick={() => { void events.refetch(); void summary.refetch(); }}
        >
          <RotateCw className={events.isFetching ? 'h-3.5 w-3.5 animate-spin' : 'h-3.5 w-3.5'} />
        </Button>
        <Link href="/timeline" className="inline-flex items-center gap-1 text-xs font-semibold text-primary hover:underline">
          {t('fullTimeline')}<ExternalLink className="h-3 w-3" />
        </Link>
      </div>
      {counts && (
        // Borderless 4-up figures with a weekly spark each (from the loaded events, client-side).
        <dl className="grid grid-cols-4 gap-3">
          {([['issues', counts.issues, 'github_issue'], ['pullRequests', counts.pull_requests, 'github_pull'], ['commits', counts.commits, 'github_commit'], ['releases', counts.releases, 'github_release']] as const).map(([key, value, type]) => {
            const series = weeklySeries(allEvents.filter((e) => e.type === type).map((e) => e.started_at || e.occurred_date || e.observed_at), now);
            return (
              <div key={key} className="min-w-0">
                <dt className="truncate text-[11px] text-muted-foreground">{t(key)}</dt>
                <dd className="text-figure-sm m-0">{value}</dd>
                {series.length > 0 && <div title={tDash('githubActivityPerWeek', { label: t(key) })}><Sparkline data={series} height={20} width={96} line /></div>}
              </div>
            );
          })}
        </dl>
      )}
      {summary.data && !summary.data.live_verified && (
        <p className="text-[11px] text-muted-foreground">{t('liveNotVerified')}</p>
      )}
      <h3 className="text-xs font-medium text-muted-foreground">{t('recentActivity')}</h3>
      {events.isLoading && <GadgetSkeleton variant="list" label={t('summaryLoading')} className="px-0" />}
      {events.isError && (
        <div role="alert" className="text-xs text-destructive">
          {t('loadError')}{' '}
          <Button type="button" variant="outline" size="sm" onClick={() => { void events.refetch(); }}>{t('retry')}</Button>
        </div>
      )}
      {!events.isLoading && !events.isError && items.length === 0 && (
        <GadgetNotice kind="empty" message={t('noActivity')} action={{ href: '/settings/sources', label: tDash('gadgetConfigureAction') }} className="my-auto px-0 py-1 text-xs" />
      )}
      {items.length > 0 && (
        <ol className="min-h-0 flex-1 space-y-2 overflow-y-auto">
          {items.map((event) => {
            const url = safeGithubUrl(event.metadata?.canonical_url);
            const when = event.started_at || event.occurred_date || event.observed_at;
            return (
              <li key={event.id} className="space-y-0.5 border-b border-border pb-2 last:border-b-0">
                <div className="flex items-center justify-between gap-2 text-[11px] text-muted-foreground">
                  <span className="rounded bg-muted px-1.5 py-0.5 text-foreground">
                    {t.has(`type_${event.type}`) ? t(`type_${event.type}`) : event.type}
                  </span>
                  <time dateTime={when}>{formatDateTime(when, display.locale, display.timezone)}</time>
                </div>
                <p className="text-xs font-medium leading-snug">{event.title}</p>
                <div className="flex flex-wrap gap-2 text-[11px]">
                  {url && (
                    <a href={url} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-0.5 text-primary hover:underline">
                      {t('openOnGithub')}<ExternalLink className="h-3 w-3" />
                    </a>
                  )}
                  {event.participants.filter((p) => p.role === 'repository').slice(0, 1).map((p) => (
                    <Link key={p.entity_id} href={`/knowledge/entities/${p.entity_id}`} className="text-primary hover:underline">
                      {t('repositoryEntity')}
                    </Link>
                  ))}
                </div>
              </li>
            );
          })}
        </ol>
      )}
    </section>
  );
}
