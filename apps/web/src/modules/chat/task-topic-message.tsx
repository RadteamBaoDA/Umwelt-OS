'use client';

import { ExternalLink } from 'lucide-react';
import Link from 'next/link';
import { useTranslations } from 'next-intl';
import { formatDateTime } from '@/core/i18n';
import { useDisplayPreferences } from '@/core/query-provider';
import type { ChatMessage } from './api';

/** One collected item after defensive parsing; every string is untrusted and rendered as text only. */
interface TopicItem {
  title: string;
  url: string | null;
  site: string | null;
  summary: string | null;
  documentId: string | null;
}

/** Literal catalog keys of the partial-run reasons the collector can report; any other reason shows no text. */
const REASON_KEYS = {
  budget: 'taskThread.reason.budget',
  search_quota: 'taskThread.reason.search_quota',
  deadline: 'taskThread.reason.deadline',
} as const;

/** Read a non-negative integer counter from the untrusted stats object (0 when absent or malformed). */
function count(stats: Record<string, unknown> | undefined, key: string): number {
  const value = stats?.[key];
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 ? Math.floor(value) : 0;
}

/**
 * True for an assistant message posted by a Topic watch scheduled task (`task.task_kind === 'topic_watch'`).
 *
 * @param msg - Chat message to classify.
 */
export function isTaskTopicMessage(msg: ChatMessage): boolean {
  return msg.role === 'assistant' && msg.task?.task_kind === 'topic_watch';
}

/** Return `value` when it is a non-empty string, else null. */
function text(value: unknown): string | null {
  return typeof value === 'string' && value ? value : null;
}

/**
 * Parse the allowlisted `task.items` payload without trusting it: non-array input yields no items, entries without a
 * string title are dropped, and a URL is kept only when it is http(s) (the server enforces this too).
 *
 * @param raw - The `items` value of the message's `task` metadata.
 */
function parseItems(raw: unknown): TopicItem[] {
  if (!Array.isArray(raw)) return [];
  const items: TopicItem[] = [];
  for (const entry of raw) {
    if (!entry || typeof entry !== 'object') continue;
    const e = entry as Record<string, unknown>;
    const title = text(e.title);
    if (!title) continue;
    const url = text(e.url);
    items.push({
      title,
      url: url && /^https?:\/\//i.test(url) ? url : null,
      site: text(e.site),
      summary: text(e.summary),
      documentId: text(e.document_id),
    });
  }
  return items;
}

/**
 * Deep link to the run log of a post: the run id plus, when known, the owning task id so Settings opens that task and
 * not the daily brief. Returns null without a run id. Both ids are URL-encoded.
 *
 * @param meta - The message's `task` metadata (untrusted).
 */
function runLogHref(meta: Record<string, unknown> | null | undefined): string | null {
  if (typeof meta?.run_id !== 'string') return null;
  const task = typeof meta.automation_id === 'string' ? `&task=${encodeURIComponent(meta.automation_id)}` : '';
  return `/settings/scheduled-tasks?run=${encodeURIComponent(meta.run_id)}${task}`;
}

/**
 * Catalog key of the localized partial-run reason in the stats, or null for an absent or unknown reason.
 *
 * @param stats - The message's `task.stats` (untrusted).
 */
function reasonKeyOf(stats: Record<string, unknown> | undefined): (typeof REASON_KEYS)[keyof typeof REASON_KEYS] | null {
  const reason = text(stats?.reason);
  return reason && Object.hasOwn(REASON_KEYS, reason) ? REASON_KEYS[reason as keyof typeof REASON_KEYS] : null;
}

/**
 * Posted Topic watch message: a run line (time and a link to the run log of this task in Settings), a "Partial" badge
 * with its localized reason when the run stopped early, one muted stats line (searches, pages, kept, tokens), then one row per collected item with its title linked to the stored document
 * (`/knowledge/documents/{id}`) or, without a document id, to the original page, plus the site and the summary.
 * Titles and summaries are plain text nodes (never markup). External links open in a new tab with
 * `rel="noopener noreferrer"`. Read-only; tolerates a malformed payload by showing fewer or no items.
 *
 * @param props - The assistant message carrying `task.run_id`, `coverage`, `stats` and `items`.
 */
export function TaskTopicMessage({ msg }: { msg: ChatMessage }) {
  const t = useTranslations('chat');
  const display = useDisplayPreferences();
  const meta = msg.task as Record<string, unknown> | null | undefined;
  const stats = meta?.stats as Record<string, unknown> | undefined;
  const reasonKey = reasonKeyOf(stats);
  const href = runLogHref(meta);
  const partial = meta?.coverage === 'partial';
  return (
    <div className="space-y-2" data-testid="task-topic-message">
      <p className="text-xs text-muted-foreground">
        {t('taskThread.topicRunLine', { time: formatDateTime(msg.created_at, display.locale, display.timezone) })}
        {href && (
          <>
            {' · '}
            <Link href={href} className="underline">
              {t('taskThread.runLog')}
            </Link>
          </>
        )}
        {partial && <span className="ml-2 rounded border border-border px-1.5 py-0.5">{t('taskThread.partial')}</span>}
        {partial && reasonKey && <span className="ml-1">{t(reasonKey)}</span>}
      </p>
      <p className="text-xs text-muted-foreground">
        {t('taskThread.topicStats', {
          queries: count(stats, 'queries'),
          fetched: count(stats, 'fetched'),
          items: count(stats, 'items'),
          tokens: count(stats, 'tokens'),
        })}
      </p>
      <ul className="space-y-2">
        {parseItems(meta?.items).map((item, index) => (
          <TopicItemRow key={`${item.documentId ?? item.url ?? item.title}-${index}`} item={item} />
        ))}
      </ul>
    </div>
  );
}

/**
 * One collected item: its title link, the site and the summary, all as plain text.
 *
 * @param props - The parsed item.
 */
function TopicItemRow({ item }: { item: TopicItem }) {
  const t = useTranslations('chat');
  return (
    <li className="text-sm">
      <TopicItemTitle item={item} openLabel={t('taskThread.openOriginal')} />
      {item.site && <span className="ml-2 text-xs text-muted-foreground">{item.site}</span>}
      {item.summary && <p className="text-xs text-muted-foreground">{item.summary}</p>}
    </li>
  );
}

/**
 * Item title: an internal link to the stored document when its id is known, else an external link to the original page,
 * else plain text; with both, a small external-link icon also opens the original page.
 *
 * @param props - The parsed item and the accessible label of the external link.
 */
function TopicItemTitle({ item, openLabel }: { item: TopicItem; openLabel: string }) {
  const external = { target: '_blank', rel: 'noopener noreferrer' } as const;
  if (item.documentId) {
    return (
      <>
        <Link href={`/knowledge/documents/${encodeURIComponent(item.documentId)}`} className="font-medium underline">
          {item.title}
        </Link>
        {item.url && (
          <a href={item.url} aria-label={openLabel} className="ml-1 inline-block align-middle text-muted-foreground" {...external}>
            <ExternalLink className="h-3 w-3" aria-hidden="true" />
          </a>
        )}
      </>
    );
  }
  return item.url ? (
    <a href={item.url} className="font-medium underline" {...external}>
      {item.title}
    </a>
  ) : (
    <span className="font-medium">{item.title}</span>
  );
}
