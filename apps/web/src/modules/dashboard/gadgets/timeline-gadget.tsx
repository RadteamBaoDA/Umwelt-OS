'use client';

import { useQuery } from '@tanstack/react-query';
import {
  ExternalLink,
  History,
  Link as LinkIcon,
  RotateCw,
} from 'lucide-react';
import Link from 'next/link';
import { useTranslations } from 'next-intl';
import React, { useMemo } from 'react';
import { Button } from '@/components/ui/button';
import { useDisplayPreferences } from '@/core/query-provider';
import { listTimeline, type TimelineEvent, type TimelineQuery } from '@/modules/timeline/api';
import type { GadgetInstance } from '../api';
import { BareButton } from '@/components/bare-button';

/** Props for the TimelineGadget component. */
export interface TimelineGadgetProps {
  /** Gadget instance configuration and definition. */
  instance: GadgetInstance;
  /** Optional pre-loaded event list. */
  initialEvents?: TimelineEvent[];
}

/**
 * Standard Timeline gadget template.
 * Renders a chronological slice of recorded and derived events from the Phase 5 Temporal API,
 * including date precision, linked participants, and evidence excerpts.
 * Its fixed loading stack contributes to the reading floor; the root remains scrollable when compact.
 *
 * @param props Gadget instance configuration and optional initial items.
 * @returns Accessible timeline slice gadget component.
 */
export function TimelineGadget({ instance, initialEvents }: TimelineGadgetProps) {
  const t = useTranslations('dashboard');
  const ts = useTranslations('uiSweep');
  const tc = useTranslations('common');
  const display = useDisplayPreferences();

  const definition = instance.definition;

  // Build query parameters from gadget configuration
  const query: TimelineQuery = useMemo(() => {
    const sourceId = definition.source_ids?.[0] ?? '';
    const entityId = definition.scope?.source_item_ids?.[0] ?? '';
    const typeFilter = definition.filters?.keywords?.[0] ?? '';

    return {
      date_from: '',
      date_to: '',
      timezone: display.timezone,
      source_id: sourceId,
      entity_id: entityId,
      precision: 'all',
      type: typeFilter || undefined,
    };
  }, [definition, display.timezone]);

  // Fetch timeline slice from API
  const timelineQuery = useQuery({
    queryKey: ['dashboard-timeline-gadget', instance.id, query],
    queryFn: async () => {
      const result = await listTimeline(query);
      return result.items.slice(0, definition.filters?.limit ?? 15);
    },
    staleTime: 30_000,
  });

  const events = useMemo(() => timelineQuery.data ?? initialEvents ?? [], [timelineQuery.data, initialEvents]);
  // Group by the owner's local day so the sticky day label replaces a full timestamp on every row.
  const dayLabel = useMemo(() => new Intl.DateTimeFormat(display.locale, { dateStyle: 'medium', timeZone: display.timezone }), [display.locale, display.timezone]);
  const timeLabel = useMemo(() => new Intl.DateTimeFormat(display.locale, { timeStyle: 'short', timeZone: display.timezone }), [display.locale, display.timezone]);
  const days = useMemo(() => {
    const groups: { key: string; label: string; items: TimelineEvent[] }[] = [];
    for (const evt of events) {
      const when = evt.started_at || evt.occurred_date || evt.observed_at;
      const parsed = new Date(when);
      const label = Number.isNaN(parsed.getTime()) ? when : dayLabel.format(parsed);
      const last = groups[groups.length - 1];
      if (last && last.label === label) last.items.push(evt);
      else groups.push({ key: `${label}:${evt.id}`, label, items: [evt] });
    }
    return groups;
  }, [events, dayLabel]);

  return (
    <div className="flex flex-col h-full bg-card text-card-foreground p-3 space-y-3 overflow-y-auto overflow-x-hidden">
      {/* Quiet top row: localized event count, refresh and the full-timeline link. */}
      <div className="flex items-center justify-between text-xs">
        <span className="text-muted-foreground tabular-nums">
          {t('timelineEventCount', { count: events.length })}
        </span>
        <div className="flex items-center gap-1.5">
          <BareButton
            type="button"
            onClick={() => timelineQuery.refetch()}
            disabled={timelineQuery.isFetching}
            className="p-1 rounded text-muted-foreground hover:text-foreground transition-colors"
            title={ts('tlRefresh')}
            aria-label={ts('tlRefresh')}
          >
            <RotateCw
              className={`w-3.5 h-3.5 ${timelineQuery.isFetching ? 'animate-spin' : ''}`}
            />
          </BareButton>
          <Link
            href="/timeline"
            className="inline-flex items-center gap-1 text-[11px] font-semibold text-primary hover:underline"
          >
            <span>{ts('tlFull')}</span>
            <ExternalLink className="w-3 h-3" />
          </Link>
        </div>
      </div>

      {/* Loading Skeleton */}
      {timelineQuery.isLoading && events.length === 0 && (
        <div className="flex-1 space-y-3 p-2 motion-safe:animate-pulse">
          <div className="h-4 bg-rule rounded w-3/4" />
          <div className="h-12 bg-rule rounded" />
          <div className="h-12 bg-rule rounded" />
        </div>
      )}

      {/* Error state */}
      {timelineQuery.isError && events.length === 0 && (
        <div className="flex-1 flex flex-col items-center justify-center p-4 text-center text-xs text-muted-foreground">
          <p className="text-destructive mb-2">{ts('tlFailed')}</p>
          <Button
            onClick={() => timelineQuery.refetch()}
            className="secondary text-xs h-7"
          >
            {tc('retry')}
          </Button>
        </div>
      )}

      {/* Empty State */}
      {!timelineQuery.isLoading && events.length === 0 && (
        <div className="flex-1 flex flex-col items-center justify-center p-6 text-center text-muted-foreground">
          <History className="w-8 h-8 mb-2 opacity-50" />
          <p className="text-xs font-semibold text-foreground mb-1">{ts('tlNone')}</p>
          <p className="text-[11px] max-w-xs text-muted-foreground">
            {ts('tlNoneHint')}
          </p>
        </div>
      )}

      {/* Events grouped by day: sticky day label, ink dot, time-only meta. */}
      {events.length > 0 && (
        <div className="flex-1 min-h-0 overflow-y-auto pr-1">
          {days.map((day) => (
            <section key={day.key} aria-label={day.label}>
              <h4 className="sticky top-0 z-10 bg-card py-1 text-xs font-semibold text-muted-foreground">{day.label}</h4>
              <ol className="relative ml-1 space-y-3 border-l border-border pb-3 pt-1">
                {day.items.map((evt) => (
                  <li key={evt.id} className="group relative ml-4">
                    {/* Ink dot on the rule */}
                    <span className="absolute -left-[21px] top-1.5 h-2 w-2 rounded-full bg-foreground" aria-hidden="true" />
                    <div className="space-y-0.5">
                      <div className="flex items-center gap-2 text-xs text-muted-foreground tabular-nums">
                        {evt.started_at && !Number.isNaN(Date.parse(evt.started_at)) && <time dateTime={evt.started_at}>{timeLabel.format(new Date(evt.started_at))}</time>}
                        <span>{evt.type}</span>
                        {evt.origin === 'derived' && (
                          <span title={ts('tlDerived')}>{t('timelineDerived')}</span>
                        )}
                      </div>
                      <h5 className="text-sm font-semibold leading-snug text-foreground">{evt.title}</h5>
                      {evt.summary && (
                        <p className="line-clamp-2 text-xs leading-relaxed text-muted-foreground">{evt.summary}</p>
                      )}
                      {evt.participants && evt.participants.length > 0 && (
                        <div className="flex flex-wrap gap-2 pt-0.5">
                          {evt.participants.slice(0, 3).map((p, idx) => (
                            <Link
                              key={idx}
                              href={`/knowledge/entities/${p.entity_id}`}
                              className="inline-flex items-center gap-0.5 text-xs text-muted-foreground transition-colors hover:text-foreground hover:underline"
                            >
                              <LinkIcon className="w-2.5 h-2.5" aria-hidden="true" />
                              <span>{p.role || t('timelineParticipant')}</span>
                            </Link>
                          ))}
                        </div>
                      )}
                    </div>
                  </li>
                ))}
              </ol>
            </section>
          ))}
        </div>
      )}
    </div>
  );
}
