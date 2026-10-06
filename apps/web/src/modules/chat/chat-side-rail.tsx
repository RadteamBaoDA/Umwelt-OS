'use client';

import * as React from 'react';
import { useTranslations } from 'next-intl';
import type { ChatContext, Citation } from '@/modules/chat/api';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { CitationPanel } from './citation-panel';
import { useContextLabel } from './chat-context-bar';

/** Evidence rail for the full Chat page: Sources, Activity and Context tabs built from already-loaded conversation data. */
export function ChatSideRail({ citations, context }: { citations: Citation[]; context: ChatContext | null }) {
  const t = useTranslations('chat');
  const contextLabel = useContextLabel(context);
  return (
    <aside aria-label={t('railLabel')} className="hidden w-80 shrink-0 flex-col overflow-y-auto border-l border-border bg-surface p-3 lg:flex">
      <Tabs defaultValue="sources">
        <TabsList aria-label={t('railLabel')}>
          <TabsTrigger value="sources">{t('railSources')}{citations.length > 0 ? ` (${citations.length})` : ''}</TabsTrigger>
          <TabsTrigger value="activity">{t('railActivity')}</TabsTrigger>
          <TabsTrigger value="context">{t('railContext')}</TabsTrigger>
        </TabsList>
        <TabsContent value="sources">
          {citations.length ? <CitationPanel citations={citations} variant="card" /> : <p className="mt-3 text-xs text-muted-foreground">{t('railSourcesEmpty')}</p>}
        </TabsContent>
        <TabsContent value="activity"><p className="mt-3 text-xs text-muted-foreground">{t('railActivityEmpty')}</p></TabsContent>
        <TabsContent value="context"><p className="mt-3 text-xs text-muted-foreground">{contextLabel ?? t('railContextEmpty')}</p></TabsContent>
      </Tabs>
    </aside>
  );
}
