'use client';

import { useQuery } from '@tanstack/react-query';
import { ExternalLink, RotateCw } from 'lucide-react';
import Link from 'next/link';
import { useTranslations } from 'next-intl';
import { Button } from '@/components/ui/button';
import { formatDateTime } from '@/core/i18n';
import { useDisplayPreferences } from '@/core/query-provider';
import { getGitHubSummary } from '@/modules/sources/api';
import { listTimeline } from '@/modules/timeline/api';
import type { GadgetInstance } from '../api';

/** Returns the provider URL only when it is a plain github.com https link. */
function safeGithubUrl(value: unknown): string | null {
  return typeof value === 'string' && value.startsWith('https://github.com/') ? value : null;
}

/**
 * Project gadget for one configured GitHub source: collected record counts plus recent mapped
 * issue, pull request, commit and release events from the Timeline, each linked to its provider
 * record. Data comes only from the owner APIs; nothing is sampled or fabricated. The bounded root
 * scrolls controls and events rather than clipping them in compact cards.
 */
export function GithubProjectGadget({ instance }: { instance: GadgetInstance }) {
  const t = useTranslations('github');
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
      return page.items.slice(0, limit);
    },
    enabled: Boolean(sourceId),
    staleTime: 30_000,
  });

  if (!sourceId) return <p role="status" className="p-3 text-sm text-muted-foreground">{t('gadgetNoSource')}</p>;
  if (summary.error && (summary.error as { status?: number }).status === 404) {
    return <p role="alert" className="p-3 text-sm text-destructive">{t('gadgetNotGithub')}</p>;
  }
  const counts = summary.data?.resource_counts;
  const items = events.data ?? [];
  return (
    <section className="flex h-full min-h-0 flex-col gap-3 overflow-y-auto overflow-x-hidden bg-card p-3 text-card-foreground">
      <header className="flex items-center justify-between gap-2 border-b border-border pb-2">
        <h2 className="text-sm font-semibold">{instance.title || t('gadgetTitle')}</h2>
        <div className="flex items-center gap-1.5">
          <Button
            type="button" variant="ghost" size="icon" aria-label={t('refresh')}
            disabled={events.isFetching}
            onClick={() => { void events.refetch(); void summary.refetch(); }}
          >
            <RotateCw className={events.isFetching ? 'h-3.5 w-3.5 animate-spin' : 'h-3.5 w-3.5'} />
          </Button>
          <Link href="/timeline" className="inline-flex items-center gap-1 text-xs font-semibold text-primary hover:underline">
            {t('fullTimeline')}<ExternalLink className="h-3 w-3" />
          </Link>
        </div>
      </header>
      {counts && (
        <dl className="grid grid-cols-4 gap-2 text-center">
          {([['issues', counts.issues], ['pullRequests', counts.pull_requests], ['commits', counts.commits], ['releases', counts.releases]] as const).map(([key, value]) => (
            <div key={key} className="rounded-md border border-border p-1.5">
              <dt className="text-[10px] text-muted-foreground">{t(key)}</dt>
              <dd className="text-sm font-semibold">{value}</dd>
            </div>
          ))}
        </dl>
      )}
      {summary.data && !summary.data.live_verified && (
        <p className="text-[11px] text-muted-foreground">{t('liveNotVerified')}</p>
      )}
      <h3 className="text-xs font-semibold text-muted-foreground">{t('recentActivity')}</h3>
      {events.isLoading && <p role="status" className="text-xs text-muted-foreground">{t('summaryLoading')}</p>}
      {events.isError && (
        <div role="alert" className="text-xs text-destructive">
          {t('loadError')}{' '}
          <Button type="button" variant="outline" size="sm" onClick={() => { void events.refetch(); }}>{t('retry')}</Button>
        </div>
      )}
      {!events.isLoading && !events.isError && items.length === 0 && (
        <p className="text-xs text-muted-foreground">{t('noActivity')}</p>
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
