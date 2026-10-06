'use client';

import { useMutation, useQuery } from '@tanstack/react-query';
import {
  AlertCircle,
  Check,
  LayoutTemplate,
  Plus,
  Redo2,
  Sliders,
  Undo2,
  X,
} from 'lucide-react';
import { useTranslations } from 'next-intl';
import React, { useCallback, useMemo, useRef, useState } from 'react';
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
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import { SOURCE_BACKED_RENDERERS, SourcePicker } from './source-picker';
import { Label } from '@/components/ui/label';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { useWorkspaceSession } from '@/core/app-shell/workspace-shell';
import {
  createGadgetDefinition,
  dashboardKeys,
  listGadgetDefinitions,
  listGadgetRenderers,
  type DashboardGroup,
} from './api';

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
  /** Callback fired when user opens the preset picker dialog. */
  onOpenPresetPicker: () => void;
  /** Callback fired when adding a selected definition or new gadget instance. */
  onAddGadgetInstance: (params: {
    definitionId: string;
    groupId: string;
    title?: string;
    renderer: string;
  }, reservationId: string) => Promise<void>;
  /** Reserves the owner layout before any asynchronous definition creation begins. */
  onBeginAddGadgetInstance: (renderer: string) => string | null;
  /** Releases a reservation when quick definition creation fails before instance creation. */
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
 * Provides explicit Save/Cancel/Undo/Redo controls, optional readable-size adoption, and Add
 * preflight before quick definitions are created.
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
  const [isAdding, setIsAdding] = useState<boolean>(false);
  const isAddingRef = useRef(false);

  // States for Add Gadget modal
  const [selectedDefinitionId, setSelectedDefinitionId] = useState<string>('');
  const [instanceTitle, setInstanceTitle] = useState<string>('');
  const [targetGroupId, setTargetGroupId] = useState<string>(activeGroupId || groups[0]?.id || '');
  const [selectedRenderer, setSelectedRenderer] = useState<string>('news_feed');
  const [newDefName, setNewDefName] = useState<string>('');
  const [newDefSourceId, setNewDefSourceId] = useState<string | null>(null);

  // Queries for reusable definitions and renderers
  const definitionsQuery = useQuery({
    queryKey: dashboardKeys.definitions,
    queryFn: ({ signal }) => listGadgetDefinitions(200, signal),
    enabled: addDialogOpen,
  });

  const renderersQuery = useQuery({
    queryKey: dashboardKeys.renderers,
    queryFn: ({ signal }) => listGadgetRenderers(signal),
    enabled: addDialogOpen,
  });

  const definitions = useMemo(() => definitionsQuery.data ?? [], [definitionsQuery.data]);
  const renderers = renderersQuery.data ?? [];

  // Quick definition creation mutation
  const createDefMutation = useMutation({
    mutationFn: async () => {
      return createGadgetDefinition(
        {
          name: newDefName.trim() || `${selectedRenderer} gadget`,
          renderer: selectedRenderer,
          // A chosen source is saved only for source-backed renderers.
          source_ids: selectedRenderer in SOURCE_BACKED_RENDERERS && newDefSourceId ? [newDefSourceId] : [],
          scope: {},
          filters: {},
          highlight_rules: [],
        },
        session.csrfToken,
      );
    },
  });

  /** Handles confirmation of adding a gadget to the dashboard. */
  const handleConfirmAdd = useCallback(async () => {
    if (isAddingRef.current) return;
    let definitionId = selectedDefinitionId;
    const finalGroupId = targetGroupId || groups[0]?.id || '';
    const selectedDefinition = definitions.find((definition) => definition.id === definitionId);
    const renderer = selectedDefinition?.renderer ?? selectedRenderer;
    const reservationId = onBeginAddGadgetInstance(renderer);
    if (!reservationId) {
      setAddDialogOpen(false);
      return;
    }

    isAddingRef.current = true;
    setIsAdding(true);
    try {
      // If no existing definition selected, create a quick definition first.
      if (!definitionId) {
        const created = await createDefMutation.mutateAsync();
        definitionId = created.id;
      }

      await onAddGadgetInstance({
        definitionId,
        groupId: finalGroupId,
        title: instanceTitle.trim() || undefined,
        renderer,
      }, reservationId);

      setAddDialogOpen(false);
      setSelectedDefinitionId('');
      setInstanceTitle('');
      setNewDefName('');
    } catch {
      onCancelPendingAdd(reservationId);
    } finally {
      isAddingRef.current = false;
      setIsAdding(false);
    }
  }, [createDefMutation, definitions, groups, instanceTitle, onAddGadgetInstance, onBeginAddGadgetInstance, onCancelPendingAdd, selectedDefinitionId, selectedRenderer, targetGroupId]);

  return (
    <>
      {/* Top Edit Toolbar */}
      <div className="flex flex-wrap items-center justify-between gap-3 p-3 rounded-xl border border-primary/40 bg-primary/5 backdrop-blur-xs mb-4 shadow-xs">
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
          <span className="flex items-center gap-1.5 text-sm font-semibold text-foreground">
            <Sliders className="w-4 h-4 text-primary" aria-hidden="true" />
            <span>{t('editingTitle', { name: dashboardName })}</span>
          </span>
          {isDirty ? (
            <span role="status" className="flex items-center gap-1.5 text-xs font-semibold text-foreground">
              <span className="w-2 h-2 rounded-full bg-primary" aria-hidden="true" />
              <span>{t('unsavedChanges')}</span>
            </span>
          ) : null}
          <span className="text-xs text-muted-foreground">{t('keyboardHelp')}</span>
          <span className="text-xs text-muted-foreground">
            {t('editCount', { count: editCount })}
            {baseRevision != null ? <> {'·'} {t('draftOf', { revision: baseRevision })}</> : null}
          </span>
        </div>

        {/* Toolbar Buttons */}
        <div className="flex items-center gap-1.5 flex-wrap">
          {/* Undo */}
          <button
            type="button"
            disabled={!canUndo || isSaving}
            onClick={onUndo}
            aria-label={t('undo')}
            title={t('undo')}
            className="p-1.5 rounded-lg border border-border bg-card text-foreground hover:bg-primary/10 disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
          >
            <Undo2 className="w-4 h-4" />
          </button>

          {/* Redo */}
          <button
            type="button"
            disabled={!canRedo || isSaving}
            onClick={onRedo}
            aria-label={t('redo')}
            title={t('redo')}
            className="p-1.5 rounded-lg border border-border bg-card text-foreground hover:bg-primary/10 disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
          >
            <Redo2 className="w-4 h-4" />
          </button>

          <div className="w-px h-5 bg-border mx-1" />

          {onUseReadableMobileSizes && (
            <Button
              type="button"
              className="secondary min-h-11 text-xs px-3"
              disabled={!canUseReadableMobileSizes || isSaving}
              onClick={onUseReadableMobileSizes}
            >
              {t('useReadableMobileSizes')}
            </Button>
          )}

          {/* Add Gadget */}
          <Button
            type="button"
            disabled={isSaving}
            className="secondary text-xs h-8 px-2.5 gap-1.5"
            onClick={() => setAddDialogOpen(true)}
          >
            <Plus className="w-3.5 h-3.5" />
            <span>{t('addGadget')}</span>
          </Button>

          {/* Presets */}
          <Button
            type="button"
            disabled={isSaving}
            className="secondary text-xs h-8 px-2.5 gap-1.5"
            onClick={onOpenPresetPicker}
          >
            <LayoutTemplate className="w-3.5 h-3.5" />
            <span>{t('presets')}</span>
          </Button>

          <div className="w-px h-5 bg-border mx-1" />

          {/* Cancel */}
          <Button
            type="button"
            disabled={isSaving}
            onClick={onCancel}
            className="secondary text-xs h-8 px-3"
          >
            {t('cancelEdit')}
          </Button>

          {/* Save */}
          <Button
            type="button"
            disabled={isSaving || !isDirty}
            onClick={onSave}
            className="text-xs h-8 px-3.5 font-bold gap-1.5"
          >
            {isSaving ? (
              t('saving')
            ) : (
              <>
                <Check className="w-3.5 h-3.5" />
                <span>{t('saveLayout')}</span>
              </>
            )}
          </Button>
        </div>
      </div>

      <ul aria-label={t('gridLegend')} className="-mt-2 mb-4 flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground">
        <li className="flex items-center gap-1.5"><span aria-hidden="true" className="h-3 w-3 rounded-sm border border-border" />{t('legendFree')}</li>
        <li className="flex items-center gap-1.5"><span aria-hidden="true" className="h-3 w-3 rounded-sm border border-primary bg-primary/15" />{t('legendDrop')}</li>
        <li className="flex items-center gap-1.5"><span aria-hidden="true" className="h-3 w-3 rounded-sm border border-destructive bg-destructive/15" />{t('legendOverlap')}</li>
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
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle>{t('addGadgetTitle')}</DialogTitle>
            <DialogDescription className="text-xs text-muted-foreground">
              {t('selectDefinition')}
            </DialogDescription>
          </DialogHeader>

          <div className="space-y-4 py-2">
            {/* Instance Title */}
            <div className="space-y-1">
              <Label htmlFor="inst-title" className="text-xs">
                {t('instanceTitle')}
              </Label>
              <Input
                id="inst-title"
                value={instanceTitle}
                onChange={(e) => setInstanceTitle(e.target.value)}
                placeholder="My News Feed"
                className="h-8 text-xs"
              />
            </div>

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

            {/* Existing Definition selector */}
            <div className="space-y-1">
              <Label htmlFor="inst-def" className="text-xs font-semibold">
                {t('selectDefinition')}
              </Label>
              {definitions.length > 0 ? (
                <Select
                  value={selectedDefinitionId}
                  onValueChange={setSelectedDefinitionId}
                >
                  <SelectTrigger id="inst-def" className="h-8 text-xs">
                    <SelectValue placeholder="Choose existing definition" />
                  </SelectTrigger>
                  <SelectContent>
                    {definitions.map((def) => (
                      <SelectItem key={def.id} value={def.id} className="text-xs">
                        {def.name} ({def.renderer})
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              ) : (
                <p className="text-xs text-muted-foreground italic">
                  {t('noDefinitions')}
                </p>
              )}
            </div>

            {/* If no definition selected, quick create option */}
            {!selectedDefinitionId && (
              <div className="p-3 rounded-lg border border-border bg-muted/20 space-y-2.5">
                <div className="text-xs font-semibold text-foreground">
                  {t('orCreateInstance')}
                </div>
                <div className="space-y-1">
                  <Label htmlFor="new-def-name" className="text-xs">
                    Definition Name
                  </Label>
                  <Input
                    id="new-def-name"
                    value={newDefName}
                    onChange={(e) => setNewDefName(e.target.value)}
                    placeholder="New gadget"
                    className="h-8 text-xs"
                  />
                </div>
                <div className="space-y-1">
                  <Label htmlFor="new-renderer" className="text-xs">
                    {t('renderer')}
                  </Label>
                  <Select
                    value={selectedRenderer}
                    onValueChange={setSelectedRenderer}
                  >
                    <SelectTrigger id="new-renderer" className="h-8 text-xs">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      {renderers.length > 0 ? (
                        renderers.map((r) => (
                          <SelectItem key={r.id} value={r.id} className="text-xs">
                            {r.id} (min {r.minimum_width}×{r.minimum_height})
                          </SelectItem>
                        ))
                      ) : (
                        <SelectItem value="news_feed" className="text-xs">
                          news_feed (min 4×4)
                        </SelectItem>
                      )}
                    </SelectContent>
                  </Select>
                </div>
                {selectedRenderer in SOURCE_BACKED_RENDERERS && (
                  <SourcePicker
                    id="new-def-source"
                    provider={SOURCE_BACKED_RENDERERS[selectedRenderer].provider}
                    value={newDefSourceId}
                    onChange={setNewDefSourceId}
                  />
                )}
              </div>
            )}
          </div>

          <DialogFooter className="gap-2">
            <Button
              type="button"
              className="secondary text-xs"
              disabled={isAdding}
              onClick={() => { if (!isAddingRef.current) setAddDialogOpen(false); }}
            >
              {t('cancel')}
            </Button>
            <Button
              type="button"
              disabled={isAdding}
              className="text-xs font-bold"
              onClick={handleConfirmAdd}
            >
              {t('addGadget')}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
