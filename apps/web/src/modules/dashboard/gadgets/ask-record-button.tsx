'use client';

import { useTranslations } from 'next-intl';
import { Button } from '@/components/ui/button';
import { useChatController } from '@/core/app-shell/chat-controller';
import { useWorkspace } from '@/core/workspace-context';

/** Document identity of one dashboard record; all three ids are required to anchor a chat on it. */
export interface RecordIdentity {
  sourceId: string;
  documentId: string;
  documentVersionId: string;
}

/**
 * Shared per-record "Ask AI" action. Opens the chat drawer with a one-item selection flagged `anchor: true`, so the
 * server resolves that exact document version (with its fence and local-only checks) and keeps it as evidence while
 * normal workspace retrieval stays on. Visible only to the workspace owner unless `allowNonOwner` is set (the feed's
 * Telegram exception); the server re-checks ownership and policy before any model call.
 *
 * @param props.record Identity of the record; sent verbatim as the single context item.
 * @param props.allowNonOwner Show the button to non-owners too (only the Telegram feed uses this).
 * @param props.className Extra classes for layout by the enclosing row.
 * @returns The ghost button, or `null` when the viewer may not ask about records.
 */
export function AskRecordButton({
  record,
  allowNonOwner = false,
  className = 'h-6 px-2 text-xs',
}: {
  record: RecordIdentity;
  allowNonOwner?: boolean;
  className?: string;
}) {
  const t = useTranslations('dashboard');
  const { openDrawer } = useChatController();
  const { isOwner } = useWorkspace();
  if (!allowNonOwner && !isOwner) return null;
  return (
    <Button
      type="button"
      variant="ghost"
      size="sm"
      className={className}
      onClick={() => openDrawer({ context: { kind: 'selection', anchor: true, items: [record] } })}
    >
      {t('askAbout.message')}
    </Button>
  );
}
