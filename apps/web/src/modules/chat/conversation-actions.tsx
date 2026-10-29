'use client';

import * as React from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslations } from 'next-intl';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog';
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { chatKeys, deleteConversation, patchConversation } from '@/modules/chat/api';
import { useChatController } from '@/core/app-shell/chat-controller';
import { useWorkspaceSession } from '@/core/app-shell/workspace-shell';

/** Openers returned by {@link useConversationActions}; `dialogs` must be rendered once by the owner. */
export interface ConversationActions {
  /** Opens the rename dialog for a conversation. */
  rename: (conversation: { id: string; title: string }) => void;
  /** Opens the delete confirmation for a conversation id. */
  remove: (id: string) => void;
  /** Rename and delete dialogs; mounted by the owner so they work whether or not the history list is mounted. */
  dialogs: React.ReactNode;
}

/**
 * Owns the rename and delete dialogs plus their mutations for conversations.
 * Lifted out of ChatHistory so the conversation header can use them while the history sidebar is collapsed.
 * Deleting the active conversation starts a new one; deleting always unbinds a day-conversation target.
 *
 * @param activeConversationId - Currently selected conversation, to detect deleting the active one.
 * @param onNewConversation - Invoked after the active conversation was deleted.
 * @returns Openers and the dialogs element to render once.
 */
export function useConversationActions(
  activeConversationId: string | null,
  onNewConversation: () => void,
): ConversationActions {
  const t = useTranslations('chat');
  const session = useWorkspaceSession();
  const queryClient = useQueryClient();
  const { unbindConversation } = useChatController();
  const [pendingDeleteId, setPendingDeleteId] = React.useState<string | null>(null);
  const [pendingRename, setPendingRename] = React.useState<{ id: string; title: string } | null>(null);
  const [renameValue, setRenameValue] = React.useState('');

  const deleteMutation = useMutation({
    mutationFn: (id: string) => deleteConversation(id, session.csrfToken),
    onSuccess: (_, deletedId) => {
      void queryClient.invalidateQueries({ queryKey: chatKeys.conversations() });
      // A deleted day conversation must stop being that day's drawer target.
      unbindConversation(deletedId);
      if (activeConversationId === deletedId) onNewConversation();
      setPendingDeleteId(null);
    },
  });
  const renameMutation = useMutation({
    mutationFn: ({ id, title }: { id: string; title: string }) => patchConversation(id, { title }, session.csrfToken),
    onSuccess: (updated) => {
      void queryClient.invalidateQueries({ queryKey: chatKeys.conversations() });
      void queryClient.invalidateQueries({ queryKey: chatKeys.conversation(updated.id) });
      setPendingRename(null);
    },
  });
  const resetDelete = deleteMutation.reset;
  const resetRename = renameMutation.reset;

  const rename = React.useCallback((conversation: { id: string; title: string }) => {
    resetRename();
    setRenameValue(conversation.title);
    setPendingRename(conversation);
  }, [resetRename]);
  const remove = React.useCallback((id: string) => {
    resetDelete();
    setPendingDeleteId(id);
  }, [resetDelete]);

  const dialogs = (
    <>
      <AlertDialog open={pendingDeleteId !== null} onOpenChange={(open) => { if (!open) setPendingDeleteId(null); }}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t('delete')}</AlertDialogTitle>
            <AlertDialogDescription>{t('deleteConfirm')}</AlertDialogDescription>
          </AlertDialogHeader>
          {deleteMutation.isError && <p role="alert" className="text-xs text-destructive">{t('deleteFailed')}</p>}
          <AlertDialogFooter>
            <AlertDialogCancel>{t('cancel')}</AlertDialogCancel>
            <AlertDialogAction
              onClick={(event) => {
                // Keep the dialog open so a failed delete shows its error and can be retried.
                event.preventDefault();
                if (pendingDeleteId && !deleteMutation.isPending) deleteMutation.mutate(pendingDeleteId);
              }}
              className="bg-destructive text-destructive-foreground hover:opacity-90"
            >
              {deleteMutation.isError ? t('retry') : t('delete')}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      <Dialog open={pendingRename !== null} onOpenChange={(open) => { if (!open) setPendingRename(null); }}>
        <DialogContent closeLabel={t('cancel')}>
          <DialogHeader>
            <DialogTitle>{t('renameConversation')}</DialogTitle>
            <DialogDescription className="sr-only">{t('renameLabel')}</DialogDescription>
          </DialogHeader>
          <form
            className="grid gap-3"
            onSubmit={(event) => {
              event.preventDefault();
              const title = renameValue.trim();
              if (pendingRename && title && !renameMutation.isPending) renameMutation.mutate({ id: pendingRename.id, title });
            }}
          >
            <Input value={renameValue} onChange={(event) => setRenameValue(event.target.value)} aria-label={t('renameLabel')} maxLength={200} />
            {renameMutation.isError && <p role="alert" className="text-xs text-destructive">{t('renameFailed')}</p>}
            <DialogFooter>
              <Button type="button" className="secondary" onClick={() => setPendingRename(null)}>{t('cancel')}</Button>
              <Button type="submit" disabled={!renameValue.trim() || renameMutation.isPending}>{t('renameSave')}</Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>
    </>
  );

  return { rename, remove, dialogs };
}
