'use client';

import { useTranslations } from 'next-intl';
import React, { useState } from 'react';
import { useReportGadgetFreshness } from '../gadget-freshness';
import type { GadgetInstance } from '../api';
import { GadgetNotice, GadgetSkeleton } from './gadget-notice';
import { ShowMoreButton, useShowMore } from './show-more';
import { useQuery } from '@tanstack/react-query';
import { documentKeys, listGadgetDocumentProjections } from '@/modules/knowledge';
import { useSkin } from '@/core/use-skin';

/** Video media item representation. */
/** Props for the VideoPanel gadget component. */
export interface VideoPanelProps {
  /** Gadget instance configuration projection. */
  instance: GadgetInstance;
}

/** Decorative 16:9 thumbnail; renders nothing without a URL or after a load error (no empty placeholder). */
function VideoThumb({ src }: { src: string | null }) {
  const [failed, setFailed] = useState(false);
  if (!src || failed) return null;
  return (
    <div className="aspect-video w-28 shrink-0 overflow-hidden rounded-md bg-muted">
      {/* eslint-disable-next-line @next/next/no-img-element -- same-origin signed proxy path */}
      <img src={src} alt="" loading="lazy" decoding="async" onError={() => setFailed(true)} className="size-full object-cover" />
    </div>
  );
}

/** Video item as the panel maps it from a YouTube document projection. */
interface VideoItem {
  id: string;
  title: string;
  channel: string;
  videoUrl: string;
  imageUrl: string | null;
  durationSeconds: number | null;
}

/**
 * `mm:ss` (or `h:mm:ss`) clock text of a duration.
 *
 * @param seconds Non-negative duration in seconds; null, negative or non-finite yields an empty string.
 */
export function formatVideoDuration(seconds: number | null): string {
  if (seconds === null || !Number.isFinite(seconds) || seconds < 0) return '';
  const total = Math.round(seconds);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  // Minutes are two digits like the demo caption (`02:14`).
  const s = String(total % 60).padStart(2, '0');
  return h > 0 ? `${h}:${String(m).padStart(2, '0')}:${s}` : `${String(m).padStart(2, '0')}:${s}`;
}

/**
 * Terminal skin: the first video as a 16:9 frame with a play square and a `title  duration` caption (demo `.dash-video`).
 * The frame is the same outbound link as the list rows (new tab, `noopener`); the thumbnail shows when the record has one.
 *
 * @param props.video First video of the list.
 */
function TerminalVideoFrame({ video }: { video: VideoItem }) {
  const dashboard = useTranslations('dashboard');
  const [failed, setFailed] = useState(false);
  return (
    <a
      className="video-frame"
      href={video.videoUrl}
      target="_blank"
      rel="noopener noreferrer"
      title={dashboard('video.open')}
      aria-label={`${dashboard('video.open')}: ${video.title}`}
    >
      {video.imageUrl && !failed && (
        // eslint-disable-next-line @next/next/no-img-element -- same-origin signed proxy path
        <img src={video.imageUrl} alt="" loading="lazy" decoding="async" onError={() => setFailed(true)} className="video-frame-img" />
      )}
      <span className="video-frame-play" aria-hidden="true">
        ▶
      </span>
      <span className="video-frame-cap" aria-hidden="true">
        <span>{video.title}</span>
        <span>{formatVideoDuration(video.durationSeconds)}</span>
      </span>
    </a>
  );
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
      return page.items.flatMap((item): VideoItem[] => {
        const url = item.canonical_url;
        let safeUrl: string | undefined;
        try {
          const parsed = new URL(url ?? '');
          if (parsed.protocol === 'https:' && ['youtube.com', 'www.youtube.com', 'youtu.be'].includes(parsed.hostname))
            safeUrl = parsed.toString();
        } catch {
          /* Invalid provider URLs are omitted. */
        }
        if (item.provider_metadata?.provider !== 'youtube' || !safeUrl) return [];
        const fields = item.provider_metadata.source_fields;
        return [
          {
            id: item.document_version_id,
            title: typeof fields.title === 'string' ? fields.title : item.title,
            channel: typeof fields.author === 'string' ? fields.author : '',
            videoUrl: safeUrl,
            imageUrl: item.image_url ?? null,
            durationSeconds: item.media?.duration_seconds ?? null,
          },
        ];
      });
    },
  });
  const items = query.data ?? [];
  const more = useShowMore(items, 5);
  const terminal = useSkin() === 'terminal';
  const first = terminal ? items[0] : undefined;
  // The record count belongs in the frame footer, not an inner header row.
  useReportGadgetFreshness(null, items.length > 0 ? daily('streamCount', { count: items.length }) : null);

  return (
    <div className="flex flex-col h-full bg-card text-card-foreground p-3 space-y-3 overflow-y-auto">
      {sourceIds.length === 0 && <GadgetNotice kind="setup" className="p-0" message={dashboard('video.sourcesRequired')} />}
      {sourceIds.length > 0 && query.isPending && <GadgetSkeleton variant="list" className="p-0" label={dashboard('gadget.dataLoading')} />}
      {query.isError && <GadgetNotice kind="error" className="p-0" message={dashboard('gadget.dataLoadFailed')} />}
      {sourceIds.length > 0 && !query.isPending && !query.isError && items.length === 0 && (
        <GadgetNotice
          kind="empty"
          className="p-0"
          message={dashboard('video.recordsEmpty')}
          action={{ href: '/settings/sources', label: dashboard('gadget.configureAction') }}
        />
      )}
      {first && (
        <>
          <TerminalVideoFrame video={first} />
          <p className="video-term-note m-0" aria-hidden="true">
            {dashboard('video.termNote', { count: items.length, channel: first.channel })}
          </p>
        </>
      )}
      {/* Terminal skin: the first video is the frame above; the other rows stay available to assistive tech only. */}
      <ul className={first ? 'video-rest divide-y divide-border' : 'divide-y divide-border'}>
        {more.visible.slice(first ? 1 : 0).map((video) => (
          <li key={video.id} className="flex items-start gap-3 py-2">
            <VideoThumb src={video.imageUrl} />
            <div className="min-w-0 flex-1">
              {/* Ink title, underline only on hover; the link still opens YouTube in a new tab. */}
              <a
                href={video.videoUrl}
                target="_blank"
                rel="noopener noreferrer"
                title={dashboard('video.open')}
                className="line-clamp-2 text-sm font-semibold text-foreground hover:underline underline-offset-4"
              >
                {video.title}
              </a>
              {video.channel && <p className="mt-0.5 text-xs text-muted-foreground">{video.channel}</p>}
            </div>
          </li>
        ))}
      </ul>
      <ShowMoreButton state={more} />
    </div>
  );
}
