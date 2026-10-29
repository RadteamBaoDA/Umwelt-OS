'use client';

import * as React from 'react';
import { useTranslations } from 'next-intl';
import type { ChatContext, Citation } from '@/modules/chat/api';
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle, SheetTrigger } from '@/components/ui/sheet';
import { Button } from '@/components/ui/button';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { BookOpenIcon, PanelRightClose, PanelRightOpen } from 'lucide-react';
import { CitationPanel } from './citation-panel';
import { useContextLabel } from './chat-context-bar';

/**
 * Sources and Context tabs built from loaded conversation data; shared by the desktop rail and mobile sheet.
 *
 * @param props - Citations and context to display.
 * @returns Evidence tabs element.
 */
function RailTabs({ citations, context }: { citations: Citation[]; context: ChatContext | null }) {
  const t = useTranslations('chat');
  const contextLabel = useContextLabel(context);
  return (
    <Tabs defaultValue="sources">
      <TabsList aria-label={t('railLabel')}>
        <TabsTrigger value="sources">{t('railSources')}{citations.length > 0 ? ` (${citations.length})` : ''}</TabsTrigger>
        <TabsTrigger value="context">{t('railContext')}</TabsTrigger>
      </TabsList>
      <TabsContent value="sources">
        {citations.length ? <CitationPanel citations={citations} variant="card" /> : <p className="mt-3 text-xs text-muted-foreground">{t('railSourcesEmpty')}</p>}
      </TabsContent>
      <TabsContent value="context"><p className="mt-3 text-xs text-muted-foreground">{contextLabel ?? t('railContextEmpty')}</p></TabsContent>
    </Tabs>
  );
}

/**
 * Evidence sidebar for the full Chat page; it starts collapsed and toggles the full Sources, Activity, and Context panel at desktop widths.
 *
 * @param props - Current conversation citations and attached context.
 * @returns Collapsible desktop evidence sidebar.
 */
export function ChatSideRail({ citations, context }: { citations: Citation[]; context: ChatContext | null }) {
  const t = useTranslations('chat');
  const [collapsed, setCollapsed] = React.useState(true);
  return (
    <aside id="chat-evidence-rail" aria-label={t('railLabel')} className={`hidden shrink-0 flex-col overflow-y-auto border-l border-border bg-surface lg:flex ${collapsed ? 'w-12 items-center p-0' : 'w-[300px] p-3'}`}>
      <Button type="button" variant="ghost" size="icon" className="size-11 shrink-0" onClick={() => setCollapsed((value) => !value)} aria-label={collapsed ? t('expandRail') : t('collapseRail')} aria-expanded={!collapsed} aria-controls={collapsed ? undefined : 'chat-evidence-rail-content'}>
        {collapsed ? <PanelRightOpen aria-hidden="true" /> : <PanelRightClose aria-hidden="true" />}
      </Button>
      {!collapsed && <div id="chat-evidence-rail-content" className="w-full"><RailTabs citations={citations} context={context} /></div>}
    </aside>
  );
}

/**
 * Below-lg entry point to the evidence rail: a 44px trigger opening the same Sources, Activity, and Context tabs in a right-side Sheet.
 *
 * @param props - Citations and context to display.
 * @returns Trigger plus sheet; the trigger is hidden at lg where the rail is inline.
 */
export function ChatSourcesSheet({ citations, context }: { citations: Citation[]; context: ChatContext | null }) {
  const t = useTranslations('chat');
  return (
    <Sheet>
      <SheetTrigger asChild>
        <Button type="button" variant="outline" className="min-h-11 gap-2 px-3 text-xs lg:hidden" aria-label={t('railOpen')}>
          <BookOpenIcon className="size-4" aria-hidden="true" />
          <span>{t('railSources')}{citations.length > 0 ? ` (${citations.length})` : ''}</span>
        </Button>
      </SheetTrigger>
      <SheetContent side="right" className="w-full overflow-y-auto p-3 sm:max-w-sm">
        <SheetHeader>
          <SheetTitle>{t('railLabel')}</SheetTitle>
          <SheetDescription className="sr-only">{t('railSheetDescription')}</SheetDescription>
        </SheetHeader>
        <RailTabs citations={citations} context={context} />
      </SheetContent>
    </Sheet>
  );
}
