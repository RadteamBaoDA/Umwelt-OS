'use client';

import * as React from 'react';
import Link from 'next/link';
import { useTranslations } from 'next-intl';
import { Maximize2Icon } from 'lucide-react';
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from '@/components/ui/sheet';
import { ChatSession } from './chat-session';
import { useChatController } from '@/core/app-shell/chat-controller';

/**
 * Accessible right-side overlay chat drawer built on shadcn/Radix Sheet primitives.
 * Closed by default, occupies full viewport on mobile and large responsive width on desktop.
 * Closing the presentation via Escape or close button does not cancel background response generation
 * or drop in-memory composer drafts.
 *
 * @returns Accessible ChatDrawer Sheet element.
 */
export function ChatDrawer() {
  const t = useTranslations('chat');
  const chatCtrl = useChatController();

  /**
   * Closes the drawer without canceling active generation or clearing in-memory drafts.
   */
  const handleOpenChange = React.useCallback(
    (open: boolean) => {
      if (!open) {
        chatCtrl.closeDrawer();
      }
    },
    [chatCtrl],
  );

  return (
    <Sheet open={chatCtrl.isDrawerOpen} onOpenChange={handleOpenChange}>
      <SheetContent
        side="right"
        closeLabel={t('closeDrawer')}
        className="p-0 flex flex-col h-full bg-background border-l border-border shadow-2xl focus:outline-none"
      >
        <SheetHeader className="sr-only">
          <SheetTitle>{t('quickChat')}</SheetTitle>
          <SheetDescription>{t('emptyStateDescription')}</SheetDescription>
        </SheetHeader>

        {/* Top bar with Open in full page link */}
        <div className="absolute top-3.5 right-12 z-20 flex items-center gap-1">
          <Link
            href="/chat"
            onClick={() => chatCtrl.closeDrawer()}
            className="p-1.5 rounded-md text-muted-foreground hover:text-foreground hover:bg-accent/10 transition-colors"
            title={t('openInFullPage')}
            aria-label={t('openInFullPage')}
          >
            <Maximize2Icon className="size-4" />
          </Link>
        </div>

        <div className="flex-1 min-h-0 flex flex-col">
          <ChatSession
            conversationId={chatCtrl.activeConversationId}
            onConversationCreated={(id) => chatCtrl.setActiveConversationId(id)}
            mode="drawer"
            onCloseDrawer={chatCtrl.closeDrawer}
          />
        </div>
      </SheetContent>
    </Sheet>
  );
}
