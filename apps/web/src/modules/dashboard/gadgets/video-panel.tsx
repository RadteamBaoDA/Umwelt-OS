'use client';

import { ExternalLink, Video } from 'lucide-react';
import { useTranslations } from 'next-intl';
import React from 'react';
import type { GadgetInstance } from '../api';
import { useQuery } from '@tanstack/react-query';
import { documentKeys, listGadgetDocumentProjections } from '@/modules/knowledge/api';

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

  return (
    <div className="flex flex-col h-full bg-card text-card-foreground p-3 space-y-3 overflow-y-auto">
      <div className="flex items-center justify-between text-xs text-muted-foreground border-b border-border pb-2">
        <div className="flex items-center gap-1.5 font-medium">
          <Video className="w-3.5 h-3.5 text-primary" />
          <span>{daily('videoStream')}</span>
        </div>
        <span className="font-mono text-[10px]">{daily('streamCount', { count: items.length })}</span>
      </div>

      {sourceIds.length === 0 && <p role="status" className="text-sm text-muted-foreground">{dashboard('videoSourcesRequired')}</p>}
      {sourceIds.length > 0 && query.isPending && <p role="status" className="text-sm text-muted-foreground">{dashboard('gadgetDataLoading')}</p>}
      {query.isError && <p role="alert" className="text-sm text-destructive">{dashboard('gadgetDataLoadFailed')}</p>}
      {sourceIds.length > 0 && !query.isPending && !query.isError && items.length === 0 && <p role="status" className="text-sm text-muted-foreground">{dashboard('videoRecordsEmpty')}</p>}
      <div className="space-y-2.5">
        {items.map((video) => (
          <div
            key={video.id}
            className="rounded-lg border border-border bg-card p-2.5 space-y-2 hover:bg-muted/15 transition-colors"
          >
            <a href={video.videoUrl} target="_blank" rel="noopener noreferrer" aria-label={video.title} className="flex items-center gap-2 text-xs text-primary underline">
              <ExternalLink className="w-3.5 h-3.5" />
              {daily('videoStream')}
            </a>

            <div className="space-y-0.5">
              <h5 className="text-xs font-semibold text-foreground line-clamp-1">{video.title}</h5>
              <p className="text-[10px] text-muted-foreground font-mono">{video.channel}</p>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
