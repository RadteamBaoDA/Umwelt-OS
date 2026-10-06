'use client';

import { useCallback, useEffect, useState } from 'react';
import { useTranslations } from 'next-intl';
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { StoryDetail } from '@/modules/news/story-detail';
import { DocumentDialogBody } from './document-dialog-body';
import { EntityDialogBody } from './entity-dialog-body';

export type DetailTarget = {
  kind: 'story' | 'entity' | 'document';
  id: string;
  /** Gadget that opened the dialog; reserved for focus return and "Open in" links. */
  sourceGadget?: string;
  /** Required for kind 'story': the visible source ids that authorize the snapshot. */
  sourceIds?: string[];
};

export type DetailDialogProps = {
  target: DetailTarget | null;
  onClose: () => void;
  /** Forwarded to DialogContent so callers can restore focus to the opener (call event.preventDefault() first). */
  onCloseAutoFocus?: (event: Event) => void;
};

/**
 * Shared detail host. Controlled: open while `target` is set; `story`, `entity` and `document` render.
 * Opening a related entity from inside replaces the dialog content and Back returns to the previous view
 * (a stack local to the opened target; closing clears it). Unsupported targets (story without `sourceIds`)
 * render nothing and call `onClose` once so the caller clears `target`; callers must clear it in `onClose`.
 */
export function DetailDialog({ target, onClose, onCloseAutoFocus }: DetailDialogProps) {
  const t = useTranslations('detail');
  const [pushed, setPushed] = useState<{ owner: DetailTarget | null; stack: DetailTarget[] }>({ owner: null, stack: [] });
  // The stack belongs to the target that opened it; a new or cleared target starts from an empty stack.
  const stack = pushed.owner === target ? pushed.stack : [];
  const current = stack.length ? stack[stack.length - 1] : target;
  const supported = current !== null && (current.kind !== 'story' || Boolean(current.sourceIds?.length));
  const unsupported = target !== null && !supported;
  useEffect(() => { if (unsupported) onClose(); }, [unsupported, onClose]);
  const openEntity = useCallback((id: string) => setPushed({ owner: target, stack: [...stack, { kind: 'entity', id }] }), [target, stack]);
  const goBack = useCallback(() => setPushed({ owner: target, stack: stack.slice(0, -1) }), [target, stack]);
  const kind = current?.kind ?? 'story';
  return (
    <Dialog open={supported} onOpenChange={(open) => { if (!open) onClose(); }}>
      <DialogContent
        closeLabel={t('close')}
        onCloseAutoFocus={onCloseAutoFocus}
        className="grid-rows-[auto_minmax(0,1fr)] gap-0 h-[calc(100dvh-1rem)] max-h-[calc(100dvh-1rem)] w-[calc(100vw-1rem)] max-w-4xl overflow-hidden p-0 sm:h-[85dvh] sm:max-h-[85dvh] sm:w-[calc(100vw-2rem)] sm:max-w-4xl"
      >
        <DialogHeader className="border-b border-border px-4 py-3 text-left">
          <DialogTitle>{t(`title_${kind}`)}</DialogTitle>
          <DialogDescription>{t(`description_${kind}`)}</DialogDescription>
        </DialogHeader>
        <div className="min-h-0 flex-1 overflow-hidden">
          {current?.kind === 'story' && supported && <div className="h-full p-3 sm:p-4"><StoryDetail storyId={current.id} sourceIds={current.sourceIds ?? []} onBack={stack.length ? goBack : onClose} /></div>}
          {current?.kind === 'entity' && <EntityDialogBody key={current.id} entityId={current.id} canGoBack={stack.length > 0} onBack={goBack} onOpenEntity={openEntity} />}
          {current?.kind === 'document' && <DocumentDialogBody key={current.id} documentId={current.id} />}
        </div>
      </DialogContent>
    </Dialog>
  );
}
