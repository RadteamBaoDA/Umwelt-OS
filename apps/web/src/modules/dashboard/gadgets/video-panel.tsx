'use client';

import { Play, Video } from 'lucide-react';
import { useTranslations } from 'next-intl';
import React, { useState } from 'react';
import type { GadgetInstance } from '../api';

/** Video media item representation. */
export interface VideoMediaItem {
  id: string;
  title: string;
  channel: string;
  duration: string;
  thumbnailUrl?: string;
  videoUrl?: string;
}

/** Props for the VideoPanel gadget component. */
export interface VideoPanelProps {
  /** Gadget instance configuration projection. */
  instance: GadgetInstance;
  /** Optional media items list. */
  items?: VideoMediaItem[];
}

/**
 * Standard Video Panel gadget template.
 * Displays monitored video broadcasts and media feeds with playback placeholders.
 *
 * @param props Gadget instance configuration and video items.
 * @returns Accessible video panel component.
 */
export function VideoPanel({ instance, items = [] }: VideoPanelProps) {
  const daily = useTranslations('daily');
  const definition = instance.definition;
  const [activeVideoId, setActiveVideoId] = useState<string | null>(null);

  return (
    <div className="flex flex-col h-full bg-card text-card-foreground p-3 space-y-3 overflow-y-auto">
      <div className="flex items-center justify-between text-xs text-muted-foreground border-b border-border pb-2">
        <div className="flex items-center gap-1.5 font-medium">
          <Video className="w-3.5 h-3.5 text-primary" />
          <span>{daily('videoStream')}</span>
        </div>
        <span className="font-mono text-[10px]">{daily('streamCount', { count: items.length })}</span>
      </div>

      {items.length === 0 && <p role="status" className="text-sm text-muted-foreground">{daily('noGadgetData')}</p>}
      <div className="space-y-2.5">
        {items.map((video) => (
          <div
            key={video.id}
            className="rounded-lg border border-border bg-card p-2.5 space-y-2 hover:bg-muted/15 transition-colors"
          >
            {/* Video preview / placeholder */}
            <div className="relative w-full h-28 bg-muted/40 rounded flex items-center justify-center overflow-hidden group cursor-pointer">
              <div className="w-10 h-10 rounded-full bg-primary/80 text-primary-foreground flex items-center justify-center shadow-md group-hover:scale-110 transition-transform">
                <Play className="w-4 h-4 ml-0.5" />
              </div>
              <span className="absolute bottom-1.5 right-1.5 px-1 py-0.2 rounded bg-surface/80 text-foreground text-[10px] font-mono">
                {video.duration}
              </span>
            </div>

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
