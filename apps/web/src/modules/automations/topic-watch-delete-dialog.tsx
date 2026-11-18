'use client';

import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslations } from 'next-intl';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { Label } from '@/components/ui/label';
import { countSourceDocuments, deleteAutomation, errorMessageKey, ITEM_COUNT_CAP, type Automation } from './api';

/**
 * Confirm dialog for deleting a Topic watch. The "Also delete the collected items" checkbox is unchecked by default
 * (items stay searchable and the source is archived); its label shows the item count read from the source's documents
 * ("100+" at the read cap). Confirming issues one revision-fenced DELETE with `purge_items` true or false, then
 * invalidates the rule list, calls `onDeleted` and `onClose`. A failure shows a localized message and keeps the dialog.
 */
export function TopicWatchDeleteDialog({
  rule,
  csrfToken,
  onDeleted,
  onClose,
}: {
  rule: Automation;
  csrfToken: string;
  onDeleted?: () => void;
  onClose: () => void;
}) {
  const t = useTranslations('automations');
  const client = useQueryClient();
  const [purge, setPurge] = useState(false);
  const [failed, setFailed] = useState<ReturnType<typeof errorMessageKey> | null>(null);
  const sourceId = rule.actions.find((item) => item.type === 'collect_source')?.source_id ?? null;
  const count = useQuery({
    queryKey: ['topic-watch-item-count', sourceId],
    queryFn: () => countSourceDocuments(sourceId as string),
    enabled: sourceId !== null,
    retry: false,
  });
  const remove = useMutation({
    mutationFn: () => deleteAutomation(rule.id, rule.revision, csrfToken, purge),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ['automations'] });
      onDeleted?.();
      onClose();
    },
    onError: (error) => setFailed(errorMessageKey(error, 'action.failed')),
  });
  const shown = count.data ? (count.data.capped ? `${ITEM_COUNT_CAP}+` : String(count.data.count)) : '…';
  return (
    <Dialog open onOpenChange={(open) => !open && !remove.isPending && onClose()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{t('scheduledTasks.topic.deleteTitle', { name: rule.name })}</DialogTitle>
          <DialogDescription>{t('scheduledTasks.topic.deleteBody')}</DialogDescription>
        </DialogHeader>
        <div className="flex items-center gap-2">
          <Checkbox id="topic-purge" checked={purge} onCheckedChange={(value) => setPurge(value === true)} />
          <Label htmlFor="topic-purge">{t('scheduledTasks.topic.deletePurge', { count: shown })}</Label>
        </div>
        {failed && (
          <p role="alert" className="text-sm text-destructive">
            {t(failed)}
          </p>
        )}
        <DialogFooter>
          <Button type="button" variant="outline" disabled={remove.isPending} onClick={onClose}>
            {t('scheduledTasks.cancel')}
          </Button>
          <Button type="button" variant="destructive" disabled={remove.isPending} onClick={() => remove.mutate()}>
            {t('scheduledTasks.topic.deleteConfirm')}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
