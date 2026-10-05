'use client';

import Link from 'next/link';
import { useTranslations } from 'next-intl';
import { useDisplayPreferences } from '@/core/query-provider';
import { normalizeFormattingLocale } from '@/core/i18n';
import type { GoalStatus } from '@/modules/goals/types';
import type { TaskStatus } from '@/modules/tasks/types';
import type { GoalSearchHit, SearchHit, TaskSearchHit } from './api';

/** Formats date-only values in UTC while formatting instants in the owner's preferred time zone. */
function formatSearchDate(value: string | null, locale: string, timezone: string, dateOnly = false) {
  if (!value) return null;
  const dateOnlyMatch = dateOnly ? /^(\d{4})-(\d{2})-(\d{2})$/.exec(value) : null;
  const date = dateOnlyMatch
    ? new Date(Date.UTC(Number(dateOnlyMatch[1]), Number(dateOnlyMatch[2]) - 1, Number(dateOnlyMatch[3])))
    : new Date(value);
  if (!Number.isFinite(date.getTime())) return value;
  return new Intl.DateTimeFormat(locale, dateOnlyMatch
    ? { dateStyle: 'medium', timeZone: 'UTC' }
    : { dateStyle: 'medium', timeStyle: 'short', timeZone: timezone }).format(date);
}

/** Formats a document hit using its preferred publication/observation instant and localized fallback. */
function displayDate(hit: SearchHit, locale: string, timezone: string, unknownLabel: string) {
  return formatSearchDate(hit.published_at ?? hit.observed_at, locale, timezone) ?? unknownLabel;
}

const taskStatusKeys: Record<TaskStatus, string> = {
  inbox: 'taskStatusInbox', todo: 'taskStatusTodo', in_progress: 'taskStatusInProgress',
  blocked: 'taskStatusBlocked', done: 'taskStatusDone', cancelled: 'taskStatusCancelled',
};

const goalStatusKeys: Record<GoalStatus, string> = {
  active: 'goalStatusActive', completed: 'goalStatusCompleted', paused: 'goalStatusPaused', cancelled: 'goalStatusCancelled',
};

/** Renders source-backed document hits while preserving their cited revision links. */
export function SearchResults({ items }: { items: SearchHit[] }) {
  const t = useTranslations('entities');
  const { locale, timezone } = useDisplayPreferences();
  const formattingLocale = normalizeFormattingLocale(locale);
  return <ul className="record-list" aria-label={t('searchResults')}>
    {items.map((hit) => {
      const date = displayDate(hit, formattingLocale, timezone, t('dateUnknown'));
      const revisionHref = `/knowledge/documents/${hit.citation.documentId}?version=${hit.version_number}#cited-revision`;
      return <li className="record-row border border-border bg-card text-card-foreground rounded-md p-4" key={hit.chunk_id}>
        <div className="record-content">
          <Link href={revisionHref}><strong>{hit.title}</strong></Link>
          <p className="muted">{hit.source.name} · {hit.source.type} · {date}{hit.content_type ? ` · ${hit.content_type}` : ''}</p>
          <p className="search-excerpt">{hit.excerpt}</p>
          <Link href={revisionHref}>{t('openCitedRevision')} {hit.version_number}</Link>
        </div>
      </li>;
    })}
  </ul>;
}

/** Renders owner task read records without exposing write actions or search-only scores. */
export function TaskSearchResults({ items }: { items: TaskSearchHit[] }) {
  const t = useTranslations('entities');
  const { locale, timezone } = useDisplayPreferences();
  const formattingLocale = normalizeFormattingLocale(locale);
  return <ul className="record-list" aria-label={t('searchTasks')}>
    {items.map((item) => <li className="record-row border border-border bg-card text-card-foreground rounded-md p-4" key={item.id}>
      <strong>{item.title}</strong>
      {item.description && <p className="search-excerpt">{item.description}</p>}
      <p className="muted">{t(taskStatusKeys[item.status])} · {t('revision')} {item.revision}
        {item.due_date ? ` · ${t('taskDueDate')}: ${formatSearchDate(item.due_date, formattingLocale, timezone, true)}` : ''}
        {item.due_at ? ` · ${t('taskDueAt')}: ${formatSearchDate(item.due_at, formattingLocale, timezone)}` : ''}
      </p>
    </li>)}
  </ul>;
}

/** Renders owner goal read records with the owner's progress and date-only deadline. */
export function GoalSearchResults({ items }: { items: GoalSearchHit[] }) {
  const t = useTranslations('entities');
  const { locale, timezone } = useDisplayPreferences();
  const formattingLocale = normalizeFormattingLocale(locale);
  const numberFormat = new Intl.NumberFormat(formattingLocale, { maximumFractionDigits: 0 });
  return <ul className="record-list" aria-label={t('searchGoals')}>
    {items.map((item) => <li className="record-row border border-border bg-card text-card-foreground rounded-md p-4" key={item.id}>
      <strong>{item.title}</strong>
      {item.description && <p className="search-excerpt">{item.description}</p>}
      {item.desired_outcome && <p className="muted">{item.desired_outcome}</p>}
      <p className="muted">{t(goalStatusKeys[item.status])} · {t('revision')} {item.revision} · {t('goalProgress', { progress: numberFormat.format(item.progress) })}
        {item.deadline ? ` · ${t('goalDeadline')}: ${formatSearchDate(item.deadline, formattingLocale, timezone, true)}` : ''}
      </p>
    </li>)}
  </ul>;
}
