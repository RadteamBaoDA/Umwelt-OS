'use client';

import { useTranslations } from 'next-intl';
import React from 'react';
import { useReportGadgetFreshness } from '../gadget-freshness';
import type { GadgetInstance } from '../api';
import { GadgetNotice, GadgetSkeleton } from './gadget-notice';
import { useQuery } from '@tanstack/react-query';
import { documentKeys, listGadgetDocumentProjections } from '@/modules/knowledge';

/** Video media item representation. */
/** Props for the VideoPanel gadget component. */
export interface VideoPanelProps {
  /** Gadget instance configuration projection. */
  instance: GadgetInstance;
}

/**
 * Standard Video Panel gadget template.
 * Lists persisted YouTube records as outbound links; media playback is not provided here.
 *
 * @param props Gadget instance configuration and video items.
 * @returns Accessible video panel component.
 */
export function VideoPanel({ instance }: VideoPanelProps) {
  const daily = useTranslations('daily');
  const dashboard = useTranslations('dashboard');
  const definition = instance.definition;
  const sourceIds = [...new Set(definition.source_ids)].slice(0, 32);
  const query = useQuery({
    queryKey: [...documentKeys.all, 'video-panel', sourceIds],
    enabled: sourceIds.length > 0,
    queryFn: async () => {
      const page = await listGadgetDocumentProjections(sourceIds);
      return page.items.flatMap((item) => {
        const url = item.canonical_url;
        let safeUrl: string | undefined;
        try {
          const parsed = new URL(url ?? '');
          if (parsed.protocol === 'https:' && ['youtube.com', 'www.youtube.com', 'youtu.be'].includes(parsed.hostname)) safeUrl = parsed.toString();
        } catch { /* Invalid provider URLs are omitted. */ }
        if (item.provider_metadata?.provider !== 'youtube' || !safeUrl) return [];
        const fields = item.provider_metadata.source_fields;
        return [{
          id: item.document_version_id,
          title: typeof fields.title === 'string' ? fields.title : item.title,
          channel: typeof fields.author === 'string' ? fields.author : '',
          videoUrl: safeUrl,
        }];
      });
    },
  });
  const items = query.data ?? [];
  // The record count belongs in the frame footer, not an inner header row.
  useReportGadgetFreshness(null, items.length > 0 ? daily('streamCount', { count: items.length }) : null);

  return (
    <div className="flex flex-col h-full bg-card text-card-foreground p-3 space-y-3 overflow-y-auto">
      {sourceIds.length === 0 && <GadgetNotice kind="setup" className="p-0" message={dashboard('videoSourcesRequired')} />}
      {sourceIds.length > 0 && query.isPending && <GadgetSkeleton variant="list" className="p-0" label={dashboard('gadgetDataLoading')} />}
      {query.isError && <GadgetNotice kind="error" className="p-0" message={dashboard('gadgetDataLoadFailed')} />}
      {sourceIds.length > 0 && !query.isPending && !query.isError && items.length === 0 && <GadgetNotice kind="empty" className="p-0" message={dashboard('videoRecordsEmpty')} action={{ href: '/settings/sources', label: dashboard('gadgetConfigureAction') }} />}
      <ul className="divide-y divide-border">
        {items.map((video) => (
          <li key={video.id} className="py-2">
            {/* Ink title, underline only on hover; the link still opens YouTube in a new tab. */}
            <a href={video.videoUrl} target="_blank" rel="noopener noreferrer" title={dashboard('videoOpen')} className="line-clamp-2 text-sm font-semibold text-foreground hover:underline underline-offset-4">
              {video.title}
            </a>
            {video.channel && <p className="mt-0.5 text-xs text-muted-foreground">{video.channel}</p>}
          </li>
        ))}
      </ul>
    </div>
  );
}
