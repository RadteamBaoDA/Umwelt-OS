'use client';

import { useQuery } from '@tanstack/react-query';
import { useRef, useState } from 'react';
import { useTranslations } from 'next-intl';
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { useWorkspace } from '@/core/workspace-context';
import { listSources } from '@/modules/sources/api';
import { StoryDetail } from './story-detail';
import { StoryList } from './story-list';

/**
 * Full News page. Owners scope stories to their active sources; members get the backend's shared
 * projection (source ids are ignored server side) with no trends and no topic or entity filters.
 */
export function NewsPage() {
  const t = useTranslations('shell');
  const d = useTranslations('detail');
  const { isOwner } = useWorkspace();
  const [storyId, setStoryId] = useState<string | null>(null);
  const trigger = useRef<HTMLButtonElement | null>(null);
  const sources = useQuery({ queryKey: ['news-page-sources'], queryFn: () => listSources(), enabled: isOwner });
  const sourceIds = isOwner ? (sources.data?.items ?? []).filter((s) => s.status === 'active').slice(0, 32).map((s) => s.id) : [];
  const ready = !isOwner || sources.isSuccess;
  return (
    <section className="space-y-3" aria-labelledby="news-page-title">
      <h1 id="news-page-title" className="text-xl font-semibold">{t('news')}</h1>
      {ready && <StoryList sourceIds={sourceIds} showTrends={isOwner} onSelectStory={(id, el) => { trigger.current = el; setStoryId(id); }} />}
      <Dialog open={storyId !== null} onOpenChange={(open) => { if (!open) setStoryId(null); }}>
        <DialogContent closeLabel={d('close')} onCloseAutoFocus={(e) => { e.preventDefault(); if (trigger.current?.isConnected) trigger.current.focus(); }}
          className="h-[85dvh] max-w-4xl overflow-hidden">
          <DialogHeader><DialogTitle>{d('title_story')}</DialogTitle><DialogDescription className="sr-only">{d('title_story')}</DialogDescription></DialogHeader>
          {storyId && <StoryDetail storyId={storyId} sourceIds={sourceIds} onBack={() => setStoryId(null)} />}
        </DialogContent>
      </Dialog>
    </section>
  );
}
