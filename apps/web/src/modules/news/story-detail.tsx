'use client';

import { useInfiniteQuery, useQueryClient } from '@tanstack/react-query';
import { ArrowLeft, CopyIcon, ExternalLink } from 'lucide-react';
import { useTranslations } from 'next-intl';
import { useState } from 'react';
import { Button } from '@/components/ui/button';
import { formatDateTime } from '@/core/i18n';
import { safeHttpUrl } from '@/core/safe-url';
import { useDisplayPreferences } from '@/core/query-provider';
import { fetchStory } from './story-api';
import { TranslatedBadge } from '@/modules/translations/translated-badge';
import { TranslationNote } from '@/modules/translations/translation-note';
import { useContentTranslation } from '@/modules/translations/use-content-translation';
import { useNewsIncompleteLabel } from './use-news-incomplete-label';

/** Props for the detail renderer shown when a story is opened from its gadget. */
export type StoryDetailProps = {
  storyId: string;
  sourceIds: string[];
  onBack: () => void;
};

const badgeClass = 'inline-flex h-6 items-center rounded-full border border-border bg-background px-2 text-xs font-semibold text-muted-foreground';
const signalKeys = ['topic', 'entity', 'goal', 'recency', 'importance', 'novelty'] as const;
type SignalKey = (typeof signalKeys)[number];

/**
 * Render current excerpts and citations from one protected detail snapshot.
 * Ordinary title-free support pages retain their cursor; an authority-change
 * omission suppresses every cached page until the owner explicitly restarts.
 */
export function StoryDetail({ storyId, sourceIds, onBack }: StoryDetailProps) {
  const t = useTranslations('news');
  const d = useTranslations('detail');
  const display = useDisplayPreferences();
  const incompleteLabel = useNewsIncompleteLabel();
  const queryClient = useQueryClient();
  const [snapshotGeneration, setSnapshotGeneration] = useState(0);
  const [copied, setCopied] = useState<'ok' | 'failed' | null>(null);
  const detail = useInfiniteQuery({
    queryKey: ['news-story', storyId, sourceIds, snapshotGeneration],
    queryFn: ({ pageParam }) => fetchStory(storyId, sourceIds, pageParam),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (page) => page.evidence_cursor ?? undefined,
    enabled: Boolean(storyId),
    refetchOnMount: false,
    refetchOnReconnect: false,
    refetchOnWindowFocus: false,
    retry: false,
  });
  /**
   * Drop every page from the prior cursor chain and start a fresh fixed-as-of read.
   * This is only invoked by an explicit retry/restart action, never automatically.
   */
  function restartSnapshot(): void {
    queryClient.removeQueries({ queryKey: ['news-story', storyId, sourceIds] });
    setSnapshotGeneration((generation) => generation + 1);
  }
  const pages = detail.data?.pages ?? [];
  const authorityChanged = pages.some((page) => page.incomplete_reasons.includes('evidence_changed_during_read'));
  const contentAvailable = !detail.isError && !authorityChanged;
  // A later authority failure revokes the whole cursor snapshot, including earlier cached pages.
  const visiblePages = contentAvailable ? pages : [];
  const story = visiblePages.find((page) => page.story !== null)?.story ?? undefined;
  const targets = story?.translation_revision ? [{ id: story.id, revision: story.translation_revision }] : [];
  const translation = useContentTranslation('news_story', targets);
  const [showOriginal, setShowOriginal] = useState(false);
  const translated = showOriginal || !story ? undefined : translation.results.get(story.id)?.translation ?? undefined;
  const evidence = visiblePages.flatMap((page) => page.story?.evidence ?? []);
  const omissionReasons = [...new Set(visiblePages.flatMap((page) => page.incomplete_reasons))];
  const firstUrl = evidence.map((item) => safeHttpUrl(item.url)).find(Boolean) ?? null;
  const reasons = story?.relevance_state === 'unavailable' ? [] : (story?.why_relevant ?? []).filter((name): name is SignalKey => (signalKeys as readonly string[]).includes(name));

  /** Copies a plain-text citation (title, source, time, link) for the first evidence rows. */
  async function copyCitation(): Promise<void> {
    if (!story) return;
    const lines = evidence.slice(0, 5).map((item) => `${item.title} — ${item.source_name}, ${formatDateTime(item.observed_at, display.locale, display.timezone)}${item.url ? ` ${item.url}` : ''}`);
    try {
      await navigator.clipboard.writeText([story.title, ...lines].join('\n'));
      setCopied('ok');
    } catch {
      setCopied('failed');
    }
  }

  return (
    <section className="flex h-full min-h-0 flex-col gap-4 overflow-auto rounded-lg border border-border bg-background p-4 text-foreground">
      <Button variant="ghost" className="w-fit" onClick={onBack}>
        <ArrowLeft aria-hidden="true" className="mr-2 size-4" />{t('back')}
      </Button>
      {detail.isPending ? <p role="status" className="text-sm text-muted-foreground">{t('loading')}</p> : null}
      {detail.isError || authorityChanged ? (
        <div role="alert" className="flex items-center justify-between gap-2 text-sm text-destructive">
          <span>{authorityChanged ? t('evidenceChanged') : t('detailUnavailable')}</span>
          <Button variant="outline" size="sm" onClick={restartSnapshot}>{t('restartSnapshot')}</Button>
        </div>
      ) : null}
      {story ? (
        <article className="space-y-3" aria-labelledby="story-detail-title">
          <div className="flex flex-wrap items-center gap-2">
            <span className={badgeClass}>{d('evidenceCount', { count: story.evidence_count })}</span>
            <span className={badgeClass}>{d('sourceCount', { count: story.source_count })}</span>
          </div>
          <div className="flex items-start gap-2">
            <h2 id="story-detail-title" className="flex-1 text-xl font-semibold leading-snug">{translated?.title ?? story.title}</h2>
            {firstUrl ? <a href={firstUrl} target="_blank" rel="noopener noreferrer" aria-label={d('openOriginal')} title={d('openOriginal')} className="inline-flex size-11 items-center justify-center rounded-[9px] text-muted-foreground hover:bg-secondary hover:text-foreground"><ExternalLink aria-hidden="true" className="size-4" /></a> : null}
            <Button variant="ghost" size="icon" aria-label={d('copyCitation')} title={d('copyCitation')} onClick={() => { void copyCitation(); }}><CopyIcon aria-hidden="true" className="size-4" /></Button>
          </div>
          <p className="text-xs text-muted-foreground">
            {t('observed', { date: formatDateTime(story.observed_at, display.locale, display.timezone) })} · {display.timezone}
          </p>
          {translation.results.has(story.id) ? <TranslatedBadge showOriginal={showOriginal} onToggle={() => setShowOriginal((v) => !v)} /> : <TranslationNote issue={translation.issues.get(story.id)} />}
          <p role="status" className="text-xs text-muted-foreground">{copied === 'ok' ? d('copied') : copied === 'failed' ? d('copyFailed') : ''}</p>
          <h3 className="text-sm font-semibold">{t('excerpt')}</h3>
          <blockquote className="border-l-2 border-border pl-3 text-sm leading-relaxed">{translated?.excerpt ?? story.excerpt}</blockquote>
          {story.incomplete_reasons.length ? (
            <div role="status" className="space-y-1 text-xs text-muted-foreground">
              <p>{t('partialData')}</p>
              {story.incomplete_reasons.map((reason) => <p key={reason}>{incompleteLabel(reason)}</p>)}
            </div>
          ) : null}
          {story.relevance_signals.project?.available === false ? <p className="text-xs text-muted-foreground">{t('unavailable')}</p> : null}
          {reasons.length ? (
            <div className="space-y-1">
              <h3 className="text-sm font-semibold">{d('whySeeThis')}</h3>
              <ul className="flex flex-wrap gap-2">{reasons.map((name) => <li key={name} className={badgeClass}>{d(`signal_${name}`)}</li>)}</ul>
            </div>
          ) : null}
        </article>
      ) : null}
      {contentAvailable && omissionReasons.length ? (
        <div role="status" className="space-y-1 text-xs text-muted-foreground">
          <p>{t('partialData')}</p>
          {omissionReasons.map((reason) => <p key={reason}>{incompleteLabel(reason)}</p>)}
        </div>
      ) : null}
      <section className="space-y-3" aria-label={t('evidence', { count: story?.evidence_count ?? 0 })}>
        <h3 className="text-sm font-semibold">{d('sourcesAndEvidence')}</h3>
        {evidence.map((item) => (
          <article key={`${item.source_id}:${item.document_version_id}:${item.chunk_id}`} className="rounded-md border border-border p-3">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <h4 className="text-sm font-medium">{item.title}</h4>
              {safeHttpUrl(item.url) ? (
                <a href={safeHttpUrl(item.url)} target="_blank" rel="noopener noreferrer" className="inline-flex min-h-11 items-center gap-1 text-xs text-primary underline-offset-4 hover:underline">
                  {t('openSource')}<ExternalLink aria-hidden="true" className="size-3" />
                </a>
              ) : null}
            </div>
            <p className="mt-1 text-xs text-muted-foreground">{item.source_name} · {formatDateTime(item.observed_at, display.locale, display.timezone)}</p>
            <blockquote className="mt-2 border-l-2 border-border pl-3 text-sm">{item.excerpt}</blockquote>
          </article>
        ))}
        {contentAvailable && story ? <p className="text-xs text-muted-foreground">{d('independenceUnknown')}</p> : null}
      </section>
      {contentAvailable && detail.hasNextPage ? (
        <Button variant="outline" onClick={() => { void detail.fetchNextPage(); }} disabled={detail.isFetchingNextPage}>
          {detail.isFetchingNextPage ? t('loading') : t('loadMore')}
        </Button>
      ) : null}
    </section>
  );
}
