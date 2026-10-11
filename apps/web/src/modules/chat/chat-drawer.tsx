'use client';

import * as React from 'react';
import { useTranslations } from 'next-intl';
import { XIcon } from 'lucide-react';
import { Button } from '@/components/ui/button';
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetClose,
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
        showCloseButton={false}
        className="p-0 flex flex-col h-full w-full sm:w-[65vw] sm:max-w-[56rem] md:max-w-[56rem] lg:max-w-[56rem] xl:max-w-[56rem] bg-surface border-l border-border shadow-2xl focus:outline-none"
      >
        <SheetHeader className="sr-only">
          <SheetTitle>{t('quickChat')}</SheetTitle>
          <SheetDescription>{t('emptyStateDescription')}</SheetDescription>
        </SheetHeader>

        {/* 44px ghost close (QuickChat.dc.html); SheetClose keeps Radix focus return and Escape handling. */}
        <SheetClose asChild>
          <Button type="button" variant="ghost" size="icon" className="absolute right-3 top-2 z-10 size-11" aria-label={t('closeDrawer')}>
            <XIcon className="size-5" aria-hidden="true" />
            <span className="sr-only">{t('closeDrawer')}</span>
          </Button>
        </SheetClose>

        <div className="flex-1 min-h-0 flex flex-col">
          <ChatSession
            // Remount per day so in-flight stream UI of another day's run never shows here; the run continues server-side.
            key={chatCtrl.context?.kind === 'day' ? `${chatCtrl.context.date}|${chatCtrl.context.timezone}` : 'general'}
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
