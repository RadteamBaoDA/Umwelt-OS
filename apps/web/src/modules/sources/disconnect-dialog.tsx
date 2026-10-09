'use client';

import { useQuery, useQueryClient } from '@tanstack/react-query';
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
import { useDisplayPreferences } from '@/core/query-provider';
import { normalizeFormattingLocale } from '@/core/i18n';
import { getSourceImpact } from './api';

// Backend counts saturate at 1000; show that as "1000+".
const shownWith = (format: Intl.NumberFormat) => (n: number) => (n >= 1000 ? '1000+' : format.format(n));

/**
 * Confirms disconnecting a source with explicit consequence copy for keeping or deleting collected data.
 * Archived sources can only be purged. Impact counts load when the dialog opens; if the call fails the generic text is shown.
 */
export function DisconnectDialog({ sourceId, name, archived, disabled, trigger, onConfirm, onCloseAutoFocus }: {
  sourceId: string;
  name: string;
  archived: boolean;
  disabled?: boolean;
  trigger: ReactNode;
  onConfirm: (deleteData: boolean) => void;
  /** Lets the owner redirect focus when the trigger is busy or unmounted after the action. */
  onCloseAutoFocus?: (event: Event) => void;
}) {
  const t = useTranslations('sources');
  const { locale } = useDisplayPreferences();
  const shown = shownWith(new Intl.NumberFormat(normalizeFormattingLocale(locale)));
  const [choice, setChoice] = useState<'keep' | 'delete'>(archived ? 'delete' : 'keep');
  const [open, setOpen] = useState(false);
  const deleting = choice === 'delete';
  const queryClient = useQueryClient();
  const impact = useQuery({ queryKey: ['sources', sourceId, 'impact'], queryFn: ({ signal }) => getSourceImpact(sourceId, signal), enabled: open, retry: false });
  return <AlertDialog open={open} onOpenChange={(next) => { if (next && disabled) return; setOpen(next); if (!next) void queryClient.cancelQueries({ queryKey: ['sources', sourceId, 'impact'] }); if (next) setChoice(archived ? 'delete' : 'keep'); }}>
    <AlertDialogTrigger asChild>{trigger}</AlertDialogTrigger>
    <AlertDialogContent onCloseAutoFocus={onCloseAutoFocus}>
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
      <p className="muted" role="status">{impact.data ? t('disconnectImpact', { documents: shown(impact.data.document_count), gadgets: shown(impact.data.gadget_definition_count), placements: shown(impact.data.gadget_placement_count), chats: shown(impact.data.conversation_count) }) : impact.isFetching ? t('disconnectImpactLoading') : t('disconnectImpactUnknown')}</p>
      <AlertDialogFooter>
        <AlertDialogCancel>{t('cancel')}</AlertDialogCancel>
        <AlertDialogAction variant={deleting ? 'destructive' : undefined} onClick={() => onConfirm(deleting)}>
          {t(deleting ? 'disconnectDelete' : 'disconnectKeep')}
        </AlertDialogAction>
      </AlertDialogFooter>
    </AlertDialogContent>
  </AlertDialog>;
}
