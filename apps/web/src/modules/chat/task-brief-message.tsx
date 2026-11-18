'use client';

import Link from 'next/link';
import { useTranslations } from 'next-intl';
import { formatDateTime } from '@/core/i18n';
import { useDisplayPreferences } from '@/core/query-provider';
import { BriefText, briefCitationHref } from '@/modules/dashboard/public-api';
import type { ChatMessage } from './api';

/**
 * True for a conversation that is a scheduled-task thread: an automation thread whose metadata carries `scheduled_task`.
 *
 * @param conv - Conversation fields the check reads (list or detail projection).
 */
export function scheduledTaskThread(conv: { context_kind?: string | null; metadata?: Record<string, unknown> | null }): boolean {
  return conv.context_kind === 'automation' && Boolean(conv.metadata?.scheduled_task);
}

/**
 * True for an assistant message posted by the daily-brief scheduled task (`task.task_kind === 'daily_brief'`).
 *
 * @param msg - Chat message to classify.
 */
export function isTaskBriefMessage(msg: ChatMessage): boolean {
  return msg.role === 'assistant' && msg.task?.task_kind === 'daily_brief';
}

/**
 * Posted daily-brief message: a run line (generated time and a link to the run log in Settings), the brief text rendered
 * by the dashboard's `BriefText` (so translation of the saved revision works as on the gadget) and the brief's own
 * citations from `task.brief_citations`, each linked by `briefCitationHref` (story, event or Sources page) or plain text. Read-only; no side effects beyond the translation lookup in BriefText.
 *
 * @param props - The assistant message carrying the brief metadata (`task.run_id`, `brief_id`, `brief_revision`, `brief_citations`).
 */
export function TaskBriefMessage({ msg }: { msg: ChatMessage }) {
  const t = useTranslations('chat');
  const display = useDisplayPreferences();
  const meta = msg.task;
  // Defensive: tolerate a malformed payload (non-array, items missing ref/title) instead of crashing the transcript.
  const rawCitations: unknown = meta?.brief_citations;
  const citations = (Array.isArray(rawCitations) ? rawCitations : [])
    .filter((c) => Boolean(c) && typeof c.ref === 'number' && typeof c.title === 'string')
    .map((c) => ({
      ref: c.ref as number,
      kind: String(c.kind ?? ''),
      id: String(c.id ?? ''),
      title: c.title as string,
      source_ids: Array.isArray(c.source_ids) ? (c.source_ids as string[]) : [],
    }));
  const runId = typeof meta?.run_id === 'string' ? meta.run_id : null;
  // BriefText takes a saved revision; the posted message carries only the fields it reads (id, revision, content).
  const brief = {
    id: String(meta?.brief_id ?? msg.id),
    brief_date: '',
    timezone: display.timezone,
    revision: Number(meta?.brief_revision ?? 0),
    status: 'current' as const,
    content: msg.content,
    citations,
    model_alias: msg.model_identity ?? '',
    generated_at: msg.created_at,
  };
  return (
    <div className="space-y-2" data-testid="task-brief-message">
      <p className="text-xs text-muted-foreground">
        {t('taskThread.runLine', { time: formatDateTime(msg.created_at, display.locale, display.timezone) })}
        {runId && (
          <>
            {' · '}
            <Link href={`/settings/scheduled-tasks?run=${encodeURIComponent(runId)}`} className="underline">
              {t('taskThread.runLog')}
            </Link>
          </>
        )}
      </p>
      <BriefText brief={brief} />
      {citations.length > 0 && (
        <ol className="space-y-0.5 text-xs text-muted-foreground" aria-label={t('taskThread.citations')}>
          {citations.map((citation) => {
            const href = briefCitationHref(citation.kind, citation.id);
            return (
              <li key={citation.ref}>
                [{citation.ref}]{' '}
                {href ? (
                  <Link href={href} className="underline">
                    {citation.title}
                  </Link>
                ) : (
                  citation.title
                )}
              </li>
            );
          })}
        </ol>
      )}
    </div>
  );
}
