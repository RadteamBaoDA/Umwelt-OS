import Link from 'next/link';
import { useTranslations } from 'next-intl';
import { formatDateTime, normalizeFormattingLocale } from '@/core/i18n';
import type { AppLocaleId } from '@/core/i18n';
import type { TimelineEvent } from './api';

/** Renders distinct occurrence, observation, and validity timestamps with linked provenance. */
export function EventDetail({ event, locale, timezone, entityNames }: {
  event: TimelineEvent;
  locale: AppLocaleId;
  timezone: string;
  entityNames: ReadonlyMap<string, string>;
}) {
  const t = useTranslations('timeline');
  /** Formats date-only values as calendar dates without converting their day through a local zone. */
  function formatCalendarDate(value: string) {
    const [year, month, day] = value.split('-').map(Number);
    return new Intl.DateTimeFormat(normalizeFormattingLocale(locale), { dateStyle: 'medium', timeZone: 'UTC' })
      .format(new Date(Date.UTC(year, month - 1, day)));
  }
  /** Labels late arrival only when a derived event's precision supports a safe comparison. */
  function isLateArrival() {
    if (event.origin !== 'derived') return false;
    if (event.date_precision === 'timed' && event.started_at) {
      return new Date(event.observed_at).getTime() > new Date(event.started_at).getTime();
    }
    if (event.date_precision === 'date' && event.occurred_date) {
      const observedDate = new Intl.DateTimeFormat('en-CA', {
        timeZone: event.occurrence_timezone ?? timezone,
        year: 'numeric', month: '2-digit', day: '2-digit',
      }).format(new Date(event.observed_at));
      return observedDate > event.occurred_date;
    }
    return false;
  }

  let occurred = t('unknownTime');
  if (event.date_precision === 'timed' && event.started_at) {
    occurred = formatDateTime(event.started_at, locale, timezone);
    if (event.ended_at) occurred += ` – ${formatDateTime(event.ended_at, locale, timezone)}`;
  } else if (event.date_precision === 'date' && event.occurred_date) {
    occurred = formatCalendarDate(event.occurred_date);
    if (event.end_date) occurred += ` – ${formatCalendarDate(event.end_date)}`;
  }

  return <article className="card stack" aria-labelledby={`event-${event.id}-title`}>
    <div><p className="muted">{event.type}{event.subtype ? ` · ${event.subtype}` : ''} · {event.origin === 'derived' ? t('derived') : t('manual')}</p>
      <h2 id={`event-${event.id}-title`}>{event.title}</h2>{event.summary && <p>{event.summary}</p>}</div>
    <dl className="timeline-times">
      <div><dt>{t('occurred')}</dt><dd>{occurred}</dd></div>
      <div><dt>{t('observed')}</dt><dd>{formatDateTime(event.observed_at, locale, timezone)}</dd></div>
      <div><dt>{t('recorded')}</dt><dd>{formatDateTime(event.created_at, locale, timezone)}</dd></div>
      <div><dt>{t('validity')}</dt><dd>{event.valid_from ? formatDateTime(event.valid_from, locale, timezone) : t('openStart')} – {event.valid_to ? formatDateTime(event.valid_to, locale, timezone) : t('openEnd')}</dd></div>
    </dl>
    {isLateArrival() && <p className="muted" role="status">{t('lateArrival')}</p>}
    {!!event.participants.length && <section aria-label={t('participants')}><h3>{t('participants')}</h3><ul className="stack">{event.participants.map((participant) => <li key={`${participant.entity_id}:${participant.role}`}><Link href={`/knowledge/entities/${participant.entity_id}`}>{entityNames.get(participant.entity_id) ?? participant.entity_id}</Link> · {participant.role}</li>)}</ul></section>}
    {!!event.evidence.length && <section aria-label={t('evidence')}><h3>{t('evidence')}</h3><ul className="stack">{event.evidence.map((evidence) => <li key={`${evidence.document_version_id}:${evidence.chunk_id}`}>
      <Link href={`/knowledge/documents/${evidence.document_id}?version=${evidence.version_number}#cited-revision`}>{evidence.title}</Link>
      <small className="muted">{evidence.metadata_is_version_snapshot ? t('metadataVersionSnapshot') : t('metadataCurrentFallback')}</small>
      <p className="muted">{t('evidenceObserved')} {formatDateTime(evidence.observed_at, locale, timezone)} · {t('sourceId')} {evidence.source_id}</p><blockquote>{evidence.excerpt}</blockquote>
    </li>)}</ul></section>}
  </article>;
}
