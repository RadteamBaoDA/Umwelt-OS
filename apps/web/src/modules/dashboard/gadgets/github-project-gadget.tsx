'use client';

import { useQuery } from '@tanstack/react-query';
import { ExternalLink, RotateCw } from 'lucide-react';
import Link from 'next/link';
import { useState } from 'react';
import { useTranslations } from 'next-intl';
import { Button } from '@/components/ui/button';
import { formatDateTime } from '@/core/i18n';
import { useDisplayPreferences } from '@/core/query-provider';
import { getGitHubSummary } from '@/modules/sources';
import { listTimeline, type TimelineEvent } from '@/modules/timeline';
import { useReportGadgetFreshness } from '../gadget-freshness';
import type { GadgetInstance } from '../api';
import { GadgetNotice, GadgetSkeleton } from './gadget-notice';
import { Sparkline } from './sparkline';
import { formatRelativeTime } from './relative-time';
import { TitleBarSlot } from '@/components/gadget-title-slot';

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

/** Borderless 4-up figures with a weekly spark each (from the loaded events, client-side). */
function GithubCounts({
  counts,
  events,
  now,
}: {
  counts: { issues: number; pull_requests: number; commits: number; releases: number };
  events: TimelineEvent[];
  now: number;
}) {
  const t = useTranslations('github');
  const tDash = useTranslations('dashboard');
  return (
    <dl className="github-counts grid grid-cols-4 gap-3">
      {(
        [
          ['issues', counts.issues, 'github_issue'],
          ['pullRequests', counts.pull_requests, 'github_pull'],
          ['commits', counts.commits, 'github_commit'],
          ['releases', counts.releases, 'github_release'],
        ] as const
      ).map(([key, value, type]) => {
        const series = weeklySeries(
          events.filter((e) => e.type === type).map((e) => e.started_at || e.occurred_date || e.observed_at),
          now,
        );
        return (
          <div key={key} className="min-w-0">
            <dt className="truncate text-[11px] text-muted-foreground">{t(key)}</dt>
            <dd className="text-figure-sm m-0">{value}</dd>
            {series.length > 0 && (
              <div title={tDash('githubActivityPerWeek', { label: t(key) })}>
                <Sparkline data={series} height={20} width={96} line />
              </div>
            )}
          </div>
        );
      })}
    </dl>
  );
}

/** Literal `github` catalog keys per timeline event type; other types show the raw type string. */
const EVENT_TYPE_KEY = {
  github_issue: 'type.githubIssue',
  github_pull: 'type.githubPull',
  github_commit: 'type.githubCommit',
  github_release: 'type.githubRelease',
} as const;

/**
 * Terminal-only short code of an event (demo `.log` level column): the 7-char commit sha when the provider metadata
 * carries one, `PR` for pull requests, otherwise the lowercase kind.
 *
 * @param event Timeline event of the GitHub source.
 * @returns Short code and the tone the skin colours it with.
 */
function eventCode(event: TimelineEvent): { code: string; tone: 'info' | 'ok' | 'warn' } {
  if (event.type === 'github_pull') return { code: 'PR', tone: 'ok' };
  if (event.type === 'github_issue') return { code: 'issue', tone: 'warn' };
  if (event.type === 'github_release') return { code: 'release', tone: 'info' };
  const sha = event.metadata?.sha;
  return { code: typeof sha === 'string' && sha ? sha.slice(0, 7) : 'commit', tone: 'info' };
}

/**
 * One recent activity event: type chip, time, title, provider link and repository entity link. The terminal skin
 * shows the short code and a relative time instead of the chip and full date (both stay in the DOM for assistive tech).
 */
function GithubEventItem({ event }: { event: TimelineEvent }) {
  const t = useTranslations('github');
  const display = useDisplayPreferences();
  const url = safeGithubUrl(event.metadata?.canonical_url);
  const when = event.started_at || event.occurred_date || event.observed_at;
  return (
    <li className="github-event space-y-0.5 border-b border-border pb-2 last:border-b-0">
      <div className="github-event-head flex items-center justify-between gap-2 text-[11px] text-muted-foreground">
        <span className="github-event-code" data-tone={eventCode(event).tone} aria-hidden="true">
          {eventCode(event).code}
        </span>
        <span className="github-event-type rounded bg-muted px-1.5 py-0.5 text-foreground">
          {event.type in EVENT_TYPE_KEY ? t(EVENT_TYPE_KEY[event.type as keyof typeof EVENT_TYPE_KEY]) : event.type}
        </span>
        <time dateTime={when}>{formatDateTime(when, display.locale, display.timezone)}</time>
        <span className="github-event-ago" aria-hidden="true">
          {formatRelativeTime(when, display.locale)}
        </span>
      </div>
      <p className="github-event-title text-xs font-medium leading-snug">{event.title}</p>
      <div className="github-event-links flex flex-wrap gap-2 text-[11px]">
        {url && (
          <a href={url} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-0.5 text-primary hover:underline">
            {t('openOnGithub')}
            <ExternalLink className="h-3 w-3" />
          </a>
        )}
        {event.participants
          .filter((p) => p.role === 'repository')
          .slice(0, 1)
          .map((p) => (
            <Link key={p.entity_id} href={`/knowledge/entities/${p.entity_id}`} className="text-primary hover:underline">
              {t('repositoryEntity')}
            </Link>
          ))}
      </div>
    </li>
  );
}

/**
 * Loads the GitHub summary (record counts) and the mapped Timeline events of the configured source, and reports
 * the newest observation time as freshness (footer carries attribution and freshness instead of a title row).
 * Both queries are disabled without a source; `now` is fixed at mount so weekly buckets do not shift on re-render.
 */
function useGithubProjectData(instance: GadgetInstance) {
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
        date_from: '',
        date_to: '',
        timezone: display.timezone,
        source_id: sourceId,
        entity_id: '',
        precision: 'all',
        type: 'github_',
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
  return { sourceId, limit, summary, events, allEvents, now };
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
  const { sourceId, limit, summary, events, allEvents, now } = useGithubProjectData(instance);

  if (!sourceId)
    return (
      <GadgetNotice
        kind="setup"
        message={t('gadget.noSource')}
        action={{ href: '/settings/sources', label: tDash('gadget.configureAction') }}
      />
    );
  if (summary.error && (summary.error as { status?: number }).status === 404) {
    return <GadgetNotice kind="error" message={t('gadget.notGithub')} />;
  }
  const counts = summary.data?.resource_counts;
  const items = allEvents.slice(0, limit);
  return (
    <section className="flex h-full min-h-0 flex-col gap-3 overflow-y-auto overflow-x-hidden bg-card p-3 text-card-foreground">
      {/* Terminal skin: refresh and the timeline link sit in the pane title bar. */}
      <TitleBarSlot>
        <div className="gadget-toolbar flex items-center justify-end gap-1.5">
          <Button
            type="button"
            variant="ghost"
            size="icon"
            aria-label={t('refresh')}
            className="size-7"
            disabled={events.isFetching}
            onClick={() => {
              void events.refetch();
              void summary.refetch();
            }}
          >
            <RotateCw className={events.isFetching ? 'h-3.5 w-3.5 animate-spin' : 'h-3.5 w-3.5'} />
          </Button>
          <Link href="/timeline" className="inline-flex items-center gap-1 text-xs font-semibold text-primary hover:underline">
            {t('fullTimeline')}
            <ExternalLink className="h-3 w-3" />
          </Link>
        </div>
      </TitleBarSlot>
      {counts && <GithubCounts counts={counts} events={allEvents} now={now} />}
      {summary.data && !summary.data.live_verified && <p className="text-[11px] text-muted-foreground">{t('liveNotVerified')}</p>}
      <h3 className="github-recent text-xs font-medium text-muted-foreground">{t('recentActivity')}</h3>
      {events.isLoading && <GadgetSkeleton variant="list" label={t('summary.loading')} className="px-0" />}
      {events.isError && (
        <div role="alert" className="text-xs text-destructive">
          {t('loadError')}{' '}
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={() => {
              void events.refetch();
            }}
          >
            {t('retry')}
          </Button>
        </div>
      )}
      {!events.isLoading && !events.isError && items.length === 0 && (
        <GadgetNotice
          kind="empty"
          message={t('noActivity')}
          action={{ href: '/settings/sources', label: tDash('gadget.configureAction') }}
          className="my-auto px-0 py-1 text-xs"
        />
      )}
      {items.length > 0 && (
        <ol className="github-events min-h-0 flex-1 space-y-2 overflow-y-auto">
          {items.map((event) => (
            <GithubEventItem key={event.id} event={event} />
          ))}
        </ol>
      )}
    </section>
  );
}
