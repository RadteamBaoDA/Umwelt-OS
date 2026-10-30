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
import { Label } from '@/components/ui/label';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { useWorkspaceSession } from '@/core/app-shell/workspace-shell';
import { deleteGadgetDefinition, type DashboardGroup, type GadgetDefinition } from './api';
import { GadgetEditor } from './gadget-editor';

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
  /** Optional explicit adoption of the current local readable mobile projection. */
  onUseReadableMobileSizes?: () => void;
  /** True only after a complete measured readable projection is available. */
  canUseReadableMobileSizes?: boolean;
  /** Opens the Add group dialog owned by the dashboard page; the button is hidden when omitted. */
  onAddGroup?: () => void;
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

/**
 * Toolbar and dirty navigation guard for dashboard layout editing.
 * Provides explicit Save/Cancel/Undo/Redo controls, optional readable-size adoption, and the Add dialog (template editor or library pick).
 * Shows a Save / Discard / Stay modal before discarding unsaved layout changes,
 * and retains draft state upon failed save attempts.
 *
 * @param props Toolbar state flags and action callbacks.
 * @returns Accessible toolbar and owner decision dialogs.
 */
export function LayoutEditor({
  dashboardName,
  editCount,
  baseRevision,
  isDirty,
  isSaving,
  canUndo,
  canRedo,
  onSave,
  onCancel,
  onUndo,
  onRedo,
  onUseReadableMobileSizes,
  canUseReadableMobileSizes = false,
  onOpenPresetPicker,
  onAddGroup,
  onAddGadgetInstance,
  onBeginAddGadgetInstance,
  onCancelPendingAdd,
  groups,
  activeGroupId,
  dirtyModalOpen,
  dirtyDescription,
  onDirtyModalResolution,
  saveError,
  onDismissSaveError,
}: LayoutEditorProps) {
  const t = useTranslations('dashboard');
  const session = useWorkspaceSession();
  const [addDialogOpen, setAddDialogOpen] = useState<boolean>(false);
  const isAddingRef = useRef(false);

  const [targetGroupId, setTargetGroupId] = useState<string>(activeGroupId || groups[0]?.id || '');
  const [addError, setAddError] = useState<string | null>(null);
  const tg = useTranslations('gadgetSettings');

  /**
   * Places the newly-created definition on the dashboard and deletes it if reservation or instance creation fails.
   * Never throws; failures leave the add dialog open with a localized error.
   */
  const addDefinition = useCallback(async (definition: Pick<GadgetDefinition, 'id' | 'renderer' | 'revision'>) => {
    // Empty means "let the server pick or create the default group".
    const finalGroupId = targetGroupId || groups[0]?.id || undefined;
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
  }, [groups, onAddGadgetInstance, onBeginAddGadgetInstance, onCancelPendingAdd, session.csrfToken, targetGroupId, tg]);

  return (
    <>
      {/* Top Edit Toolbar (DashboardEdit artboard): title + edit state left, explicit Cancel / Save right */}
      <div className="flex flex-wrap items-center gap-2 rounded-xl border border-border bg-card px-4 py-3">
        <div className="mr-auto flex min-w-0 flex-col">
          <strong className="text-[15px] font-bold text-foreground">{t('editingTitle', { name: dashboardName })}</strong>
          <span role="status" className="flex items-center text-xs text-muted-foreground">
            <span className={`mr-1.5 inline-block size-2 rounded-full ${isDirty ? 'bg-primary' : 'bg-border'}`} aria-hidden="true" />
            {isDirty ? <>{t('unsavedChanges')} {'·'} </> : null}
            {t('editCount', { count: editCount })}
            {baseRevision != null ? <> {'·'} {t('draftOf', { revision: baseRevision })}</> : null}
          </span>
        </div>

        <Button type="button" variant="outline" className="min-h-11" disabled={!canUndo || isSaving} onClick={onUndo}>
          <Undo2 className="size-4" aria-hidden="true" />{t('undo')}
        </Button>
        <Button type="button" variant="outline" className="min-h-11" disabled={!canRedo || isSaving} onClick={onRedo}>
          <Redo2 className="size-4" aria-hidden="true" />{t('redo')}
        </Button>
        <span aria-hidden="true" className="mx-1 h-7 w-px bg-border" />
        {onUseReadableMobileSizes && (
          <Button type="button" variant="outline" className="min-h-11" disabled={!canUseReadableMobileSizes || isSaving} onClick={onUseReadableMobileSizes}>
            {t('useReadableMobileSizes')}
          </Button>
        )}
        <Button type="button" variant="outline" className="min-h-11" disabled={isSaving} onClick={() => { setAddError(null); setAddDialogOpen(true); }}>
          <Plus className="size-4" aria-hidden="true" />{t('addGadget')}
        </Button>
        {onAddGroup && (
          <Button type="button" variant="outline" className="min-h-11" disabled={isSaving} onClick={onAddGroup}>
            <FolderPlus className="size-4" aria-hidden="true" />{t('addGroup')}
          </Button>
        )}
        <Button type="button" variant="outline" className="min-h-11" disabled={isSaving} onClick={onOpenPresetPicker}>
          <LayoutTemplate className="size-4" aria-hidden="true" />{t('presets')}
        </Button>
        <span aria-hidden="true" className="mx-1 h-7 w-px bg-border" />
        <Button type="button" variant="outline" className="min-h-11" disabled={isSaving} onClick={onCancel}>
          {t('cancelEdit')}
        </Button>
        <Button type="button" className="min-h-11 font-semibold" disabled={isSaving || !isDirty} onClick={onSave}>
          {isSaving ? t('saving') : <><Check className="size-4" aria-hidden="true" />{t('saveLayout')}</>}
        </Button>
      </div>

      <ul aria-label={t('gridLegend')} className="m-0 flex list-none flex-wrap items-center gap-x-4 gap-y-1 p-0 text-xs text-muted-foreground">
        <li className="flex items-center gap-1.5"><span aria-hidden="true" className="h-3 w-3 rounded-sm border border-border" />{t('legendFree')}</li>
        <li className="flex items-center gap-1.5"><span aria-hidden="true" className="h-3 w-3 rounded-sm border border-primary bg-primary/15" />{t('legendDrop')}</li>
        <li className="flex items-center gap-1.5"><span aria-hidden="true" className="h-3 w-3 rounded-sm border border-destructive bg-destructive/15" />{t('legendOverlap')}</li>
        <li>{t('keyboardHelp')}</li>
      </ul>

      {/* Save Error Alert Banner */}
      {saveError && (
        <div className="mb-4 p-3 rounded-lg bg-destructive/10 border border-destructive/30 text-destructive text-xs flex items-center justify-between">
          <div className="flex items-center gap-2">
            <AlertCircle className="w-4 h-4 shrink-0" />
            <span className="font-medium">{saveError}</span>
          </div>
          {onDismissSaveError && (
            <button
              type="button"
              onClick={onDismissSaveError}
              className="p-1 hover:bg-destructive/20 rounded"
            >
              <X className="w-3.5 h-3.5" />
            </button>
          )}
        </div>
      )}

      {/* Dirty Switch Warning Modal (Save / Discard / Stay) */}
      <AlertDialog open={dirtyModalOpen}>
        <AlertDialogContent className="sm:max-w-md">
          <AlertDialogHeader>
            <AlertDialogTitle className="flex items-center gap-2 text-foreground">
              <AlertCircle className="w-5 h-5 text-muted-foreground" />
              <span>{t('dirtyWarningTitle')}</span>
            </AlertDialogTitle>
            <AlertDialogDescription className="text-muted-foreground text-sm">
              {dirtyDescription ?? t('dirtyWarningDesc')}
            </AlertDialogDescription>
          </AlertDialogHeader>

          <AlertDialogFooter className="flex-col sm:flex-row gap-2 mt-4">
            <Button
              type="button"
              disabled={isSaving}
              className="secondary w-full sm:w-auto text-xs"
              onClick={() => onDirtyModalResolution('stay')}
            >
              {t('stayOnPage')}
            </Button>
            <Button
              type="button"
              disabled={isSaving}
              className="secondary text-destructive hover:bg-destructive/10 w-full sm:w-auto text-xs"
              onClick={() => onDirtyModalResolution('discard')}
            >
              {t('discardChanges')}
            </Button>
            <Button
              type="button"
              disabled={isSaving}
              className="w-full sm:w-auto text-xs font-bold"
              onClick={() => onDirtyModalResolution('save')}
            >
              {t('saveAndContinue')}
            </Button>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* Add Gadget Dialog */}
      <Dialog open={addDialogOpen} onOpenChange={(open) => { if (!isAddingRef.current) setAddDialogOpen(open); }}>
        <DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-2xl">
          <DialogHeader>
            <DialogTitle>{t('addGadgetTitle')}</DialogTitle>
            <DialogDescription className="text-xs text-muted-foreground">
              {tg('addDescription')}
            </DialogDescription>
          </DialogHeader>

          <div className="space-y-4 py-2">
            {/* Target Group */}
            {groups.length > 0 && (
              <div className="space-y-1">
                <Label htmlFor="inst-group" className="text-xs">
                  {t('selectGroup')}
                </Label>
                <Select
                  value={targetGroupId || groups[0]?.id}
                  onValueChange={setTargetGroupId}
                >
                  <SelectTrigger id="inst-group" className="h-8 text-xs">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {groups.map((group) => (
                      <SelectItem key={group.id} value={group.id} className="text-xs">
                        {group.name}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
            )}

            {/* Save creates the definition, then places its instance; failure removes the unused new definition. */}
            <GadgetEditor
              onSaved={addDefinition}
              onCancel={() => { if (!isAddingRef.current) setAddDialogOpen(false); }}
              saveLabel={t('addGadget')}
            />
            {addError && <p role="alert" className="text-xs text-destructive">{addError}</p>}
          </div>
        </DialogContent>
      </Dialog>
    </>
  );
}
