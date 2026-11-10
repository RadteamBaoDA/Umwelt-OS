'use client';

import { useMutation, useQueryClient } from '@tanstack/react-query';
import { Trash2 } from 'lucide-react';
import { useRouter } from 'next/navigation';
import { useTranslations } from 'next-intl';
import React, { useState } from 'react';
import {
  AlertDialog,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog';
import { Button } from '@/components/ui/button';
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { RadioGroup, RadioGroupItem } from '@/components/ui/radio-group';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { useWorkspaceSession } from '@/core/app-shell/workspace-shell';
import { TopicSettings } from '@/modules/news';
import { HighlightRuleEditor } from '@/modules/settings';
import {
  createDashboard,
  createDashboardGroup,
  dashboardKeys,
  deleteDashboard,
  renameDashboard,
  type Dashboard,
} from './api';
import { setGroupFrame } from './group-frame';
import { LAYOUT_FRAMES, type LayoutFrame } from './layout-geometry';
import type { ManagementDialogState } from './use-dashboard-data';

/** Props of the single-field name dialog shared by create, rename and add-group. */
interface NameDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Heading; also used as the (visually redundant) description, as before. */
  title: string;
  /** DOM id pairing the label with the input. */
  inputId: string;
  label: string;
  placeholder?: string;
  value: string;
  onValueChange: (value: string) => void;
  /** Text of the primary action. */
  confirmLabel: string;
  confirmDisabled: boolean;
  /** Called with the trimmed value. */
  onConfirm: (value: string) => void;
  cancelLabel: string;
  /** Extra content rendered under the name field (e.g. layout frame choice). */
  children?: React.ReactNode;
}

/**
 * Dialog with one text field, a cancel button and a confirm button that submits the trimmed value.
 * The confirm button is disabled by the caller (blank value or pending mutation).
 */
export function NameDialog(props: NameDialogProps) {
  const { open, onOpenChange, title, inputId, label, placeholder, value, onValueChange } = props;
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          <DialogDescription className="text-xs text-muted-foreground">{title}</DialogDescription>
        </DialogHeader>
        <div className="space-y-2 py-2">
          <Label htmlFor={inputId} className="text-xs">{label}</Label>
          <Input
            id={inputId}
            value={value}
            onChange={(e) => onValueChange(e.target.value)}
            placeholder={placeholder}
            className="h-8 text-xs"
          />
          {props.children}
        </div>
        <DialogFooter className="gap-2">
          <Button type="button" className="secondary text-xs" onClick={() => onOpenChange(false)}>
            {props.cancelLabel}
          </Button>
          <Button
            type="button"
            disabled={props.confirmDisabled}
            className="text-xs font-semibold"
            onClick={() => props.onConfirm(value.trim())}
          >
            {props.confirmLabel}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

/** Destructive confirmation shown before deleting the active dashboard. */
export function DeleteDashboardDialog({ open, dashboardName, pending, onCancel, onConfirm }: {
  open: boolean;
  dashboardName: string;
  pending: boolean;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  const t = useTranslations('dashboard');
  return (
    <AlertDialog open={open}>
      <AlertDialogContent className="sm:max-w-md">
        <AlertDialogHeader>
          <AlertDialogTitle className="text-destructive flex items-center gap-2">
            <Trash2 className="w-4 h-4" />
            <span>{t('deleteDashboardTitle')}</span>
          </AlertDialogTitle>
          <AlertDialogDescription className="text-xs text-muted-foreground">
            {t('deleteDashboardConfirm', { name: dashboardName })}
          </AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter className="gap-2">
          <Button type="button" className="secondary text-xs" onClick={onCancel}>{t('cancel')}</Button>
          <Button
            type="button"
            disabled={pending}
            className="text-xs font-semibold bg-destructive text-destructive-foreground hover:bg-destructive/90"
            onClick={onConfirm}
          >
            {t('delete')}
          </Button>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}

/** Zone widths (grid units) drawn as bars in each frame's preview; `free` shows a single wide bar. */
const FRAME_PREVIEW: Record<LayoutFrame, number[]> = {
  free: [20], three_columns: [7, 7, 6], main_rail: [14, 6], hero_grid: [20, 7, 7, 6], board: [5, 5, 5, 5],
};

/**
 * Layout-frame radio group for a new page (with a small column-bar preview per option) plus template shortcuts.
 * The frame only shapes where gadgets are auto-placed; it never moves existing ones.
 */
function GroupFramePicker({ frame, onChange, onFromTemplate }: {
  frame: LayoutFrame;
  onChange: (frame: LayoutFrame) => void;
  onFromTemplate?: (presetId: string) => void;
}) {
  const t = useTranslations('dashboard');
  return (
    <div className="space-y-3 pt-2">
      <p className="text-xs font-medium">{t('groupFrameLabel')}</p>
      <RadioGroup value={frame} onValueChange={(value) => onChange(value as LayoutFrame)} className="grid grid-cols-1 gap-1.5">
        {LAYOUT_FRAMES.map((id) => (
          <Label key={id} htmlFor={`group-frame-${id}`} className="flex items-center gap-2 text-xs font-normal">
            <RadioGroupItem id={`group-frame-${id}`} value={id} />
            <span aria-hidden="true" className="flex h-5 w-16 shrink-0 flex-wrap content-start">
              {FRAME_PREVIEW[id].map((w, i) => (
                <span key={i} style={{ width: `${w * 5}%` }} className="h-2 border border-background bg-muted-foreground/40" />
              ))}
            </span>
            {t(`groupFrame_${id}`)}
          </Label>
        ))}
      </RadioGroup>
      {onFromTemplate && (
        <div className="space-y-1.5 border-t border-border pt-2">
          <p className="text-xs text-muted-foreground">{t('groupFromTemplate')}</p>
          <div className="flex flex-wrap gap-1.5">
            {(['news', 'markets', 'work'] as const).map((id) => (
              <Button key={id} type="button" size="sm" variant="outline" className="text-xs" onClick={() => onFromTemplate(id)}>
                {t(`groupTemplate_${id}`)}
              </Button>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

/** Topics / highlight-rules management dialog (owner only). */
export function ManagementDialog({ management }: { management: ManagementDialogState }) {
  const t = useTranslations('dashboard');
  const session = useWorkspaceSession();
  return (
    <Dialog open={management.open} onOpenChange={management.setOpen}>
      <DialogContent className="max-h-[90dvh] overflow-y-auto sm:max-w-4xl">
        <DialogHeader>
          <DialogTitle>{t('manageTopicsAndHighlights')}</DialogTitle>
          <DialogDescription>{t('manageTopicsAndHighlightsDescription')}</DialogDescription>
        </DialogHeader>
        <Tabs value={management.tab} onValueChange={(value) => management.setTab(value as 'topics' | 'rules')}>
          <TabsList aria-label={t('manageTopicsAndHighlights')}>
            <TabsTrigger value="topics" className="min-h-9 pointer-coarse:min-h-11">{t('manageTopics')}</TabsTrigger>
            <TabsTrigger value="rules" className="min-h-9 pointer-coarse:min-h-11">{t('manageHighlights')}</TabsTrigger>
          </TabsList>
          <TabsContent value="topics" className="pt-4"><TopicSettings csrfToken={session.csrfToken} /></TabsContent>
          <TabsContent value="rules" className="pt-4"><HighlightRuleEditor /></TabsContent>
        </Tabs>
      </DialogContent>
    </Dialog>
  );
}

/** Collaborators of the dashboard CRUD dialogs. */
interface DashboardCrudInputs {
  activeDashboard: Dashboard | null;
  dashboards: { id: string }[];
  layoutOperationPending: boolean;
  navigateToDashboard: (id: string, replace?: boolean) => void;
}

/**
 * Dialog state and mutations for creating, renaming and deleting dashboards and adding groups.
 *
 * Side effects (unchanged): create waits for the list refetch before navigating, because the URL-validity
 * effect replaces ids missing from a stale list with the first dashboard; rename/delete/add-group invalidate
 * the affected dashboard queries; delete navigates to the next dashboard or `/dashboard`.
 */
export function useDashboardCrud({ activeDashboard, dashboards, layoutOperationPending, navigateToDashboard }: DashboardCrudInputs) {
  const workspace = useWorkspaceSession();
  const queryClient = useQueryClient();
  const router = useRouter();
  const [createOpen, setCreateOpen] = useState<boolean>(false);
  const [createName, setCreateName] = useState<string>('');
  const [renameOpen, setRenameOpen] = useState<boolean>(false);
  const [renameValue, setRenameValue] = useState<string>('');
  const [deleteOpen, setDeleteOpen] = useState<boolean>(false);
  const [groupOpen, setGroupOpen] = useState<boolean>(false);
  const [groupName, setGroupName] = useState<string>('');
  const [groupFrame, setGroupFrameChoice] = useState<LayoutFrame>('free');

  const createMutation = useMutation({
    mutationFn: async (name: string) => createDashboard(name, workspace.csrfToken),
    /**
     * Refreshes the dashboard list, then selects the newly created dashboard. Navigation must wait for the refetch:
     * the URL-validity effect replaces ids missing from a stale list with the first dashboard (Overview).
     */
    onSuccess: async (newDash) => {
      await queryClient.invalidateQueries({ queryKey: dashboardKeys.all });
      navigateToDashboard(newDash.id, true);
      setCreateOpen(false);
      setCreateName('');
    },
  });
  const renameMutation = useMutation({
    mutationFn: async (name: string) => {
      if (!activeDashboard) return;
      if (layoutOperationPending) return;
      return renameDashboard(activeDashboard.id, name, activeDashboard.revision, workspace.csrfToken);
    },
    onSuccess: (renamed) => {
      if (renamed) {
        queryClient.setQueryData(dashboardKeys.detail(renamed.id), renamed);
        void queryClient.invalidateQueries({ queryKey: dashboardKeys.all });
      }
      setRenameOpen(false);
    },
  });
  const deleteMutation = useMutation({
    mutationFn: async () => {
      if (!activeDashboard) return;
      return deleteDashboard(activeDashboard.id, activeDashboard.revision, workspace.csrfToken);
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: dashboardKeys.all });
      setDeleteOpen(false);
      const fallback = dashboards.find((dashboard) => dashboard.id !== activeDashboard?.id);
      if (fallback) navigateToDashboard(fallback.id, true);
      else router.replace('/dashboard');
    },
  });
  const groupMutation = useMutation({
    mutationFn: async (name: string) => {
      if (!activeDashboard) return;
      return createDashboardGroup(activeDashboard.id, name, activeDashboard.revision, workspace.csrfToken, activeDashboard.groups.length);
    },
    onSuccess: (created) => {
      if (activeDashboard) {
        void queryClient.invalidateQueries({ queryKey: dashboardKeys.detail(activeDashboard.id) });
      }
      if (created) setGroupFrame(created.id, groupFrame);
      setGroupOpen(false);
      setGroupName('');
      setGroupFrameChoice('free');
    },
  });
  return {
    createOpen, setCreateOpen, createName, setCreateName, createMutation,
    renameOpen, setRenameOpen, renameValue, setRenameValue, renameMutation,
    deleteOpen, setDeleteOpen, deleteMutation,
    groupOpen, setGroupOpen, groupName, setGroupName, groupMutation, groupFrame, setGroupFrameChoice,
  };
}

/** Return type of {@link useDashboardCrud}. */
export type DashboardCrud = ReturnType<typeof useDashboardCrud>;

/**
 * Renders the create, rename, delete and add-group dialogs driven by {@link useDashboardCrud}.
 *
 * @param crud Dialog state and mutations.
 * @param activeDashboardName Name shown in the delete confirmation.
 * @param layoutOperationPending Blocks add-group while a layout operation is running.
 */
export function DashboardCrudDialogs({ crud, activeDashboardName, layoutOperationPending, onFromTemplate }: {
  crud: DashboardCrud;
  /** Opens the preset picker in append mode on the chosen purpose preset; hides the template row when absent. */
  onFromTemplate?: (presetId: string) => void;
  activeDashboardName: string;
  layoutOperationPending: boolean;
}) {
  const t = useTranslations('dashboard');
  const ts = useTranslations('uiSweep');
  return (
    <>
      <NameDialog
        open={crud.createOpen} onOpenChange={crud.setCreateOpen} title={t('createDashboardTitle')}
        inputId="create-name" label={t('dashboardName')} placeholder={ts('dpOverview')}
        value={crud.createName} onValueChange={crud.setCreateName} cancelLabel={t('cancel')}
        confirmLabel={t('create')} confirmDisabled={!crud.createName.trim() || crud.createMutation.isPending}
        onConfirm={(name) => crud.createMutation.mutate(name)}
      />
      <NameDialog
        open={crud.renameOpen} onOpenChange={crud.setRenameOpen} title={t('renameDashboardTitle')}
        inputId="rename-name" label={t('dashboardName')}
        value={crud.renameValue} onValueChange={crud.setRenameValue} cancelLabel={t('cancel')}
        confirmLabel={t('save')} confirmDisabled={!crud.renameValue.trim() || crud.renameMutation.isPending}
        onConfirm={(name) => crud.renameMutation.mutate(name)}
      />
      <DeleteDashboardDialog
        open={crud.deleteOpen} dashboardName={activeDashboardName} pending={crud.deleteMutation.isPending}
        onCancel={() => crud.setDeleteOpen(false)} onConfirm={() => crud.deleteMutation.mutate()}
      />
      <NameDialog
        open={crud.groupOpen} onOpenChange={crud.setGroupOpen} title={t('addGroupTitle')}
        inputId="new-group-name" label={t('groupName')} placeholder={ts('dpMarketFocus')}
        value={crud.groupName} onValueChange={crud.setGroupName} cancelLabel={t('cancel')}
        confirmLabel={t('create')}
        confirmDisabled={!crud.groupName.trim() || crud.groupMutation.isPending || layoutOperationPending}
        onConfirm={(name) => crud.groupMutation.mutate(name)}
      >
        <GroupFramePicker frame={crud.groupFrame} onChange={crud.setGroupFrameChoice} onFromTemplate={onFromTemplate} />
      </NameDialog>
    </>
  );
}
