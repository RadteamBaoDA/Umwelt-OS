'use client';

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  ChevronDown,
  Edit3,
  Eye,
  FolderPlus,
  Layout,
  LayoutTemplate,
  Pencil,
  Plus,
  Sparkles,
  Trash2,
} from 'lucide-react';
import { useTranslations } from 'next-intl';
import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
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
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Tabs, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { useWorkspaceSession } from '@/core/app-shell/workspace-shell';
import { useGuardedNavigation } from '@/core/guarded-navigation';
import {
  createDashboard,
  createDashboardGroup,
  createGadgetInstance,
  dashboardKeys,
  deleteDashboard,
  deleteGadgetInstance,
  getDashboard,
  listDashboards,
  renameDashboard,
  replaceDashboardLayout,
  type Dashboard,
  type DashboardPlacement,
  type GadgetInstance,
} from './api';
import {
  DashboardGrid,
  doPlacementsOverlap,
  RENDERER_MIN_SIZES,
} from './dashboard-grid';
import { LayoutEditor } from './layout-editor';
import { PresetPicker } from './preset-picker';
import { GadgetSettings } from './gadget-settings';
import { NotificationBell } from '@/modules/notifications/notification-bell';

/**
 * Finds the first free non-overlapping integer rectangle on the 20-column grid.
 *
 * @param existing Current placements on the grid.
 * @param w Width of the new gadget.
 * @param h Height of the new gadget.
 * @param columns Total columns count.
 * @returns Valid (x, y) coordinates.
 */
function findFreeCoordinates(
  existing: DashboardPlacement[],
  w: number,
  h: number,
  columns = 20,
): { x: number; y: number } {
  let y = 0;
  while (y < 500) {
    for (let x = 0; x <= columns - w; x++) {
      const candidate: DashboardPlacement = { instance_id: '', x, y, w, h };
      const collides = existing.some((p) => doPlacementsOverlap(candidate, p));
      if (!collides) {
        return { x, y };
      }
    }
    y++;
  }
  return { x: 0, y: 0 };
}

/**
 * Main dashboard container view for BBD-OS.
 * Supports viewing and editing modes, dashboard switching, group tab filtering,
 * preset picker triggering, reading stability queuing, and dirty navigation guarding.
 *
 * @returns Authenticated dashboard view component.
 */
export function DashboardPage() {
  const t = useTranslations('dashboard');
  const session = useWorkspaceSession();
  const queryClient = useQueryClient();
  const guardedNavigation = useGuardedNavigation();

  // Active dashboard selection
  const [selectedDashboardId, setSelectedDashboardId] = useState<string | null>(null);
  const [isEditMode, setIsEditMode] = useState<boolean>(false);
  const [activeGroupId, setActiveGroupId] = useState<string>('all');
  const [isMobile, setIsMobile] = useState<boolean>(false);

  // Dialogs state
  const [presetPickerOpen, setPresetPickerOpen] = useState<boolean>(false);
  const [createDashboardOpen, setCreateDashboardOpen] = useState<boolean>(false);
  const [newDashboardName, setNewDashboardName] = useState<string>('');
  const [renameDialogOpen, setRenameDialogOpen] = useState<boolean>(false);
  const [renameValue, setRenameValue] = useState<string>('');
  const [deleteDialogOpen, setDeleteDialogOpen] = useState<boolean>(false);
  const [addGroupOpen, setAddGroupOpen] = useState<boolean>(false);
  const [newGroupName, setNewGroupName] = useState<string>('');
  const [configuringInstance, setConfiguringInstance] = useState<GadgetInstance | null>(null);
  const [gadgetSettingsOpen, setGadgetSettingsOpen] = useState<boolean>(false);

  // Dirty state and undo/redo history
  const [isDirty, setIsDirty] = useState<boolean>(false);
  const [dirtyModalOpen, setDirtyModalOpen] = useState<boolean>(false);
  const pendingDashboardSwitchRef = useRef<string | null>(null);
  const pendingExitEditRef = useRef<boolean>(false);

  // Layout placements draft and history stack
  const [draftPlacements, setDraftPlacements] = useState<DashboardPlacement[]>([]);
  const [history, setHistory] = useState<DashboardPlacement[][]>([]);
  const [historyIndex, setHistoryIndex] = useState<number>(-1);
  const [saveError, setSaveError] = useState<string | null>(null);

  // Reading stability queues (queued item count per gadget instance)
  const [readingQueues, setReadingQueues] = useState<Record<string, number>>({});

  // Responsive breakpoint tracking
  useEffect(() => {
    const media = window.matchMedia('(max-width: 768px)');
    setIsMobile(media.matches);
    const listener = (e: MediaQueryListEvent) => setIsMobile(e.matches);
    media.addEventListener('change', listener);
    return () => media.removeEventListener('change', listener);
  }, []);

  // Fetch dashboards list
  const dashboardsQuery = useQuery({
    queryKey: dashboardKeys.all,
    queryFn: ({ signal }) => listDashboards(signal),
  });

  const dashboards = dashboardsQuery.data ?? [];

  // Automatically select first dashboard if none selected
  useEffect(() => {
    if (!selectedDashboardId && dashboards.length > 0) {
      setSelectedDashboardId(dashboards[0].id);
    }
  }, [dashboards, selectedDashboardId]);

  // Fetch active dashboard details
  const activeDashboardQuery = useQuery({
    queryKey: dashboardKeys.detail(selectedDashboardId || ''),
    queryFn: ({ signal }) => getDashboard(selectedDashboardId!, signal),
    enabled: Boolean(selectedDashboardId),
  });

  const activeDashboard = activeDashboardQuery.data ?? null;
  const currentBreakpoint = isMobile ? 'mobile' : 'desktop';
  const savedPlacements = useMemo(() => {
    if (!activeDashboard) return [];
    return activeDashboard.layouts[currentBreakpoint]?.items ?? [];
  }, [activeDashboard, currentBreakpoint]);

  // Sync draft placements from saved placements when not dirty
  useEffect(() => {
    if (!isDirty) {
      setDraftPlacements(savedPlacements);
      setHistory([savedPlacements]);
      setHistoryIndex(0);
    }
  }, [isDirty, savedPlacements]);

  // Register GuardedNavigation leave guard for dirty layout edits
  useEffect(() => {
    return guardedNavigation.registerLeaveGuard({
      hasUnsavedChanges: () => isDirty,
      confirmDiscard: () => {
        setDirtyModalOpen(true);
        return false;
      },
      acceptLeave: () => {
        setIsDirty(false);
        setIsEditMode(false);
      },
    });
  }, [guardedNavigation, isDirty]);

  /** Pushes a new placements snapshot to the undo/redo history stack. */
  const pushHistory = useCallback((nextPlacements: DashboardPlacement[]) => {
    setHistory((prev) => {
      const truncated = prev.slice(0, historyIndex + 1);
      return [...truncated, nextPlacements];
    });
    setHistoryIndex((prev) => prev + 1);
    setDraftPlacements(nextPlacements);
    setIsDirty(true);
  }, [historyIndex]);

  /** Handles Undo action from layout editor toolbar. */
  const handleUndo = useCallback(() => {
    if (historyIndex > 0) {
      const nextIndex = historyIndex - 1;
      setHistoryIndex(nextIndex);
      setDraftPlacements(history[nextIndex]);
      setIsDirty(true);
    }
  }, [history, historyIndex]);

  /** Handles Redo action from layout editor toolbar. */
  const handleRedo = useCallback(() => {
    if (historyIndex < history.length - 1) {
      const nextIndex = historyIndex + 1;
      setHistoryIndex(nextIndex);
      setDraftPlacements(history[nextIndex]);
      setIsDirty(true);
    }
  }, [history, historyIndex]);

  // Layout save mutation
  const saveLayoutMutation = useMutation({
    mutationFn: async () => {
      if (!activeDashboard) throw new Error('No active dashboard');
      setSaveError(null);
      return replaceDashboardLayout(
        activeDashboard.id,
        {
          expected_revision: activeDashboard.revision,
          breakpoint: currentBreakpoint,
          columns: isMobile ? 1 : 20,
          items: draftPlacements,
        },
        session.csrfToken,
      );
    },
    onSuccess: (updated) => {
      queryClient.setQueryData(dashboardKeys.detail(updated.id), updated);
      void queryClient.invalidateQueries({ queryKey: dashboardKeys.all });
      setIsDirty(false);
      setHistory([draftPlacements]);
      setHistoryIndex(0);
    },
    onError: (error) => {
      // Failed save retains draft state!
      setSaveError(error instanceof Error ? error.message : t('saveFailed'));
    },
  });

  /** Triggered by Save button on toolbar. */
  const handleSave = useCallback(() => {
    void saveLayoutMutation.mutateAsync();
  }, [saveLayoutMutation]);

  /** Request to exit edit mode or switch dashboard. */
  const requestExitEdit = useCallback(() => {
    if (isDirty) {
      pendingExitEditRef.current = true;
      setDirtyModalOpen(true);
    } else {
      setIsEditMode(false);
    }
  }, [isDirty]);

  /** Request switching to another dashboard (with dirty protection). */
  const requestSwitchDashboard = useCallback(
    (newId: string) => {
      if (newId === selectedDashboardId) return;
      if (isDirty) {
        pendingDashboardSwitchRef.current = newId;
        setDirtyModalOpen(true);
      } else {
        setSelectedDashboardId(newId);
        setActiveGroupId('all');
      }
    },
    [isDirty, selectedDashboardId],
  );

  /** Resolves the dirty modal selection: save, discard, or stay. */
  const handleDirtyModalResolution = useCallback(
    async (action: 'save' | 'discard' | 'stay') => {
      setDirtyModalOpen(false);

      if (action === 'stay') {
        pendingDashboardSwitchRef.current = null;
        pendingExitEditRef.current = false;
        return;
      }

      if (action === 'discard') {
        setIsDirty(false);
        setDraftPlacements(savedPlacements);
        if (pendingDashboardSwitchRef.current) {
          setSelectedDashboardId(pendingDashboardSwitchRef.current);
          pendingDashboardSwitchRef.current = null;
          setActiveGroupId('all');
        }
        if (pendingExitEditRef.current) {
          setIsEditMode(false);
          pendingExitEditRef.current = false;
        }
        return;
      }

      if (action === 'save') {
        try {
          await saveLayoutMutation.mutateAsync();
          if (pendingDashboardSwitchRef.current) {
            setSelectedDashboardId(pendingDashboardSwitchRef.current);
            pendingDashboardSwitchRef.current = null;
            setActiveGroupId('all');
          }
          if (pendingExitEditRef.current) {
            setIsEditMode(false);
            pendingExitEditRef.current = false;
          }
        } catch {
          // If save fails, retain draft and remain on page
          pendingDashboardSwitchRef.current = null;
          pendingExitEditRef.current = false;
        }
      }
    },
    [saveLayoutMutation, savedPlacements],
  );

  // Dashboard creation mutation
  const createDashboardMutation = useMutation({
    mutationFn: async (name: string) => {
      return createDashboard(name, session.csrfToken);
    },
    onSuccess: (newDash) => {
      void queryClient.invalidateQueries({ queryKey: dashboardKeys.all });
      setSelectedDashboardId(newDash.id);
      setCreateDashboardOpen(false);
      setNewDashboardName('');
    },
  });

  // Rename dashboard mutation
  const renameMutation = useMutation({
    mutationFn: async (name: string) => {
      if (!activeDashboard) return;
      return renameDashboard(
        activeDashboard.id,
        name,
        activeDashboard.revision,
        session.csrfToken,
      );
    },
    onSuccess: (renamed) => {
      if (renamed) {
        queryClient.setQueryData(dashboardKeys.detail(renamed.id), renamed);
        void queryClient.invalidateQueries({ queryKey: dashboardKeys.all });
      }
      setRenameDialogOpen(false);
    },
  });

  // Delete dashboard mutation
  const deleteMutation = useMutation({
    mutationFn: async () => {
      if (!activeDashboard) return;
      return deleteDashboard(activeDashboard.id, activeDashboard.revision, session.csrfToken);
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: dashboardKeys.all });
      setDeleteDialogOpen(false);
      setSelectedDashboardId(null);
    },
  });

  // Add Group mutation
  const addGroupMutation = useMutation({
    mutationFn: async (name: string) => {
      if (!activeDashboard) return;
      return createDashboardGroup(
        activeDashboard.id,
        name,
        activeDashboard.revision,
        session.csrfToken,
        activeDashboard.groups.length,
      );
    },
    onSuccess: () => {
      if (activeDashboard) {
        void queryClient.invalidateQueries({ queryKey: dashboardKeys.detail(activeDashboard.id) });
      }
      setAddGroupOpen(false);
      setNewGroupName('');
    },
  });

  // Add Gadget Instance
  const handleAddGadgetInstance = useCallback(
    async (params: { definitionId: string; groupId: string; title?: string }) => {
      if (!activeDashboard) return;
      try {
        const updatedDashboard = await createGadgetInstance(
          activeDashboard.id,
          {
            expected_revision: activeDashboard.revision,
            group_id: params.groupId,
            definition_id: params.definitionId,
            title: params.title,
          },
          session.csrfToken,
        );

        queryClient.setQueryData(dashboardKeys.detail(updatedDashboard.id), updatedDashboard);

        // Find newly added instance
        const newInstance = updatedDashboard.instances.find(
          (inst) => !activeDashboard.instances.some((old) => old.id === inst.id),
        );

        if (newInstance) {
          const rendererKey = newInstance.definition.renderer;
          const minSize = RENDERER_MIN_SIZES[rendererKey] ?? { minW: 4, minH: 3 };
          const coords = findFreeCoordinates(draftPlacements, minSize.minW, minSize.minH, 20);

          const newPlacement: DashboardPlacement = {
            instance_id: newInstance.id,
            x: coords.x,
            y: coords.y,
            w: minSize.minW,
            h: minSize.minH,
          };

          pushHistory([...draftPlacements, newPlacement]);
        }
      } catch (err) {
        setSaveError(err instanceof Error ? err.message : t('saveFailed'));
      }
    },
    [activeDashboard, draftPlacements, pushHistory, queryClient, session.csrfToken, t],
  );

  // Remove Gadget Instance
  const handleRemoveInstance = useCallback(
    async (instanceId: string) => {
      if (!activeDashboard) return;
      try {
        const updatedDashboard = await deleteGadgetInstance(
          activeDashboard.id,
          instanceId,
          activeDashboard.revision,
          session.csrfToken,
        );
        queryClient.setQueryData(dashboardKeys.detail(updatedDashboard.id), updatedDashboard);
        const filtered = draftPlacements.filter((p) => p.instance_id !== instanceId);
        pushHistory(filtered);
      } catch (err) {
        setSaveError(err instanceof Error ? err.message : t('saveFailed'));
      }
    },
    [activeDashboard, draftPlacements, pushHistory, queryClient, session.csrfToken, t],
  );

  /** Filter instances by active group tab. */
  const visibleInstances: GadgetInstance[] = useMemo(() => {
    if (!activeDashboard) return [];
    if (activeGroupId === 'all') return activeDashboard.instances;
    return activeDashboard.instances.filter((inst) => inst.group_id === activeGroupId);
  }, [activeDashboard, activeGroupId]);

  /** Filter placements for visible instances. */
  const visiblePlacements: DashboardPlacement[] = useMemo(() => {
    const visibleIds = new Set(visibleInstances.map((inst) => inst.id));
    return draftPlacements.filter((p) => visibleIds.has(p.instance_id));
  }, [draftPlacements, visibleInstances]);

  /** Revealing / clearing reading updates for an instance. */
  const handleApplyReadingQueue = useCallback((instanceId: string) => {
    setReadingQueues((prev) => {
      const next = { ...prev };
      delete next[instanceId];
      return next;
    });
  }, []);

  return (
    <section className="flex flex-col w-full min-h-[calc(100vh-140px)] gap-4">
      {/* Dashboard Top Header Bar */}
      <div className="flex flex-wrap items-center justify-between gap-4 pb-2 border-b border-border">
        {/* Left: Switcher, Active Title, Group Tabs */}
        <div className="flex flex-wrap items-center gap-3">
          {/* Dashboard Switcher Dropdown */}
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <button
                type="button"
                className="inline-flex items-center gap-2 px-3 py-1.5 rounded-lg border border-border bg-card text-foreground font-bold text-base hover:bg-primary/10 transition-colors"
              >
                <Layout className="w-4 h-4 text-primary" />
                <span>{activeDashboard?.name ?? t('title')}</span>
                <ChevronDown className="w-4 h-4 text-muted-foreground ml-1" />
              </button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="start" className="w-56">
              {dashboards.map((dash) => (
                <DropdownMenuItem
                  key={dash.id}
                  onClick={() => requestSwitchDashboard(dash.id)}
                  className={`text-xs flex items-center justify-between ${
                    dash.id === selectedDashboardId ? 'font-bold text-primary' : ''
                  }`}
                >
                  <span className="truncate">{dash.name}</span>
                  <span className="text-[10px] text-muted-foreground font-mono">
                    r.{dash.revision}
                  </span>
                </DropdownMenuItem>
              ))}

              <DropdownMenuSeparator />

              <DropdownMenuItem
                onClick={() => setCreateDashboardOpen(true)}
                className="text-xs gap-2 font-medium"
              >
                <Plus className="w-3.5 h-3.5" />
                <span>{t('newDashboard')}</span>
              </DropdownMenuItem>

              {activeDashboard && (
                <>
                  <DropdownMenuItem
                    onClick={() => {
                      setRenameValue(activeDashboard.name);
                      setRenameDialogOpen(true);
                    }}
                    className="text-xs gap-2"
                  >
                    <Pencil className="w-3.5 h-3.5" />
                    <span>{t('renameDashboard')}</span>
                  </DropdownMenuItem>

                  <DropdownMenuItem
                    onClick={() => setDeleteDialogOpen(true)}
                    className="text-xs gap-2 text-destructive focus:text-destructive"
                  >
                    <Trash2 className="w-3.5 h-3.5" />
                    <span>{t('deleteDashboard')}</span>
                  </DropdownMenuItem>
                </>
              )}
            </DropdownMenuContent>
          </DropdownMenu>

          {/* Group Tabs */}
          {activeDashboard && activeDashboard.groups.length > 0 && (
            <Tabs value={activeGroupId} onValueChange={setActiveGroupId} className="h-8">
              <TabsList className="h-8 p-0.5 bg-muted/30">
                <TabsTrigger value="all" className="text-xs px-2.5 h-7">
                  {t('allGroups')}
                </TabsTrigger>
                {activeDashboard.groups.map((group) => (
                  <TabsTrigger key={group.id} value={group.id} className="text-xs px-2.5 h-7">
                    {group.name}
                  </TabsTrigger>
                ))}
              </TabsList>
            </Tabs>
          )}

          {/* Add Group button in edit mode */}
          {isEditMode && activeDashboard && (
            <button
              type="button"
              onClick={() => setAddGroupOpen(true)}
              aria-label={t('addGroup')}
              title={t('addGroup')}
              className="p-1.5 rounded-md border border-dashed border-border text-muted-foreground hover:text-foreground hover:bg-primary/10 transition-colors"
            >
              <FolderPlus className="w-4 h-4" />
            </button>
          )}
        </div>

        {/* Right: Presets Trigger & Edit Mode Toggle */}
        <div className="flex items-center gap-2">
          <NotificationBell />
          {/* Preset Picker Trigger */}
          <Button
            type="button"
            className="secondary text-xs h-8 px-2.5 gap-1.5"
            onClick={() => setPresetPickerOpen(true)}
          >
            <Sparkles className="w-3.5 h-3.5 text-primary" />
            <span>{t('presets')}</span>
          </Button>

          {/* View / Edit Mode Toggle Button */}
          <Button
            type="button"
            onClick={() => {
              if (isEditMode) {
                requestExitEdit();
              } else {
                setIsEditMode(true);
              }
            }}
            className={`text-xs h-8 px-3 gap-1.5 font-semibold ${
              isEditMode ? 'bg-primary text-primary-foreground' : 'secondary'
            }`}
          >
            {isEditMode ? (
              <>
                <Eye className="w-3.5 h-3.5" />
                <span>{t('finishEditing')}</span>
              </>
            ) : (
              <>
                <Edit3 className="w-3.5 h-3.5" />
                <span>{t('toggleEdit')}</span>
              </>
            )}
          </Button>
        </div>
      </div>

      {/* Layout Editor Toolbar (visible in Edit Mode) */}
      {isEditMode && (
        <LayoutEditor
          isDirty={isDirty}
          isSaving={saveLayoutMutation.isPending}
          canUndo={historyIndex > 0}
          canRedo={historyIndex < history.length - 1}
          onSave={handleSave}
          onCancel={requestExitEdit}
          onUndo={handleUndo}
          onRedo={handleRedo}
          onOpenPresetPicker={() => setPresetPickerOpen(true)}
          onAddGadgetInstance={handleAddGadgetInstance}
          groups={activeDashboard?.groups ?? []}
          activeGroupId={activeGroupId === 'all' ? undefined : activeGroupId}
          dirtyModalOpen={dirtyModalOpen}
          onDirtyModalResolution={handleDirtyModalResolution}
          saveError={saveError}
          onDismissSaveError={() => setSaveError(null)}
        />
      )}

      {/* Main Grid Canvas or Empty State */}
      {!activeDashboard ? (
        <div className="flex-1 flex flex-col items-center justify-center p-12 text-center border border-dashed border-border rounded-2xl bg-card">
          <LayoutTemplate className="w-12 h-12 text-muted-foreground/60 mb-3" />
          <h2 className="text-lg font-bold text-foreground mb-1">{t('noDashboardsYet')}</h2>
          <p className="text-sm text-muted-foreground max-w-sm mb-4">{t('noDashboardsDesc')}</p>
          <div className="flex gap-2">
            <Button
              type="button"
              className="font-bold text-xs"
              onClick={() => setCreateDashboardOpen(true)}
            >
              <Plus className="w-3.5 h-3.5 mr-1" />
              {t('createFirstDashboard')}
            </Button>
            <Button
              type="button"
              className="secondary text-xs"
              onClick={() => setPresetPickerOpen(true)}
            >
              <Sparkles className="w-3.5 h-3.5 mr-1" />
              {t('presets')}
            </Button>
          </div>
        </div>
      ) : visibleInstances.length === 0 ? (
        <div className="flex-1 flex flex-col items-center justify-center p-12 text-center border border-dashed border-border rounded-2xl bg-card">
          <Layout className="w-10 h-10 text-muted-foreground/50 mb-2" />
          <h2 className="text-base font-bold text-foreground mb-1">{t('emptyDashboard')}</h2>
          <p className="text-xs text-muted-foreground max-w-sm mb-4">{t('emptyDashboardDesc')}</p>
          <div className="flex gap-2">
            <Button
              type="button"
              className="text-xs"
              onClick={() => {
                setIsEditMode(true);
              }}
            >
              <Plus className="w-3.5 h-3.5 mr-1" />
              {t('addGadget')}
            </Button>
            <Button
              type="button"
              className="secondary text-xs"
              onClick={() => setPresetPickerOpen(true)}
            >
              <Sparkles className="w-3.5 h-3.5 mr-1" />
              {t('presets')}
            </Button>
          </div>
        </div>
      ) : (
        <DashboardGrid
          instances={visibleInstances}
          placements={visiblePlacements}
          columns={isMobile ? 1 : 20}
          isEditMode={isEditMode}
          onLayoutChange={pushHistory}
          onRemoveInstance={handleRemoveInstance}
          onConfigureInstance={(inst) => {
            setConfiguringInstance(inst);
            setGadgetSettingsOpen(true);
          }}
          readingQueues={readingQueues}
          onApplyReadingQueue={handleApplyReadingQueue}
        />
      )}

      {/* Preset Picker Modal */}
      <PresetPicker
        open={presetPickerOpen}
        onOpenChange={setPresetPickerOpen}
        currentDashboardId={selectedDashboardId}
        currentDashboardRevision={activeDashboard?.revision}
        currentDashboardName={activeDashboard?.name}
        onPresetApplied={(applied) => {
          setSelectedDashboardId(applied.id);
          setActiveGroupId('all');
          setIsDirty(false);
          setIsEditMode(false);
        }}
      />

      {/* Create Dashboard Dialog */}
      <Dialog open={createDashboardOpen} onOpenChange={setCreateDashboardOpen}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>{t('createDashboardTitle')}</DialogTitle>
            <DialogDescription className="text-xs text-muted-foreground">
              {t('createDashboardTitle')}
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-2 py-2">
            <Label htmlFor="create-name" className="text-xs">
              {t('dashboardName')}
            </Label>
            <Input
              id="create-name"
              value={newDashboardName}
              onChange={(e) => setNewDashboardName(e.target.value)}
              placeholder="Overview"
              className="h-8 text-xs"
            />
          </div>
          <DialogFooter className="gap-2">
            <Button
              type="button"
              className="secondary text-xs"
              onClick={() => setCreateDashboardOpen(false)}
            >
              {t('cancel')}
            </Button>
            <Button
              type="button"
              disabled={!newDashboardName.trim() || createDashboardMutation.isPending}
              className="text-xs font-bold"
              onClick={() => createDashboardMutation.mutate(newDashboardName.trim())}
            >
              {t('create')}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Rename Dashboard Dialog */}
      <Dialog open={renameDialogOpen} onOpenChange={setRenameDialogOpen}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>{t('renameDashboardTitle')}</DialogTitle>
            <DialogDescription className="text-xs text-muted-foreground">
              {t('renameDashboardTitle')}
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-2 py-2">
            <Label htmlFor="rename-name" className="text-xs">
              {t('dashboardName')}
            </Label>
            <Input
              id="rename-name"
              value={renameValue}
              onChange={(e) => setRenameValue(e.target.value)}
              className="h-8 text-xs"
            />
          </div>
          <DialogFooter className="gap-2">
            <Button
              type="button"
              className="secondary text-xs"
              onClick={() => setRenameDialogOpen(false)}
            >
              {t('cancel')}
            </Button>
            <Button
              type="button"
              disabled={!renameValue.trim() || renameMutation.isPending}
              className="text-xs font-bold"
              onClick={() => renameMutation.mutate(renameValue.trim())}
            >
              {t('save')}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Delete Dashboard Confirmation Dialog */}
      <AlertDialog open={deleteDialogOpen}>
        <AlertDialogContent className="sm:max-w-md">
          <AlertDialogHeader>
            <AlertDialogTitle className="text-destructive flex items-center gap-2">
              <Trash2 className="w-4 h-4" />
              <span>{t('deleteDashboardTitle')}</span>
            </AlertDialogTitle>
            <AlertDialogDescription className="text-xs text-muted-foreground">
              {t('deleteDashboardConfirm', { name: activeDashboard?.name ?? '' })}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter className="gap-2">
            <Button
              type="button"
              className="secondary text-xs"
              onClick={() => setDeleteDialogOpen(false)}
            >
              {t('cancel')}
            </Button>
            <Button
              type="button"
              disabled={deleteMutation.isPending}
              className="text-xs font-bold bg-destructive text-destructive-foreground hover:bg-destructive/90"
              onClick={() => deleteMutation.mutate()}
            >
              {t('delete')}
            </Button>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* Add Group Dialog */}
      <Dialog open={addGroupOpen} onOpenChange={setAddGroupOpen}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>{t('addGroupTitle')}</DialogTitle>
            <DialogDescription className="text-xs text-muted-foreground">
              {t('addGroupTitle')}
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-2 py-2">
            <Label htmlFor="new-group-name" className="text-xs">
              {t('groupName')}
            </Label>
            <Input
              id="new-group-name"
              value={newGroupName}
              onChange={(e) => setNewGroupName(e.target.value)}
              placeholder="Market Focus"
              className="h-8 text-xs"
            />
          </div>
          <DialogFooter className="gap-2">
            <Button
              type="button"
              className="secondary text-xs"
              onClick={() => setAddGroupOpen(false)}
            >
              {t('cancel')}
            </Button>
            <Button
              type="button"
              disabled={!newGroupName.trim() || addGroupMutation.isPending}
              className="text-xs font-bold"
              onClick={() => addGroupMutation.mutate(newGroupName.trim())}
            >
              {t('create')}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
      {/* Gadget Instance & Definition Settings Modal */}
      <GadgetSettings
        open={gadgetSettingsOpen}
        onOpenChange={setGadgetSettingsOpen}
        instance={configuringInstance}
        dashboardId={selectedDashboardId ?? ''}
        dashboardRevision={activeDashboard?.revision ?? 1}
      />
    </section>
  );
}
