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
 * Posted Topic watch message: a run line (time and a link to the run log in Settings), a "Partial" badge when the run
 * stopped early, then one row per collected item with its title linked to the stored document
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
  const runId = typeof meta?.run_id === 'string' ? meta.run_id : null;
  const items = parseItems(meta?.items);
  const reason = text((meta?.stats as Record<string, unknown> | undefined)?.reason);
  const partial = meta?.coverage === 'partial';
  return (
    <div className="space-y-2" data-testid="task-topic-message">
      <p className="text-xs text-muted-foreground">
        {t('taskThread.topicRunLine', { time: formatDateTime(msg.created_at, display.locale, display.timezone) })}
        {runId && (
          <>
            {' · '}
            <Link href={`/settings/scheduled-tasks?run=${encodeURIComponent(runId)}`} className="underline">
              {t('taskThread.runLog')}
            </Link>
          </>
        )}
        {partial && (
          <span
            className="ml-2 rounded border border-line px-1.5 py-0.5"
            title={reason ? t('taskThread.partialReason', { reason }) : undefined}
          >
            {t('taskThread.partial')}
          </span>
        )}
      </p>
      <ul className="space-y-2">
        {items.map((item, index) => (
          <li key={`${item.documentId ?? item.url ?? item.title}-${index}`} className="text-sm">
            <TopicItemTitle item={item} openLabel={t('taskThread.openOriginal')} />
            {item.site && <span className="ml-2 text-xs text-muted-foreground">{item.site}</span>}
            {item.summary && <p className="text-xs text-muted-foreground">{item.summary}</p>}
          </li>
        ))}
      </ul>
    </div>
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
