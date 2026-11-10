'use client';

import {
  AlertCircle,
  Check,
  FolderPlus,
  LayoutTemplate,
  Plus,
  Redo2,
  Undo2,
  X,
} from 'lucide-react';
import { useTranslations } from 'next-intl';
import React, { useCallback, useRef, useState } from 'react';
import {
  AlertDialog,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog';
import { Button } from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { useWorkspaceSession } from '@/core/app-shell/workspace-shell';
import { deleteGadgetDefinition, type DashboardGroup, type GadgetDefinition } from './api';
import { GadgetEditor } from './gadget-editor';
import { BareButton } from '@/components/bare-button';

/** Props for the LayoutEditor toolbar and dirty confirmation workflow. */
export interface LayoutEditorProps {
  /** Name of the dashboard being edited. */
  dashboardName: string;
  /** Number of edits in the undo history since the saved baseline. */
  editCount: number;
  /** Saved layout revision this draft started from. */
  baseRevision?: number | null;
  /** Whether layout draft has unsaved changes. */
  isDirty: boolean;
  /** Whether save layout mutation is currently in flight. */
  isSaving: boolean;
  /** Whether undo history has prior states available. */
  canUndo: boolean;
  /** Whether redo history has future states available. */
  canRedo: boolean;
  /** Callback fired to persist the current draft layout. */
  onSave: () => void;
  /** Callback fired to cancel layout editing and discard draft changes. */
  onCancel: () => void;
  /** Callback fired to revert to previous layout snapshot. */
  onUndo: () => void;
  /** Callback fired to re-apply undone layout snapshot. */
  onRedo: () => void;
  /** Packs the draft top-left without holes; omitted hides the action. */
  onTidyLayout?: () => void;
  /** False when tidying would not move any gadget. */
  canTidyLayout?: boolean;
  /** Optional explicit adoption of the current local readable mobile projection. */
  onUseReadableMobileSizes?: () => void;
  /** True only after a complete measured readable projection is available. */
  canUseReadableMobileSizes?: boolean;
  /** Opens the Add group dialog owned by the dashboard page; the button is hidden when omitted. */
  onAddGroup?: () => void;
  /** Opens the picker immediately when an empty panel enters Edit mode through Add gadget. */
  openAddInitially?: boolean;
  /** Template id (from an add-gadget deep link) preselected in the Add dialog; ignored when unknown. */
  initialTemplateId?: string;
  /** Source id preselected with the template; ignored when not among the loaded sources. */
  initialSourceId?: string;
  /** Callback fired when user opens the preset picker dialog. */
  onOpenPresetPicker: () => void;
  /** Adds a definition as a gadget instance; resolves true only on success (false or a throw means failure). */
  onAddGadgetInstance: (params: {
    definitionId: string;
    groupId?: string;
    title?: string;
    renderer: string;
  }, reservationId: string) => Promise<boolean>;
  /** Reserves the owner layout before any asynchronous definition creation begins. */
  onBeginAddGadgetInstance: (renderer: string) => string | null;
  /** Releases a reservation when instance creation fails. */
  onCancelPendingAdd: (reservationId: string) => void;
  /** Available groups on the active dashboard. */
  groups: DashboardGroup[];
  /** Currently selected group identifier. */
  activeGroupId?: string;
  /** Controls open state of the Save / Discard / Stay modal. */
  dirtyModalOpen: boolean;
  /** Origin-specific text when a pending layout switch is caused by a viewport breakpoint. */
  dirtyDescription?: string;
  /** Callback handling dirty modal resolution (save, discard, or stay). */
  onDirtyModalResolution: (action: 'save' | 'discard' | 'stay') => void;
  /** Optional error message displayed if the last save attempt failed. */
  saveError?: string | null;
  /** Callback to clear the save error notification. */
  onDismissSaveError?: () => void;
}

/** Add-flow inputs taken from the editor props. */
type AddFlowProps = Pick<LayoutEditorProps, 'activeGroupId' | 'onAddGadgetInstance' | 'onBeginAddGadgetInstance' | 'onCancelPendingAdd' | 'openAddInitially'>;

/**
 * State and behaviour of the Add gadget dialog: target group, error text and the definition-to-instance placement.
 *
 * Placement is single-flight: while one add runs, a second definition created for a concurrent call is deleted
 * again (never left orphaned) and the dialog cannot be closed. Failures leave the dialog open with a localized error.
 */
function useAddGadgetFlow({ activeGroupId, onAddGadgetInstance, onBeginAddGadgetInstance, onCancelPendingAdd, openAddInitially }: AddFlowProps) {
  const session = useWorkspaceSession();
  const tg = useTranslations('gadgetSettings');
  const [addDialogOpen, setAddDialogOpen] = useState<boolean>(Boolean(openAddInitially));
  const isAddingRef = useRef(false);
  const [addError, setAddError] = useState<string | null>(null);

  /**
   * Places the newly-created definition on the dashboard and deletes it if reservation or instance creation fails.
   * Never throws; failures leave the add dialog open with a localized error.
   */
  const addDefinition = useCallback(async (definition: Pick<GadgetDefinition, 'id' | 'renderer' | 'revision'>) => {
    // The reservation and the selected panel must name the same group.
    const finalGroupId = activeGroupId;
    /** Rolls back partially applied layout changes after a failed save. */
    const compensate = async () => {
      try {
        await deleteGadgetDefinition(definition.id, definition.revision, session.csrfToken);
      } catch {
        // Best effort: deletion can fail independently after the dashboard placement has failed.
      }
    };
    if (isAddingRef.current) {
      // A concurrent add owns the dialog; a definition created for this call must not be left orphaned.
      await compensate();
      return;
    }
    const reservationId = onBeginAddGadgetInstance(definition.renderer);
    if (!reservationId) {
      // No free layout space: keep the dialog open with a visible reason instead of closing silently.
      await compensate();
      setAddError(tg('addFailed'));
      return;
    }
    isAddingRef.current = true;
    setAddError(null);
    try {
      const ok = await onAddGadgetInstance({ definitionId: definition.id, groupId: finalGroupId, title: undefined, renderer: definition.renderer }, reservationId);
      if (ok) {
        setAddDialogOpen(false);
      } else {
        // The handler already released the reservation and set the page banner with the server message.
        await compensate();
        setAddError(tg('addFailed'));
      }
    } catch {
      onCancelPendingAdd(reservationId);
      await compensate();
      setAddError(tg('addFailed'));
    } finally {
      isAddingRef.current = false;
    }
  }, [activeGroupId, onAddGadgetInstance, onBeginAddGadgetInstance, onCancelPendingAdd, session.csrfToken, tg]);

  /** Opens the dialog with a cleared error. */
  const openAddDialog = useCallback(() => { setAddError(null); setAddDialogOpen(true); }, []);
  /** Applies a dialog open-state change unless an add is running (the dialog cannot close mid-flight). */
  const setOpenUnlessAdding = useCallback((open: boolean) => { if (!isAddingRef.current) setAddDialogOpen(open); }, []);
  return { addDialogOpen, openAddDialog, setOpenUnlessAdding, addError, addDefinition };
}

/** Top edit toolbar (DashboardEdit artboard): title + edit state left, explicit Cancel / Save right. */
function EditToolbar(props: LayoutEditorProps & { onOpenAdd: () => void }) {
  const t = useTranslations('dashboard');
  const { dashboardName, editCount, baseRevision, isDirty, isSaving, canUndo, canRedo, canTidyLayout = false, canUseReadableMobileSizes = false } = props;
  return (
    <div className="flex flex-wrap items-center gap-2 rounded-lg border border-border bg-card px-4 py-3">
      <div className="mr-auto flex min-w-0 flex-col">
        <strong className="text-[15px] font-semibold text-foreground">{t('editingTitle', { name: dashboardName })}</strong>
        <span role="status" className="flex items-center text-xs text-muted-foreground">
          <span className={`mr-1.5 inline-block size-2 rounded-full ${isDirty ? 'bg-primary' : 'bg-border'}`} aria-hidden="true" />
          {isDirty ? <>{t('unsavedChanges')} {'·'} </> : null}
          {t('editCount', { count: editCount })}
          {baseRevision != null ? <> {'·'} {t('draftOf', { revision: baseRevision })}</> : null}
        </span>
      </div>

      <Button type="button" variant="outline" disabled={!canUndo || isSaving} onClick={props.onUndo}>
        <Undo2 className="size-4" aria-hidden="true" />{t('undo')}
      </Button>
      <Button type="button" variant="outline" disabled={!canRedo || isSaving} onClick={props.onRedo}>
        <Redo2 className="size-4" aria-hidden="true" />{t('redo')}
      </Button>
      <span aria-hidden="true" className="mx-1 h-7 w-px bg-border" />
      {props.onTidyLayout && (
        <Button type="button" variant="outline" disabled={!canTidyLayout || isSaving} onClick={props.onTidyLayout}>
          {t('tidyLayout')}
        </Button>
      )}
      {props.onUseReadableMobileSizes && (
        <Button type="button" variant="outline" disabled={!canUseReadableMobileSizes || isSaving} onClick={props.onUseReadableMobileSizes}>
          {t('useReadableMobileSizes')}
        </Button>
      )}
      <Button type="button" variant="outline" disabled={isSaving} onClick={props.onOpenAdd}>
        <Plus className="size-4" aria-hidden="true" />{t('addGadget')}
      </Button>
      {props.onAddGroup && (
        <Button type="button" variant="outline" disabled={isSaving} onClick={props.onAddGroup}>
          <FolderPlus className="size-4" aria-hidden="true" />{t('addGroup')}
        </Button>
      )}
      <Button type="button" variant="outline" disabled={isSaving} onClick={props.onOpenPresetPicker}>
        <LayoutTemplate className="size-4" aria-hidden="true" />{t('presets')}
      </Button>
      <span aria-hidden="true" className="mx-1 h-7 w-px bg-border" />
      <Button type="button" variant="outline" disabled={isSaving} onClick={props.onCancel}>
        {t('cancelEdit')}
      </Button>
      <Button type="button" className="font-semibold" disabled={isSaving || !isDirty} onClick={props.onSave}>
        {isSaving ? t('saving') : <><Check className="size-4" aria-hidden="true" />{t('saveLayout')}</>}
      </Button>
    </div>
  );
}

/** Legend of the grid guides plus the keyboard hint. */
function GridLegend() {
  const t = useTranslations('dashboard');
  return (
    <ul aria-label={t('gridLegend')} className="m-0 flex list-none flex-wrap items-center gap-x-4 gap-y-1 p-0 text-xs text-muted-foreground">
      <li className="flex items-center gap-1.5"><span aria-hidden="true" className="h-3 w-3 rounded-sm border border-border" />{t('legendFree')}</li>
      <li className="flex items-center gap-1.5"><span aria-hidden="true" className="h-3 w-3 rounded-sm border border-primary bg-primary/15" />{t('legendDrop')}</li>
      <li className="flex items-center gap-1.5"><span aria-hidden="true" className="h-3 w-3 rounded-sm border border-destructive bg-destructive/15" />{t('legendOverlap')}</li>
      <li>{t('keyboardHelp')}</li>
    </ul>
  );
}

/** Save error alert banner with an optional dismiss button. */
function SaveErrorBanner({ message, onDismiss }: { message: string; onDismiss?: () => void }) {
  return (
    <div className="mb-4 p-3 rounded-lg bg-destructive/10 border border-destructive/30 text-destructive text-xs flex items-center justify-between">
      <div className="flex items-center gap-2">
        <AlertCircle className="w-4 h-4 shrink-0" />
        <span className="font-medium">{message}</span>
      </div>
      {onDismiss && (
        <BareButton
          type="button"
          onClick={onDismiss}
          className="p-1 hover:bg-destructive/20 rounded"
        >
          <X className="w-3.5 h-3.5" />
        </BareButton>
      )}
    </div>
  );
}

/** Dirty switch warning modal (Save / Discard / Stay). */
function DirtyModal({ open, description, isSaving, onResolve }: {
  open: boolean;
  description?: string;
  isSaving: boolean;
  onResolve: LayoutEditorProps['onDirtyModalResolution'];
}) {
  const t = useTranslations('dashboard');
  return (
    <AlertDialog open={open}>
      <AlertDialogContent className="sm:max-w-md">
        <AlertDialogHeader>
          <AlertDialogTitle className="flex items-center gap-2 text-foreground">
            <AlertCircle className="w-5 h-5 text-muted-foreground" />
            <span>{t('dirtyWarningTitle')}</span>
          </AlertDialogTitle>
          <AlertDialogDescription className="text-muted-foreground text-sm">
            {description ?? t('dirtyWarningDesc')}
          </AlertDialogDescription>
        </AlertDialogHeader>

        <AlertDialogFooter className="flex-col sm:flex-row gap-2 mt-4">
          <Button type="button" disabled={isSaving} className="secondary w-full sm:w-auto text-xs" onClick={() => onResolve('stay')}>
            {t('stayOnPage')}
          </Button>
          <Button type="button" disabled={isSaving} className="secondary text-destructive hover:bg-destructive/10 w-full sm:w-auto text-xs" onClick={() => onResolve('discard')}>
            {t('discardChanges')}
          </Button>
          <Button type="button" disabled={isSaving} className="w-full sm:w-auto text-xs font-semibold" onClick={() => onResolve('save')}>
            {t('saveAndContinue')}
          </Button>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}

/** Add gadget dialog for the currently selected panel, using the shared gadget editor. */
function AddGadgetDialog({ flow, initialTemplateId, initialSourceId }: { flow: ReturnType<typeof useAddGadgetFlow>; initialTemplateId?: string; initialSourceId?: string }) {
  const t = useTranslations('dashboard');
  const tg = useTranslations('gadgetSettings');
  return (
    <Dialog open={flow.addDialogOpen} onOpenChange={flow.setOpenUnlessAdding}>
      <DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle>{t('addGadgetTitle')}</DialogTitle>
          <DialogDescription className="text-xs text-muted-foreground">
            {tg('addDescription')}
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4 py-2">
          {/* Save creates the definition, then places its instance; failure removes the unused new definition. */}
          <GadgetEditor
            onSaved={flow.addDefinition}
            onCancel={() => flow.setOpenUnlessAdding(false)}
            saveLabel={t('addGadget')}
            initialTemplateId={initialTemplateId}
            initialSourceId={initialSourceId}
          />
          {flow.addError && <p role="alert" className="text-xs text-destructive">{flow.addError}</p>}
        </div>
      </DialogContent>
    </Dialog>
  );
}

/**
 * Toolbar and dirty navigation guard for dashboard layout editing.
 * Provides explicit Save/Cancel/Undo/Redo controls, optional readable-size adoption, and the Add dialog (template editor or library pick).
 * Shows a Save / Discard / Stay modal before discarding unsaved layout changes,
 * and retains draft state upon failed save attempts.
 *
 * @param props Toolbar state flags and action callbacks.
 * @returns Accessible toolbar and owner decision dialogs.
 */
export function LayoutEditor(props: LayoutEditorProps) {
  const flow = useAddGadgetFlow(props);
  return (
    <>
      {/* Top Edit Toolbar (DashboardEdit artboard): title + edit state left, explicit Cancel / Save right */}
      <EditToolbar {...props} onOpenAdd={flow.openAddDialog} />
      <GridLegend />

      {/* Save Error Alert Banner */}
      {props.saveError && <SaveErrorBanner message={props.saveError} onDismiss={props.onDismissSaveError} />}

      {/* Dirty Switch Warning Modal (Save / Discard / Stay) */}
      <DirtyModal open={props.dirtyModalOpen} description={props.dirtyDescription} isSaving={props.isSaving} onResolve={props.onDirtyModalResolution} />

      {/* Add Gadget Dialog */}
      <AddGadgetDialog flow={flow} initialTemplateId={props.initialTemplateId} initialSourceId={props.initialSourceId} />
    </>
  );
}
