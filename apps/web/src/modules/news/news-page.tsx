'use client';

import { useQuery } from '@tanstack/react-query';
import { useRouter, useSearchParams } from 'next/navigation';
import { useRef, useState } from 'react';
import { useTranslations } from 'next-intl';
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { useWorkspace } from '@/core/workspace-context';
import { listSources } from '@/modules/sources';
import { StoryDetail } from './story-detail';
import { StoryList } from './story-list';

/**
 * Full News page. Owners scope stories to their active sources; members get the backend's shared
 * projection (source ids are ignored server side) with no trends and no topic or entity filters.
 * `?story=<id>` opens that story on load (brief citations link here); closing it drops the param so a reload does not
 * reopen it.
 */
export function NewsPage() {
  const t = useTranslations('shell');
  const d = useTranslations('detail');
  const { isOwner } = useWorkspace();
  const params = useSearchParams();
  const router = useRouter();
  const [storyId, setStoryId] = useState<string | null>(() => params.get('story')?.trim().slice(0, 64) || null);
  // Title-row slot that StoryList portals its refresh/translated controls into.
  const [actionsHost, setActionsHost] = useState<HTMLElement | null>(null);
  const trigger = useRef<HTMLButtonElement | null>(null);
  const sources = useQuery({ queryKey: ['news-page-sources'], queryFn: () => listSources(), enabled: isOwner });
  const sourceIds = isOwner
    ? (sources.data?.items ?? [])
        .filter((s) => s.status === 'active')
        .slice(0, 32)
        .map((s) => s.id)
    : [];
  const ready = !isOwner || sources.isSuccess;
  return (
    <section className="rounded-lg border bg-card p-6 space-y-3" aria-labelledby="news-page-title">
      <div className="flex items-center justify-between gap-2">
        <h1 id="news-page-title">{t('news')}</h1>
        <div ref={setActionsHost} className="flex items-center gap-1" />
      </div>
      {ready && (
        <StoryList
          sourceIds={sourceIds}
          showTrends={isOwner}
          noSources={isOwner && sourceIds.length === 0}
          actionsHost={actionsHost}
          onSelectStory={(id, el) => {
            trigger.current = el;
            setStoryId(id);
          }}
        />
      )}
      <Dialog
        open={storyId !== null}
        onOpenChange={(open) => {
          if (open) return;
          setStoryId(null);
          if (params.has('story')) router.replace('/news', { scroll: false });
        }}
      >
        <DialogContent
          closeLabel={d('close')}
          onCloseAutoFocus={(e) => {
            e.preventDefault();
            if (trigger.current?.isConnected) trigger.current.focus();
          }}
          className="h-[85dvh] max-w-4xl overflow-hidden"
        >
          <DialogHeader>
            <DialogTitle>{d('title.story')}</DialogTitle>
            <DialogDescription className="sr-only">{d('title.story')}</DialogDescription>
          </DialogHeader>
          {storyId && <StoryDetail storyId={storyId} sourceIds={sourceIds} onBack={() => setStoryId(null)} />}
        </DialogContent>
      </Dialog>
    </section>
  );
}
