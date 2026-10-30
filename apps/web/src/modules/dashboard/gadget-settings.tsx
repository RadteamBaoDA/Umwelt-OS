'use client';

import { useQueryClient } from '@tanstack/react-query';
import { Settings } from 'lucide-react';
import { useTranslations } from 'next-intl';
import { useId, useState } from 'react';
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { useWorkspaceSession } from '@/core/app-shell/workspace-shell';
import { dashboardKeys, patchGadgetInstance, type GadgetInstance } from './api';
import { GadgetEditor } from './gadget-editor';
import { rendererName } from '@/core/renderer-labels';

/** Props for the GadgetSettings modal dialog. */
export interface GadgetSettingsProps {
  /** Controls open state of the settings modal. */
  open: boolean;
  /** Callback fired when open state changes. */
  onOpenChange: (open: boolean) => void;
  /** Target gadget instance to inspect and edit. */
  instance: GadgetInstance | null;
  /** ID of the dashboard hosting this gadget instance. */
  dashboardId: string;
  /** Shared revision of the dashboard for instance updates. */
  dashboardRevision: number;
  /** Optional callback fired when gadget settings are saved successfully. */
  onSaved?: () => void;
}

/**
 * Modal dialog for configuring one gadget instance: its title plus the shared GadgetEditor for the reusable
 * definition. Saving patches the definition first, then the instance title (only when it changed). A failed title
 * patch surfaces as the editor's save error and a retry reuses the already-saved definition revision.
 *
 * @param props Configuration and state handlers for the settings dialog.
 * @returns Radix Dialog element, or null while no instance is selected.
 */
export function GadgetSettings({
  open,
  onOpenChange,
  instance,
  dashboardId,
  dashboardRevision,
  onSaved,
}: GadgetSettingsProps) {
  const t = useTranslations('dashboard');
  const tg = useTranslations('gadgetSettings');
  const titleId = useId();
  const session = useWorkspaceSession();
  const queryClient = useQueryClient();
  const [title, setTitle] = useState<string>(instance?.title ?? '');
  // Reset the draft title whenever a different instance is opened (render-phase reset, no effect needed).
  const [titleFor, setTitleFor] = useState<string | undefined>(instance?.id);
  if (instance?.id !== titleFor) { setTitleFor(instance?.id); setTitle(instance?.title ?? ''); }
  if (!instance) return null;

  /** Persists the instance title after the definition saved, then refreshes caches and closes the dialog. */
  const handleSaved = async () => {
    if (title.trim() !== (instance.title || '')) {
      await patchGadgetInstance(dashboardId, instance.id, dashboardRevision, { title: title.trim() || null }, session.csrfToken);
    }
    void queryClient.invalidateQueries({ queryKey: dashboardKeys.detail(dashboardId) });
    void queryClient.invalidateQueries({ queryKey: dashboardKeys.definitions });
    onOpenChange(false);
    onSaved?.();
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="flex max-h-[85vh] flex-col gap-0 overflow-hidden p-0 sm:max-w-xl">
        <DialogHeader className="border-b border-border p-4">
          <div className="flex items-center gap-2">
            <Settings className="size-4 text-primary" aria-hidden="true" />
            <DialogTitle className="text-base font-bold">{t('configureGadgetTitle')}</DialogTitle>
          </div>
          <DialogDescription className="text-xs text-muted-foreground">
            {tg('configureDescription', { renderer: rendererName(tg, instance.definition.renderer) })}
          </DialogDescription>
        </DialogHeader>
        <div className="flex-1 space-y-4 overflow-y-auto p-4">
          <GadgetEditor key={`${instance.id}:${instance.definition.revision}`} definition={instance.definition} onSaved={handleSaved}
            onCancel={() => onOpenChange(false)} saveLabel={t('save')}
            afterName={<div className="space-y-1.5">
            <Label htmlFor={titleId} className="text-xs font-semibold">{t('instanceTitle')}</Label>
            <Input id={titleId} value={title} onChange={(event) => setTitle(event.target.value)} aria-describedby={`${titleId}-help`}
              placeholder={instance.definition.name || instance.definition.renderer} className="h-8 text-xs" />
            <p id={`${titleId}-help`} className="text-xs text-muted-foreground">{t('instanceTitleHelp')}</p>
          </div>}
          />
        </div>
      </DialogContent>
    </Dialog>
  );
}
