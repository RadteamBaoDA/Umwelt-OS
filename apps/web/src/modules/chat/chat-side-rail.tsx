'use client';

import * as React from 'react';
import { useTranslations } from 'next-intl';
import type { Citation } from './api';
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle, SheetTrigger } from '@/components/ui/sheet';
import { Button } from '@/components/ui/button';
import { BookOpenIcon, PanelRightClose, PanelRightOpen } from 'lucide-react';
import { CitationPanel, citationIdentity } from './citation-panel';
import { onCitationFocus, showAllCitations, useCitationFocus } from './chat-citation-focus';

/**
 * Source list shared by the desktop rail and mobile sheet. Opened from an answer it shows only that answer's sources
 * with the numbering of its inline chips, plus a "View all" button; otherwise it lists every source of the conversation.
 *
 * @param props - Every de-duplicated source of the conversation and its id (focus requests of other conversations are ignored).
 * @returns The citation list with an optional "View all" action.
 */
function RailSources({ citations, conversationId }: { citations: Citation[]; conversationId?: string | null }) {
  const t = useTranslations('chat');
  const focus = useCitationFocus(conversationId);
  const known = new Set(citations.map(citationIdentity));
  // A scope left over from another conversation no longer matches anything loaded, so it is ignored.
  const scope = focus.scope && focus.scope.every((citation) => known.has(citationIdentity(citation))) ? focus.scope : null;
  return (
    <div className="flex flex-col gap-1">
      {scope && (
        <Button type="button" variant="ghost" className="min-h-9 w-fit px-2 text-xs pointer-coarse:min-h-11" onClick={showAllCitations}>
          {t('railViewAll', { count: citations.length })}
        </Button>
      )}
      <CitationPanel citations={scope ? [...scope] : citations} variant="card" highlightedKeys={focus.keys} focusNonce={focus.nonce} />
    </div>
  );
}

/**
 * Evidence rail (320px) for the full Chat page from 1100px up. It renders only when the conversation has cited sources, and can be tucked into a 48px strip.
 *
 * @param props - Current conversation citations and id.
 * @returns Collapsible desktop evidence sidebar.
 */
export function ChatSideRail({ citations, conversationId }: { citations: Citation[]; conversationId?: string | null }) {
  const t = useTranslations('chat');
  const [collapsed, setCollapsed] = React.useState(false);
  // A citation click re-opens a tucked-away rail so the highlighted source is visible.
  React.useEffect(() => onCitationFocus(() => setCollapsed(false)), []);
  // Nothing cited: the region collapses away entirely instead of showing an empty panel.
  if (citations.length === 0) return null;
  return (
    <aside
      id="chat-evidence-rail"
      aria-label={t('railLabel')}
      className={`hidden shrink-0 flex-col overflow-y-auto border-l border-border bg-surface min-[1100px]:flex ${collapsed ? 'w-12 items-center p-0' : 'w-[320px] p-3'}`}
    >
      <Button
        type="button"
        variant="ghost"
        size="icon"
        className="size-11 shrink-0"
        onClick={() => setCollapsed((value) => !value)}
        aria-label={collapsed ? t('expandRail') : t('collapseRail')}
        aria-expanded={!collapsed}
        aria-controls={collapsed ? undefined : 'chat-evidence-rail-content'}
      >
        {collapsed ? <PanelRightOpen aria-hidden="true" /> : <PanelRightClose aria-hidden="true" />}
      </Button>
      {!collapsed && (
        <div id="chat-evidence-rail-content" className="w-full">
          <RailSources citations={citations} conversationId={conversationId} />
        </div>
      )}
    </aside>
  );
}

/**
 * Below-lg entry point to the evidence rail: a 44px trigger opening the same source list in a right-side Sheet.
 *
 * @param props - Citations to display and the conversation id they belong to.
 * @returns Trigger plus sheet; the trigger is hidden at lg where the rail is inline.
 */
export function ChatSourcesSheet({ citations, conversationId }: { citations: Citation[]; conversationId?: string | null }) {
  const t = useTranslations('chat');
  const [open, setOpen] = React.useState(false);
  // Below 1100px the rail is hidden, so a citation click opens this sheet instead.
  React.useEffect(
    () =>
      onCitationFocus(() => {
        if (!window.matchMedia?.('(min-width: 1100px)')?.matches) setOpen(true);
      }),
    [],
  );
  // Mirror the inline rail: nothing to show yet, so no trigger that would open an empty sheet.
  if (citations.length === 0) return null;
  return (
    <Sheet open={open} onOpenChange={setOpen}>
      <SheetTrigger asChild>
        <Button
          type="button"
          variant="outline"
          className="gap-2 px-3 text-xs min-[1100px]:hidden"
          aria-label={t('railOpen')}
          onClick={showAllCitations}
        >
          <BookOpenIcon className="size-4" aria-hidden="true" />
          <span className="hidden min-[480px]:inline">{t('railSources')}</span>
          <span>({citations.length})</span>
        </Button>
      </SheetTrigger>
      <SheetContent side="right" className="w-full overflow-y-auto p-3 sm:max-w-sm">
        <SheetHeader>
          <SheetTitle>{t('railLabel')}</SheetTitle>
          <SheetDescription className="sr-only">{t('railSheetDescription')}</SheetDescription>
        </SheetHeader>
        <RailSources citations={citations} conversationId={conversationId} />
      </SheetContent>
    </Sheet>
  );
}
