'use client';

import { useQuery } from '@tanstack/react-query';
import { useTranslations } from 'next-intl';
import { useState, type ReactNode } from 'react';
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from '@/components/ui/alert-dialog';
import { Label } from '@/components/ui/label';
import { RadioGroup, RadioGroupItem } from '@/components/ui/radio-group';
import { getSourceImpact } from './api';

/**
 * Confirms disconnecting a source with explicit consequence copy for keeping or deleting collected data.
 * Archived sources can only be purged. Impact counts load when the dialog opens; if the call fails the generic text is shown.
 */
export function DisconnectDialog({ sourceId, name, archived, disabled, trigger, onConfirm }: {
  sourceId: string;
  name: string;
  archived: boolean;
  disabled?: boolean;
  trigger: ReactNode;
  onConfirm: (deleteData: boolean) => void;
}) {
  const t = useTranslations('sources');
  const [choice, setChoice] = useState<'keep' | 'delete'>(archived ? 'delete' : 'keep');
  const [open, setOpen] = useState(false);
  const deleting = choice === 'delete';
  const impact = useQuery({ queryKey: ['sources', sourceId, 'impact'], queryFn: ({ signal }) => getSourceImpact(sourceId, signal), enabled: open, retry: false });
  return <AlertDialog onOpenChange={(next) => { setOpen(next); if (next) setChoice(archived ? 'delete' : 'keep'); }}>
    <AlertDialogTrigger asChild disabled={disabled}>{trigger}</AlertDialogTrigger>
    <AlertDialogContent>
      <AlertDialogHeader>
        <AlertDialogTitle>{t(archived ? 'disconnectDeleteTitle' : 'disconnectTitle', { name })}</AlertDialogTitle>
        <AlertDialogDescription>{t('disconnectIntro')}</AlertDialogDescription>
      </AlertDialogHeader>
      <RadioGroup value={choice} onValueChange={(value) => setChoice(value === 'delete' ? 'delete' : 'keep')} aria-label={t('disconnectChoice')} className="grid gap-3">
        {!archived && <div className="flex items-start gap-3 rounded-md border border-border p-3">
          <RadioGroupItem id="disconnect-keep" value="keep" />
          <Label htmlFor="disconnect-keep" className="grid gap-1"><strong>{t('disconnectKeep')}</strong><span className="muted">{t('disconnectKeepConsequence')}</span></Label>
        </div>}
        <div className="flex items-start gap-3 rounded-md border border-border p-3">
          <RadioGroupItem id="disconnect-delete" value="delete" />
          <Label htmlFor="disconnect-delete" className="grid gap-1"><strong>{t('disconnectDelete')}</strong><span className="muted">{t('disconnectDeleteConsequence')}</span></Label>
        </div>
      </RadioGroup>
      <p className="muted" aria-live="polite">{impact.data ? t('disconnectImpact', { documents: impact.data.document_count, gadgets: impact.data.gadget_definition_count, placements: impact.data.gadget_placement_count, chats: impact.data.conversation_count }) : t('disconnectImpactUnknown')}</p>
      <AlertDialogFooter>
        <AlertDialogCancel>{t('cancel')}</AlertDialogCancel>
        <AlertDialogAction className={deleting ? 'bg-destructive text-destructive-foreground hover:bg-destructive/90' : undefined} onClick={() => onConfirm(deleting)}>
          {t(deleting ? 'disconnectDelete' : 'disconnectKeep')}
        </AlertDialogAction>
      </AlertDialogFooter>
    </AlertDialogContent>
  </AlertDialog>;
}
