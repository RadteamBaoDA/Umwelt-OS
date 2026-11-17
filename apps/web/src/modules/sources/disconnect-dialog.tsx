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
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip';
import { useDisplayPreferences } from '@/core/query-provider';
import { normalizeFormattingLocale } from '@/core/i18n';
import { getSourceImpact } from './api';

// Backend counts saturate at 1000; show that as "1000+".
const shownWith = (format: Intl.NumberFormat) => (n: number) => (n >= 1000 ? '1000+' : format.format(n));

type Translate = ReturnType<typeof useTranslations<'sources'>>;

/** Keep/delete radio cards; archived sources have no "keep" card because they can only be purged. */
function DisconnectChoice({
  t,
  archived,
  choice,
  onChoice,
}: {
  t: Translate;
  archived: boolean;
  choice: 'keep' | 'delete';
  onChoice: (choice: 'keep' | 'delete') => void;
}) {
  return (
    <RadioGroup
      value={choice}
      onValueChange={(value) => onChoice(value === 'delete' ? 'delete' : 'keep')}
      aria-label={t('disconnect.choice')}
      className="grid gap-3"
    >
      {!archived && (
        <div className="flex items-start gap-3 rounded-lg border border-border p-3 has-[[data-state=checked]]:border-primary has-[[data-state=checked]]:bg-accent">
          <RadioGroupItem id="disconnect-keep" value="keep" />
          <Label htmlFor="disconnect-keep" className="grid gap-1">
            <strong>{t('disconnect.keep')}</strong>
            <span className="text-muted-foreground">{t('disconnect.keepConsequence')}</span>
          </Label>
        </div>
      )}
      <div className="flex items-start gap-3 rounded-lg border border-border p-3 has-[[data-state=checked]]:border-primary has-[[data-state=checked]]:bg-accent">
        <RadioGroupItem id="disconnect-delete" value="delete" />
        <Label htmlFor="disconnect-delete" className="grid gap-1">
          <strong>{t('disconnect.delete')}</strong>
          <span className="text-muted-foreground">{t('disconnect.deleteConsequence')}</span>
        </Label>
      </div>
    </RadioGroup>
  );
}

type ImpactQuery = {
  data?: { document_count: number; gadget_definition_count: number; gadget_placement_count: number; conversation_count: number };
  isFetching: boolean;
};

/** Impact counts (items, gadgets, chats; saturated at "1000+") or a loading/unknown note while the impact query has no data. */
function ImpactSummary({ t, impact, shown }: { t: Translate; impact: ImpactQuery; shown: (n: number) => string }) {
  return (
    <>
      {impact.data ? (
        <ul className="m-0 flex list-disc flex-col gap-1 rounded-lg bg-muted py-3 pe-4 ps-8 text-sm">
          <li>{t('impact.items', { count: shown(impact.data.document_count) })}</li>
          <li>
            {t('impact.gadgets', {
              gadgets: shown(impact.data.gadget_definition_count),
              placements: shown(impact.data.gadget_placement_count),
            })}
          </li>
          <li>{t('impact.chats', { count: shown(impact.data.conversation_count) })}</li>
        </ul>
      ) : (
        <p className="text-muted-foreground m-0">{impact.isFetching ? t('disconnect.impactLoading') : t('disconnect.impactUnknown')}</p>
      )}
    </>
  );
}

/**
 * Confirms disconnecting a source with explicit consequence copy for keeping or deleting collected data.
 * Archived sources can only be purged. Impact counts load when the dialog opens; if the call fails the generic text is shown.
 */
export function DisconnectDialog({
  sourceId,
  name,
  archived,
  disabled,
  trigger,
  triggerLabel,
  onConfirm,
  onCloseAutoFocus,
}: {
  sourceId: string;
  name: string;
  archived: boolean;
  disabled?: boolean;
  trigger: ReactNode;
  triggerLabel?: string;
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
  const impact = useQuery({
    queryKey: ['sources', sourceId, 'impact'],
    queryFn: ({ signal }) => getSourceImpact(sourceId, signal),
    enabled: open,
    retry: false,
  });
  return (
    <AlertDialog
      open={open}
      onOpenChange={(next) => {
        if (next && disabled) return;
        setOpen(next);
        if (!next) void queryClient.cancelQueries({ queryKey: ['sources', sourceId, 'impact'] });
        if (next) setChoice(archived ? 'delete' : 'keep');
      }}
    >
      {triggerLabel ? (
        <Tooltip>
          <AlertDialogTrigger asChild>
            <TooltipTrigger asChild>{trigger}</TooltipTrigger>
          </AlertDialogTrigger>
          <TooltipContent>{triggerLabel}</TooltipContent>
        </Tooltip>
      ) : (
        <AlertDialogTrigger asChild>{trigger}</AlertDialogTrigger>
      )}
      <AlertDialogContent onCloseAutoFocus={onCloseAutoFocus}>
        <AlertDialogHeader>
          <AlertDialogTitle>{t(archived ? 'disconnect.deleteTitle' : 'disconnect.title', { name })}</AlertDialogTitle>
          <AlertDialogDescription>{t('disconnect.intro')}</AlertDialogDescription>
        </AlertDialogHeader>
        <DisconnectChoice t={t} archived={archived} choice={choice} onChoice={setChoice} />
        <div role="status">
          <ImpactSummary t={t} impact={impact} shown={shown} />
        </div>
        <AlertDialogFooter>
          <AlertDialogCancel>{t('cancel')}</AlertDialogCancel>
          <AlertDialogAction variant={deleting ? 'destructive' : undefined} onClick={() => onConfirm(deleting)}>
            {t(deleting ? 'disconnect.delete' : 'disconnect.keep')}
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}
