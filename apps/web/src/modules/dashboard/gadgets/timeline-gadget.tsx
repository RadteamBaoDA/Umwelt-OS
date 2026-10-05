'use client';

import { useQuery } from '@tanstack/react-query';
import {
  Calendar,
  Clock,
  ExternalLink,
  History,
  Link as LinkIcon,
  RotateCw,
  Sparkles,
} from 'lucide-react';
import Link from 'next/link';
import { useTranslations } from 'next-intl';
import React, { useMemo } from 'react';
import { Button } from '@/components/ui/button';
import { formatDateTime } from '@/core/i18n';
import { useDisplayPreferences } from '@/core/query-provider';
import { listTimeline, type TimelineEvent, type TimelineQuery } from '@/modules/timeline/api';
import type { GadgetInstance } from '../api';

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
 *
 * @param props Gadget instance configuration and optional initial items.
 * @returns Accessible timeline slice gadget component.
 */
export function TimelineGadget({ instance, initialEvents }: TimelineGadgetProps) {
  const t = useTranslations('dashboard');
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

  const events = timelineQuery.data ?? initialEvents ?? [];

  return (
    <div className="flex flex-col h-full bg-card text-card-foreground p-3 space-y-3 overflow-hidden">
      {/* Header bar with total events and link to full timeline page */}
      <div className="flex items-center justify-between border-b border-border pb-2 text-xs">
        <span className="text-muted-foreground font-mono">
          {events.length} chronological {events.length === 1 ? 'event' : 'events'}
        </span>
        <div className="flex items-center gap-1.5">
          <button
            type="button"
            onClick={() => timelineQuery.refetch()}
            disabled={timelineQuery.isFetching}
            className="p-1 rounded text-muted-foreground hover:text-foreground transition-colors"
            title="Refresh timeline slice"
            aria-label="Refresh timeline slice"
          >
            <RotateCw
              className={`w-3.5 h-3.5 ${timelineQuery.isFetching ? 'animate-spin' : ''}`}
            />
          </button>
          <Link
            href="/timeline"
            className="inline-flex items-center gap-1 text-[11px] font-semibold text-primary hover:underline"
          >
            <span>Full timeline</span>
            <ExternalLink className="w-3 h-3" />
          </Link>
        </div>
      </div>

      {/* Loading Skeleton */}
      {timelineQuery.isLoading && events.length === 0 && (
        <div className="flex-1 space-y-3 p-2 animate-pulse">
          <div className="h-4 bg-muted/30 rounded w-3/4" />
          <div className="h-12 bg-muted/20 rounded" />
          <div className="h-12 bg-muted/20 rounded" />
        </div>
      )}

      {/* Error state */}
      {timelineQuery.isError && events.length === 0 && (
        <div className="flex-1 flex flex-col items-center justify-center p-4 text-center text-xs text-muted-foreground">
          <p className="text-destructive mb-2">Failed to load temporal events</p>
          <Button
            onClick={() => timelineQuery.refetch()}
            className="secondary text-xs h-7"
          >
            Retry
          </Button>
        </div>
      )}

      {/* Empty State */}
      {!timelineQuery.isLoading && events.length === 0 && (
        <div className="flex-1 flex flex-col items-center justify-center p-6 text-center text-muted-foreground">
          <History className="w-8 h-8 mb-2 opacity-50" />
          <p className="text-xs font-semibold text-foreground mb-1">No temporal events recorded</p>
          <p className="text-[11px] max-w-xs text-muted-foreground">
            Events extracted from ingested documents and sources will appear in this timeline slice.
          </p>
        </div>
      )}

      {/* Vertical Timeline Events Slice */}
      {events.length > 0 && (
        <div className="flex-1 overflow-y-auto space-y-3 min-h-0 pr-1">
          <ol className="relative border-l border-border/80 ml-2 space-y-4 pt-1">
            {events.map((evt) => {
              const eventDate = evt.started_at || evt.occurred_date || evt.observed_at;
              const formattedDate = formatDateTime(eventDate, display.locale, display.timezone);

              return (
                <li key={evt.id} className="ml-3 group">
                  {/* Timeline point dot */}
                  <span className="absolute -left-1.5 mt-1.5 h-3 w-3 rounded-full border-2 border-card bg-primary group-hover:scale-125 transition-transform" />

                  <div className="space-y-1">
                    {/* Timestamp & origin badges */}
                    <div className="flex items-center justify-between gap-2 text-[10px] text-muted-foreground font-mono">
                      <span>{formattedDate}</span>
                      <div className="flex items-center gap-1">
                        <span className="px-1 py-0.2 rounded bg-muted/30 text-foreground font-sans uppercase">
                          {evt.type}
                        </span>
                        {evt.origin === 'derived' && (
                          <span
                            className="px-1 py-0.2 rounded bg-primary/15 text-primary font-sans"
                            title="Derived from source evidence"
                          >
                            Derived
                          </span>
                        )}
                      </div>
                    </div>

                    {/* Title */}
                    <h5 className="text-xs font-semibold text-foreground leading-snug">
                      {evt.title}
                    </h5>

                    {/* Summary excerpt */}
                    {evt.summary && (
                      <p className="text-[11px] text-muted-foreground line-clamp-2 leading-relaxed">
                        {evt.summary}
                      </p>
                    )}

                    {/* Linked participants or evidence */}
                    {evt.participants && evt.participants.length > 0 && (
                      <div className="flex flex-wrap gap-1 pt-0.5">
                        {evt.participants.slice(0, 3).map((p, idx) => (
                          <Link
                            key={idx}
                            href={`/knowledge/entities/${p.entity_id}`}
                            className="inline-flex items-center gap-0.5 text-[10px] px-1.5 py-0.2 rounded bg-muted/20 hover:bg-muted/40 text-muted-foreground transition-colors"
                          >
                            <LinkIcon className="w-2.5 h-2.5" />
                            <span>{p.role || 'Participant'}</span>
                          </Link>
                        ))}
                      </div>
                    )}
                  </div>
                </li>
              );
            })}
          </ol>
        </div>
      )}
    </div>
  );
}
