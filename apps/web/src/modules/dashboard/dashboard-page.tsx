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
import Link from 'next/link';
import { useTranslations } from 'next-intl';
import React, { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore, useLayoutEffect } from 'react';
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
import { useChatController } from '@/core/app-shell/chat-controller';
import { useWorkspaceSession } from '@/core/app-shell/workspace-shell';
import { useGuardedNavigation, type GuardedNavigationIntent } from '@/core/guarded-navigation';
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
import { getGadgetReadingBodyFloor } from './widget-registry';
import { LayoutEditor } from './layout-editor';
import { PresetPicker } from './preset-picker';
import { GadgetSettings } from './gadget-settings';
import { useDisplayPreferences } from '@/core/query-provider';
import { formatDateTime } from '@/core/i18n';
import { listSources, sourceKeys } from '@/modules/sources/api';
import { NotificationBell } from '@/modules/notifications/notification-bell';

/**
 * Finds the first free non-overlapping integer rectangle on the 20-column grid.
 *
 * @param existing Current placements on the grid.
 * @param w Width of the new gadget.
 * @param h Height of the new gadget.
 * @param columns Total columns count.
 * @returns Valid free coordinates, or null when bounded space is exhausted or invalid.
 */
function findFreeCoordinates(
  existing: DashboardPlacement[],
  w: number,
  h: number,
  columns = 20,
): { x: number; y: number } | null {
  if (!Number.isInteger(columns) || columns < 1 || columns > 20
    || !Number.isInteger(w) || !Number.isInteger(h) || w < 1 || w > columns || h < 1) return null;
  let y = 0;
  while (y + h <= MAX_LAYOUT_ROWS) {
    for (let x = 0; x <= columns - w; x++) {
      const candidate: DashboardPlacement = { instance_id: '', x, y, w, h };
      const collides = existing.some((p) => doPlacementsOverlap(candidate, p));
      if (!collides) {
        return { x, y };
      }
    }
    y++;
  }
  return null;
}

const MAX_LAYOUT_ROWS = 100_000;

const MOBILE_QUERY = '(max-width: 768px)';
/** Subscribes to the phone-width media query. */
function subscribeMobileMedia(onChange: () => void): () => void {
  const media = window.matchMedia(MOBILE_QUERY);
  media.addEventListener('change', onChange);
  return () => media.removeEventListener('change', onChange);
}
/** Reads the current phone-width match. */
function getMobileMedia(): boolean { return window.matchMedia(MOBILE_QUERY).matches; }
/** Server render assumes desktop. */
function getServerMobileMedia(): boolean { return false; }

type MobileProjection = { placements: DashboardPlacement[]; unavailable: boolean };

/** Immutable identity and baseline owned by one dirty dashboard layout session. */
interface LayoutEditOrigin {
  dashboardId: string;
  breakpoint: 'desktop' | 'mobile';
  columns: number;
  baseRevision: number;
  savedSnapshot: DashboardPlacement[];
}

/** Compares complete geometry snapshots independently of array order. */
function sameLayout(a: DashboardPlacement[], b: DashboardPlacement[]): boolean {
  if (a.length !== b.length) return false;
  const left = new Map(a.map((placement) => [placement.instance_id, placement]));
  return left.size === b.length && b.every((placement) => {
    const saved = left.get(placement.instance_id);
    return saved !== undefined && saved.x === placement.x && saved.y === placement.y
      && saved.w === placement.w && saved.h === placement.h;
  });
}

/** Validates a complete owner snapshot before it enters history or reaches the Save mutation. */
function isValidLayoutSnapshot(
  placements: DashboardPlacement[],
  instances: GadgetInstance[],
  columns: number,
): boolean {
  if (!Number.isInteger(columns) || columns < 1 || columns > 20
    || placements.length !== instances.length) return false;
  const instancesById = new Map(instances.map((instance) => [instance.id, instance]));
  if (instancesById.size !== instances.length) return false;
  const placementIds = new Set(placements.map((placement) => placement.instance_id));
  if (placementIds.size !== placements.length || instances.some((instance) => !placementIds.has(instance.id))) return false;
  for (let index = 0; index < placements.length; index++) {
    const placement = placements[index];
    const instance = instancesById.get(placement.instance_id);
    if (!instance) return false;
    const minimum = RENDERER_MIN_SIZES[instance.definition.renderer] ?? { minW: 4, minH: 3 };
    if (![placement.x, placement.y, placement.w, placement.h].every(Number.isInteger)
      || placement.x < 0 || placement.y < 0 || placement.w < minimum.minW
      || placement.h < minimum.minH || placement.x + placement.w > columns
      || placement.y + placement.h > MAX_LAYOUT_ROWS) return false;
    if (placements.slice(index + 1).some((other) => doPlacementsOverlap(placement, other))) return false;
  }
  return true;
}

/** Exact values captured when a layout save begins; later viewport state cannot rebind the request. */
interface LayoutSaveRequest extends LayoutEditOrigin {
  items: DashboardPlacement[];
}

/** Identity reserved across quick definition creation and dashboard instance creation. */
interface AddReservation {
  dashboardId: string;
  breakpoint: 'desktop' | 'mobile';
  columns: number;
  baseRevision: number;
  renderer: string;
  width: number;
  height: number;
}

/**
 * Repairs malformed mobile rectangles locally while preserving independently valid x/w and y
 * priority; overlapping repaired rectangles move only downward. Valid owner layouts pass through
 * unchanged, and no recovery is persisted until an explicit Save.
 *
 * @param placements Saved or draft mobile rectangles.
 * @param instances Dashboard instances and their renderer minimum sizes.
 * @param columns Stored mobile column count.
 * @returns Valid recovered placements, unchanged valid placements, or an unavailable result.
 */
function projectMobilePlacements(
  placements: DashboardPlacement[],
  instances: GadgetInstance[],
  columns: number,
): MobileProjection {
  const minimumSizes = new Map(instances.map((instance) => [
    instance.id,
    RENDERER_MIN_SIZES[instance.definition.renderer] ?? { minW: 4, minH: 3 },
  ]));
  /** Marks mobile projection as unavailable when ownership or bounded geometry is invalid. */
  const unavailable = (): MobileProjection => ({ placements: [], unavailable: true });
  if (!Number.isInteger(columns) || columns < 1 || columns > 20
    || [...minimumSizes.values()].some(({ minW }) => minW > columns)) {
    return unavailable();
  }

  const placementIds = new Set(placements.map((placement) => placement.instance_id));
  if (placements.length !== instances.length || placementIds.size !== placements.length
    || instances.some((instance) => !placementIds.has(instance.id))) {
    return unavailable();
  }

  const minimumRows = placements.reduce(
    (total, placement) => total + (minimumSizes.get(placement.instance_id)?.minH ?? 3), 0,
  );
  if (minimumRows > MAX_LAYOUT_ROWS) return unavailable();
  /** Checks a single rectangle against its renderer minimum and the bounded mobile canvas. */
  const validGeometry = (placement: DashboardPlacement) => {
    const minimum = minimumSizes.get(placement.instance_id);
    return minimum !== undefined
      && Number.isInteger(placement.x) && Number.isInteger(placement.y)
      && Number.isInteger(placement.w) && Number.isInteger(placement.h)
      && placement.x >= 0 && placement.y >= 0 && placement.w >= minimum.minW
      && placement.h >= minimum.minH && placement.x + placement.w <= columns
      && placement.y + placement.h <= MAX_LAYOUT_ROWS;
  };
  const noOverlap = placements.every((placement, index) =>
    placements.slice(index + 1).every((other) => !doPlacementsOverlap(placement, other)),
  );
  if (placements.every(validGeometry) && noOverlap) return { placements, unavailable: false };

  // Stable total ordering prevents NaN subtraction from making repair order engine-dependent.
  const ordered = [...placements].sort((a, b) => {
    /** Orders finite coordinates before malformed values for deterministic local recovery. */
    const compareCoordinate = (left: number, right: number) => {
      const leftFinite = Number.isFinite(left);
      const rightFinite = Number.isFinite(right);
      return leftFinite && rightFinite ? left - right : leftFinite ? -1 : rightFinite ? 1 : 0;
    };
    return compareCoordinate(a.y, b.y) || compareCoordinate(a.x, b.x)
      || a.instance_id.localeCompare(b.instance_id);
  });
  const repaired: DashboardPlacement[] = [];
  for (const source of ordered) {
    const minimum = minimumSizes.get(source.instance_id);
    if (!minimum) return unavailable();
    const xValid = Number.isInteger(source.x) && source.x >= 0;
    const wValid = Number.isInteger(source.w) && source.w >= minimum.minW
      && source.w <= 20 && xValid && source.x + source.w <= columns;
    const candidate: DashboardPlacement = {
      ...source,
      x: wValid ? source.x : 0,
      w: wValid ? source.w : minimum.minW,
      h: Number.isInteger(source.h) && source.h >= minimum.minH && source.h <= MAX_LAYOUT_ROWS
        ? source.h : minimum.minH,
      y: Number.isInteger(source.y) && source.y >= 0 && source.y <= MAX_LAYOUT_ROWS
        ? source.y : 0,
    };
    while (repaired.some((other) => doPlacementsOverlap(candidate, other))) {
      const colliding = repaired.filter((other) => doPlacementsOverlap(candidate, other));
      const nextY = Math.max(...colliding.map((other) => other.y + other.h));
      if (nextY <= candidate.y || nextY + candidate.h > MAX_LAYOUT_ROWS) return unavailable();
      candidate.y = nextY;
    }
    if (candidate.x + candidate.w > columns || candidate.y + candidate.h > MAX_LAYOUT_ROWS) return unavailable();
    repaired.push(candidate);
  }
  return { placements: placements.map((placement) => repaired.find((item) => item.instance_id === placement.instance_id)!), unavailable: false };
}

/**
 * Main dashboard container view for BBD-OS.
 * Supports owner-scoped desktop/mobile drafts, local mobile reading projection/adoption, complete
 * group-independent geometry history, dashboard switching, and dirty navigation guarding.
 *
 * @returns Authenticated dashboard view component.
 */
export function DashboardPage() {
  const t = useTranslations('dashboard');
  const session = useWorkspaceSession();
  const queryClient = useQueryClient();
  const guardedNavigation = useGuardedNavigation();
  const { openDrawer } = useChatController();
  const display = useDisplayPreferences();

  // Active dashboard selection
  const [chosenDashboardId, setSelectedDashboardId] = useState<string | null>(null);
  const [isEditMode, setIsEditMode] = useState<boolean>(false);
  const [activeGroupId, setActiveGroupId] = useState<string>('all');

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
  const pendingRouteNavigationRef = useRef<GuardedNavigationIntent | null>(null);
  const addReservationsRef = useRef(new Map<string, AddReservation>());
  const addReservationSequenceRef = useRef(0);
  const saveOperationPendingRef = useRef(false);
  const membershipOperationCountRef = useRef(0);
  const pendingBreakpointRef = useRef<'desktop' | 'mobile' | null>(null);
  const dismissedBreakpointRef = useRef<'desktop' | 'mobile' | null>(null);
  const [editOrigin, setEditOrigin] = useState<LayoutEditOrigin | null>(null);

  // Layout placements draft and history stack
  const [draftPlacements, setDraftPlacements] = useState<DashboardPlacement[]>([]);
  const [readablePlacements, setReadablePlacements] = useState<DashboardPlacement[] | null>(null);
  const [gridMetrics, setGridMetrics] = useState<{ cellSize: number; stride: number; frameHeaderHeight: number } | null>(null);
  const [history, setHistory] = useState<DashboardPlacement[][]>([]);
  const [historyIndex, setHistoryIndex] = useState<number>(-1);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [membershipMutationsPending, setMembershipMutationsPending] = useState(0);

  // Reading stability queues (queued item count per gadget instance)
  const [readingQueues, setReadingQueues] = useState<Record<string, number>>({});

  // Responsive breakpoint tracking
  const isMobile = useSyncExternalStore(subscribeMobileMedia, getMobileMedia, getServerMobileMedia);

  // Fetch dashboards list
  const dashboardsQuery = useQuery({
    queryKey: dashboardKeys.all,
    queryFn: ({ signal }) => listDashboards(signal),
  });

  const dashboards = dashboardsQuery.data ?? [];

  // Fall back to the first dashboard until the owner picks one
  const selectedDashboardId = chosenDashboardId ?? dashboards[0]?.id ?? null;

  // Fetch active dashboard details
  const activeDashboardQuery = useQuery({
    queryKey: dashboardKeys.detail(selectedDashboardId || ''),
    queryFn: ({ signal }) => getDashboard(selectedDashboardId!, signal),
    enabled: Boolean(selectedDashboardId),
  });

  const activeDashboard = activeDashboardQuery.data ?? null;

  // Stale sources: sources this dashboard reads whose latest error is newer than the latest success.
  const sourcesQuery = useQuery({
    queryKey: [...sourceKeys.list, 'dashboard-stale'],
    queryFn: () => listSources(),
    enabled: Boolean(activeDashboard),
  });
  const dashboardSourceIds = new Set(activeDashboard?.instances.flatMap((i) => i.definition.source_ids) ?? []);
  const staleSources = (sourcesQuery.data?.items ?? []).filter((s) =>
    dashboardSourceIds.has(s.id) && s.status === 'active' && s.last_error_at
    && (!s.last_success_at || Date.parse(s.last_error_at) > Date.parse(s.last_success_at)));
  const requestedBreakpoint = isMobile ? 'mobile' : 'desktop';
  const currentBreakpoint = isDirty && editOrigin?.dashboardId === selectedDashboardId
    ? editOrigin.breakpoint : requestedBreakpoint;
  const layoutColumns = (editOrigin?.dashboardId === selectedDashboardId
    ? editOrigin.columns : activeDashboard?.layouts[currentBreakpoint].columns) ?? 20;
  const layoutIdentityRef = useRef({
    dashboardId: selectedDashboardId,
    breakpoint: currentBreakpoint,
    columns: layoutColumns,
    baseRevision: activeDashboard?.revision ?? null,
  });
  useLayoutEffect(() => {
    layoutIdentityRef.current = {
      dashboardId: selectedDashboardId,
      breakpoint: currentBreakpoint,
      columns: layoutColumns,
      baseRevision: activeDashboard?.revision ?? null,
    };
  });
  const liveLayoutRef = useRef({ activeDashboard, draftPlacements });
  useLayoutEffect(() => { liveLayoutRef.current = { activeDashboard, draftPlacements }; });
  const mobileProjection = useMemo(
    () => activeDashboard
      ? projectMobilePlacements(
          activeDashboard.layouts.mobile.items,
          activeDashboard.instances,
          activeDashboard.layouts.mobile.columns,
        )
      : { placements: [], unavailable: false },
    [activeDashboard],
  );
  const mobileProjectionUnavailable = currentBreakpoint === 'mobile' && mobileProjection.unavailable;
  const canEditLayout = !mobileProjectionUnavailable;
  const editMode = isEditMode && canEditLayout;
  const savedPlacements = useMemo(() => {
    if (!activeDashboard) return [];
    return currentBreakpoint === 'mobile' ? mobileProjection.placements : activeDashboard.layouts.desktop.items;
  }, [activeDashboard, currentBreakpoint, mobileProjection.placements]);

  /** Freezes the dirty source layout across media changes and requests an explicit decision. */
  useEffect(() => {
    if (!isDirty || !editOrigin || editOrigin.dashboardId !== selectedDashboardId) return;
    if (requestedBreakpoint === editOrigin.breakpoint) {
      pendingBreakpointRef.current = null;
      dismissedBreakpointRef.current = null;
      return;
    }
    pendingBreakpointRef.current = requestedBreakpoint;
    if (dismissedBreakpointRef.current !== requestedBreakpoint && !dirtyModalOpen) {
      setDirtyModalOpen(true);
    }
  }, [dirtyModalOpen, editOrigin, isDirty, requestedBreakpoint, selectedDashboardId]);

  // Reset clean history only when its owner or saved baseline actually changes.
  useEffect(() => {
    if (isDirty) return;
    const originMatches = editOrigin?.dashboardId === selectedDashboardId
      && editOrigin.breakpoint === requestedBreakpoint
      && editOrigin.columns === layoutColumns
      && sameLayout(editOrigin.savedSnapshot, savedPlacements);
    if (originMatches) return;
    // Re-baselines the draft when its owner or saved layout changes; derived state cannot hold undo history.
    /* eslint-disable react-hooks/set-state-in-effect */
    setDraftPlacements(savedPlacements);
    setHistory([savedPlacements.map((placement) => ({ ...placement }))]);
    setHistoryIndex(0);
    if (editOrigin) setEditOrigin(null);
    /* eslint-enable react-hooks/set-state-in-effect */
  }, [editOrigin, isDirty, layoutColumns, requestedBreakpoint, savedPlacements, selectedDashboardId]);

  // Register GuardedNavigation leave guard for dirty layout edits
  useEffect(() => {
    return guardedNavigation.registerLeaveGuard({
      hasUnsavedChanges: () => isDirty,
      confirmDiscard: (intent) => {
        if (intent) pendingRouteNavigationRef.current = intent;
        setDirtyModalOpen(true);
        return false;
      },
      acceptLeave: () => {
        setIsDirty(false);
        setIsEditMode(false);
        setEditOrigin(null);
      },
    });
  }, [guardedNavigation, isDirty]);

  /** Pushes one complete, validated placements snapshot to the originating undo/redo history. */
  const pushHistory = useCallback((nextPlacements: DashboardPlacement[]) => {
    if (saveOperationPendingRef.current || membershipOperationCountRef.current > 0) return;
    const origin = editOrigin ?? (activeDashboard ? {
      dashboardId: activeDashboard.id,
      breakpoint: currentBreakpoint,
      columns: layoutColumns,
      baseRevision: activeDashboard.revision,
      savedSnapshot: savedPlacements.map((placement) => ({ ...placement })),
    } : null);
    if (!activeDashboard || !origin || origin.dashboardId !== activeDashboard.id
      || !isValidLayoutSnapshot(nextPlacements, activeDashboard.instances, origin.columns)) {
      setSaveError(t('layoutActionUnavailable'));
      return;
    }
    const dirty = origin ? !sameLayout(nextPlacements, origin.savedSnapshot) : true;
    setHistory((prev) => {
      const truncated = prev.slice(0, historyIndex + 1);
      return [...truncated, nextPlacements];
    });
    setHistoryIndex((prev) => prev + 1);
    setDraftPlacements(nextPlacements);
    setIsDirty(dirty);
    setEditOrigin(origin);
  }, [activeDashboard, currentBreakpoint, editOrigin, historyIndex, layoutColumns, savedPlacements, t]);

  /** Handles Undo action from layout editor toolbar. */
  const handleUndo = useCallback(() => {
    if (saveOperationPendingRef.current || membershipOperationCountRef.current > 0) return;
    if (historyIndex > 0) {
      const nextIndex = historyIndex - 1;
      setHistoryIndex(nextIndex);
      setDraftPlacements(history[nextIndex]);
      const origin = editOrigin;
      const dirty = origin ? !sameLayout(history[nextIndex], origin.savedSnapshot) : true;
      setIsDirty(dirty);
    }
  }, [editOrigin, history, historyIndex]);

  /** Handles Redo action from layout editor toolbar. */
  const handleRedo = useCallback(() => {
    if (saveOperationPendingRef.current || membershipOperationCountRef.current > 0) return;
    if (historyIndex < history.length - 1) {
      const nextIndex = historyIndex + 1;
      setHistoryIndex(nextIndex);
      setDraftPlacements(history[nextIndex]);
      const origin = editOrigin;
      const dirty = origin ? !sameLayout(history[nextIndex], origin.savedSnapshot) : true;
      setIsDirty(dirty);
    }
  }, [editOrigin, history, historyIndex]);

  /** Captures the owner layout session and exact geometry before starting an asynchronous save. */
  const captureLayoutSaveRequest = useCallback((): LayoutSaveRequest | null => {
    if (!activeDashboard || mobileProjectionUnavailable) {
      setSaveError(t('layoutActionUnavailable'));
      return null;
    }
    const origin = editOrigin ?? {
      dashboardId: activeDashboard.id,
      breakpoint: currentBreakpoint,
      columns: layoutColumns,
      baseRevision: activeDashboard.revision,
      savedSnapshot: savedPlacements.map((placement) => ({ ...placement })),
    };
    if (origin.dashboardId !== activeDashboard.id
      || !isValidLayoutSnapshot(draftPlacements, activeDashboard.instances, origin.columns)) {
      setSaveError(t('layoutActionUnavailable'));
      return null;
    }
    return {
      ...origin,
      savedSnapshot: origin.savedSnapshot.map((placement) => ({ ...placement })),
      items: draftPlacements.map((placement) => ({ ...placement })),
    };
  }, [activeDashboard, currentBreakpoint, draftPlacements, editOrigin, layoutColumns, mobileProjectionUnavailable, savedPlacements, t]);

  // Layout save uses only captured origin identity and geometry, even if media changes in flight.
  const saveLayoutMutation = useMutation({
    mutationFn: async (request: LayoutSaveRequest) => {
      setSaveError(null);
      return replaceDashboardLayout(
        request.dashboardId,
        {
          expected_revision: request.baseRevision,
          breakpoint: request.breakpoint,
          columns: request.columns,
          items: request.items,
        },
        session.csrfToken,
      );
    },
    onSuccess: (updated, request) => {
      if (updated.id !== request.dashboardId) {
        setSaveError(t('saveFailed'));
        return;
      }
      queryClient.setQueryData(dashboardKeys.detail(updated.id), updated);
      void queryClient.invalidateQueries({ queryKey: dashboardKeys.all });
      setIsDirty(false);
      setEditOrigin(null);
      pendingBreakpointRef.current = null;
      dismissedBreakpointRef.current = null;
      setHistory([request.items]);
      setDraftPlacements(request.items);
      setHistoryIndex(0);
    },
    onError: (error) => {
      // Failed save retains draft state!
      setSaveError(error instanceof Error ? error.message : t('saveFailed'));
    },
  });
  const layoutOperationPending = saveLayoutMutation.isPending || membershipMutationsPending > 0;

  /** Checks bounded space before a quick definition or instance can be created. */
  const canAddGadgetInstance = useCallback((renderer: string): boolean => {
    if (!activeDashboard || layoutOperationPending || membershipOperationCountRef.current > 0) return false;
    const minimum = RENDERER_MIN_SIZES[renderer] ?? { minW: 4, minH: 3 };
    const width = currentBreakpoint === 'mobile' ? layoutColumns : Math.min(minimum.minW, layoutColumns);
    const height = currentBreakpoint === 'mobile' && gridMetrics
      ? Math.max(minimum.minH, Math.ceil((getGadgetReadingBodyFloor(renderer) + gridMetrics.frameHeaderHeight + 2 + 12) / gridMetrics.stride))
      : minimum.minH;
    if (findFreeCoordinates(draftPlacements, width, height, layoutColumns)) return true;
    setSaveError(t('noFreeDashboardSpace'));
    return false;
  }, [activeDashboard, currentBreakpoint, draftPlacements, gridMetrics, layoutColumns, layoutOperationPending, t]);

  /** Reconciles an already-committed membership response without rebinding its local draft owner. */
  const reconcileMembershipDashboard = useCallback((requestedDashboardId: string, updated: Dashboard) => {
    if (updated.id === requestedDashboardId) {
      const cached = queryClient.getQueryData<Dashboard>(dashboardKeys.detail(updated.id));
      if (!cached || cached.revision <= updated.revision) {
        queryClient.setQueryData(dashboardKeys.detail(updated.id), updated);
      }
    }
    void queryClient.invalidateQueries({ queryKey: dashboardKeys.detail(requestedDashboardId) });
    void queryClient.invalidateQueries({ queryKey: dashboardKeys.all });
  }, [queryClient]);

  /** Reserves owner identity and a unique operation before quick-definition network work begins. */
  const beginAddGadgetInstance = useCallback((renderer: string): string | null => {
    if (!activeDashboard || !canAddGadgetInstance(renderer) || membershipOperationCountRef.current > 0) return null;
    const minimum = RENDERER_MIN_SIZES[renderer] ?? { minW: 4, minH: 3 };
    const width = currentBreakpoint === 'mobile' ? layoutColumns : Math.min(minimum.minW, layoutColumns);
    const height = currentBreakpoint === 'mobile' && gridMetrics
      ? Math.max(minimum.minH, Math.ceil((getGadgetReadingBodyFloor(renderer) + gridMetrics.frameHeaderHeight + 2 + 12) / gridMetrics.stride))
      : minimum.minH;
    const reservationId = `dashboard-add-${++addReservationSequenceRef.current}`;
    addReservationsRef.current.set(reservationId, {
      dashboardId: activeDashboard.id,
      breakpoint: currentBreakpoint,
      columns: layoutColumns,
      baseRevision: activeDashboard.revision,
      renderer,
      width,
      height,
    });
    membershipOperationCountRef.current += 1;
    setMembershipMutationsPending((pending) => pending + 1);
    return reservationId;
  }, [activeDashboard, canAddGadgetInstance, currentBreakpoint, gridMetrics, layoutColumns]);

  /** Releases one Add reservation idempotently after either stage of creation finishes. */
  const releaseAddReservation = useCallback((reservationId: string) => {
    if (!addReservationsRef.current.delete(reservationId)) return;
    membershipOperationCountRef.current = Math.max(0, membershipOperationCountRef.current - 1);
    setMembershipMutationsPending((pending) => Math.max(0, pending - 1));
  }, []);

  /** Triggered by Save button; the mutation never rereads viewport-derived identity. */
  const handleSave = useCallback(() => {
    if (saveOperationPendingRef.current || membershipOperationCountRef.current > 0) return;
    const request = captureLayoutSaveRequest();
    if (!request) return;
    saveOperationPendingRef.current = true;
    void saveLayoutMutation.mutateAsync(request).finally(() => { saveOperationPendingRef.current = false; });
  }, [captureLayoutSaveRequest, saveLayoutMutation]);

  /** Request to exit edit mode or switch dashboard. */
  const requestExitEdit = useCallback(() => {
    if (layoutOperationPending || membershipOperationCountRef.current > 0) return;
    if (isDirty) {
      pendingExitEditRef.current = true;
      setDirtyModalOpen(true);
    } else {
      setIsEditMode(false);
      setEditOrigin(null);
    }
  }, [isDirty, layoutOperationPending]);

  /** Request switching to another dashboard (with dirty protection). */
  const requestSwitchDashboard = useCallback(
    (newId: string) => {
      if (layoutOperationPending || membershipOperationCountRef.current > 0) return;
      if (newId === selectedDashboardId) return;
      if (isDirty) {
        pendingDashboardSwitchRef.current = newId;
        setDirtyModalOpen(true);
      } else {
        setSelectedDashboardId(newId);
        setActiveGroupId('all');
      }
    },
    [isDirty, layoutOperationPending, selectedDashboardId],
  );

  /** Resolves the dirty modal selection: save, discard, or stay. */
  const handleDirtyModalResolution = useCallback(
    async (action: 'save' | 'discard' | 'stay') => {
      setDirtyModalOpen(false);

      if (action === 'stay') {
        pendingDashboardSwitchRef.current = null;
        pendingExitEditRef.current = false;
        pendingRouteNavigationRef.current = null;
        dismissedBreakpointRef.current = pendingBreakpointRef.current;
        pendingBreakpointRef.current = null;
        return;
      }

      if (action === 'discard') {
        const routeIntent = pendingRouteNavigationRef.current;
        pendingRouteNavigationRef.current = null;
        setIsDirty(false);
        setEditOrigin(null);
        setDraftPlacements(savedPlacements);
        setHistory([savedPlacements]);
        setHistoryIndex(0);
        pendingBreakpointRef.current = null;
        dismissedBreakpointRef.current = null;
        if (routeIntent) {
          pendingDashboardSwitchRef.current = null;
          pendingExitEditRef.current = false;
          guardedNavigation.continueNavigation(routeIntent);
          return;
        }
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
        if (saveOperationPendingRef.current || membershipOperationCountRef.current > 0) return;
        const request = captureLayoutSaveRequest();
        if (!request) {
          if (pendingRouteNavigationRef.current) setDirtyModalOpen(true);
          return;
        }
        saveOperationPendingRef.current = true;
        try {
          await saveLayoutMutation.mutateAsync(request);
          const routeIntent = pendingRouteNavigationRef.current;
          pendingRouteNavigationRef.current = null;
          if (routeIntent) {
            pendingDashboardSwitchRef.current = null;
            pendingExitEditRef.current = false;
            guardedNavigation.continueNavigation(routeIntent);
            return;
          }
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
          if (pendingRouteNavigationRef.current) setDirtyModalOpen(true);
          else {
            pendingDashboardSwitchRef.current = null;
            pendingExitEditRef.current = false;
          }
        } finally {
          saveOperationPendingRef.current = false;
        }
      }
    },
    [captureLayoutSaveRequest, guardedNavigation, saveLayoutMutation, savedPlacements],
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
      if (layoutOperationPending) return;
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

  /** Captures owner identity before membership creation; late results cannot rebind another layout. */
  const handleAddGadgetInstance = useCallback(
    async (params: { definitionId: string; groupId: string; title?: string; renderer: string }, reservationId: string) => {
      const reservation = addReservationsRef.current.get(reservationId);
      if (!reservation || reservation.renderer !== params.renderer) {
        releaseAddReservation(reservationId);
        return;
      }
      try {
        const live = liveLayoutRef.current;
        const preflightIdentity = layoutIdentityRef.current;
        if (!live.activeDashboard
          || live.activeDashboard.id !== reservation.dashboardId
          || preflightIdentity.dashboardId !== reservation.dashboardId
          || preflightIdentity.breakpoint !== reservation.breakpoint
          || preflightIdentity.columns !== reservation.columns
          || preflightIdentity.baseRevision !== reservation.baseRevision
          || live.activeDashboard.revision !== reservation.baseRevision) return;
        // Recheck live occupancy after asynchronous definition creation, immediately before POST.
        const coords = findFreeCoordinates(live.draftPlacements, reservation.width, reservation.height, reservation.columns);
        if (!coords) {
          setSaveError(t('noFreeDashboardSpace'));
          return;
        }
        const updatedDashboard = await createGadgetInstance(
          reservation.dashboardId,
          {
            expected_revision: reservation.baseRevision,
            group_id: params.groupId,
            definition_id: params.definitionId,
            title: params.title,
          },
          session.csrfToken,
        );
        reconcileMembershipDashboard(reservation.dashboardId, updatedDashboard);

        const responseIdentity = layoutIdentityRef.current;
        if (updatedDashboard.id !== reservation.dashboardId
          || responseIdentity.dashboardId !== reservation.dashboardId
          || responseIdentity.breakpoint !== reservation.breakpoint
          || responseIdentity.columns !== reservation.columns
          || (responseIdentity.baseRevision !== reservation.baseRevision
            && responseIdentity.baseRevision !== updatedDashboard.revision)) return;
        // Find newly added instance
        const newInstance = updatedDashboard.instances.find(
          (inst) => !live.activeDashboard!.instances.some((old) => old.id === inst.id),
        );

        if (newInstance) {
          const newPlacement: DashboardPlacement = {
            instance_id: newInstance.id,
            x: coords.x,
            y: coords.y,
            w: reservation.width,
            h: reservation.height,
          };

          const nextPlacements = [...live.draftPlacements, newPlacement];
          const serverSnapshot = reservation.breakpoint === 'mobile'
            ? projectMobilePlacements(updatedDashboard.layouts.mobile.items, updatedDashboard.instances, updatedDashboard.layouts.mobile.columns)
            : { placements: updatedDashboard.layouts.desktop.items, unavailable: false };
          if (serverSnapshot.unavailable) {
            setSaveError(t('mobileLayoutUnavailable'));
            return;
          }
          const origin: LayoutEditOrigin = {
            dashboardId: updatedDashboard.id,
            breakpoint: reservation.breakpoint,
            columns: reservation.columns,
            baseRevision: updatedDashboard.revision,
            savedSnapshot: serverSnapshot.placements.map((placement) => ({ ...placement })),
          };
          const dirty = !sameLayout(nextPlacements, origin.savedSnapshot);
          setDraftPlacements(nextPlacements);
          setHistory([nextPlacements]);
          setHistoryIndex(0);
          setIsDirty(dirty);
          setEditOrigin(dirty ? origin : null);
        }
      } catch (err) {
        setSaveError(err instanceof Error ? err.message : t('saveFailed'));
      } finally {
        releaseAddReservation(reservationId);
      }
    },
    [layoutIdentityRef, reconcileMembershipDashboard, releaseAddReservation, session.csrfToken, t],
  );

  /** Captures owner identity before removal; accepted server membership resets layout history. */
  const handleRemoveInstance = useCallback(
    async (instanceId: string) => {
      if (!activeDashboard) return;
      if (layoutOperationPending || membershipOperationCountRef.current > 0) return;
      const requestedIdentity = {
        dashboardId: activeDashboard.id,
        breakpoint: currentBreakpoint,
        columns: layoutColumns,
        baseRevision: activeDashboard.revision,
      };
      membershipOperationCountRef.current += 1;
      setMembershipMutationsPending((pending) => pending + 1);
      try {
        const updatedDashboard = await deleteGadgetInstance(
          activeDashboard.id,
          instanceId,
          activeDashboard.revision,
          session.csrfToken,
        );
        reconcileMembershipDashboard(requestedIdentity.dashboardId, updatedDashboard);
        const currentIdentity = layoutIdentityRef.current;
        if (updatedDashboard.id !== requestedIdentity.dashboardId
          || currentIdentity.dashboardId !== requestedIdentity.dashboardId
          || currentIdentity.breakpoint !== requestedIdentity.breakpoint
          || currentIdentity.columns !== requestedIdentity.columns
          || (currentIdentity.baseRevision !== requestedIdentity.baseRevision
            && currentIdentity.baseRevision !== updatedDashboard.revision)) return;
        const filtered = draftPlacements.filter((p) => p.instance_id !== instanceId);
        const serverSnapshot = requestedIdentity.breakpoint === 'mobile'
          ? projectMobilePlacements(updatedDashboard.layouts.mobile.items, updatedDashboard.instances, updatedDashboard.layouts.mobile.columns)
          : { placements: updatedDashboard.layouts.desktop.items, unavailable: false };
        if (serverSnapshot.unavailable) {
          setSaveError(t('mobileLayoutUnavailable'));
          return;
        }
        const origin: LayoutEditOrigin = {
          dashboardId: updatedDashboard.id,
          breakpoint: requestedIdentity.breakpoint,
          columns: requestedIdentity.columns,
          baseRevision: updatedDashboard.revision,
          savedSnapshot: serverSnapshot.placements.filter((placement) => placement.instance_id !== instanceId).map((placement) => ({ ...placement })),
        };
        const dirty = !sameLayout(filtered, origin.savedSnapshot);
        setDraftPlacements(filtered);
        setHistory([filtered]);
        setHistoryIndex(0);
        setIsDirty(dirty);
        setEditOrigin(dirty ? origin : null);
      } catch (err) {
        setSaveError(err instanceof Error ? err.message : t('saveFailed'));
      } finally {
        membershipOperationCountRef.current = Math.max(0, membershipOperationCountRef.current - 1);
        setMembershipMutationsPending((pending) => Math.max(0, pending - 1));
      }
    },
    [activeDashboard, currentBreakpoint, draftPlacements, layoutColumns, layoutIdentityRef, layoutOperationPending, queryClient, reconcileMembershipDashboard, session.csrfToken, t],
  );

  /** Filter instances by active group tab. */
  const visibleInstances: GadgetInstance[] = useMemo(() => {
    if (!activeDashboard) return [];
    if (activeGroupId === 'all') return activeDashboard.instances;
    return activeDashboard.instances.filter((inst) => inst.group_id === activeGroupId);
  }, [activeDashboard, activeGroupId]);

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
      {/* Page header: dashboard name, saved layout revision and primary actions */}
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h1 className="truncate text-2xl font-bold text-foreground">{activeDashboard?.name ?? t('title')}</h1>
          <p className="text-sm text-muted-foreground">
            {t('pageSubtitle')}
            {activeDashboard ? <> {'·'} {t('layoutRevision', { revision: activeDashboard.revision })}</> : null}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Button type="button" variant="outline" className="min-h-11" onClick={() => openDrawer({ context: { kind: 'general' } })}>
            <Sparkles className="w-4 h-4" aria-hidden="true" />
            <span>{t('askAi')}</span>
          </Button>
          <Button
            type="button"
            className="min-h-11 font-semibold"
            disabled={layoutOperationPending || (!canEditLayout && !isEditMode)}
            onClick={() => { if (isEditMode) requestExitEdit(); else setIsEditMode(true); }}
          >
            {isEditMode ? <Eye className="w-4 h-4" aria-hidden="true" /> : <Edit3 className="w-4 h-4" aria-hidden="true" />}
            <span>{isEditMode ? t('finishEditing') : t('toggleEdit')}</span>
          </Button>
        </div>
      </div>
      {/* Dashboard Top Header Bar */}
      <div className="flex flex-wrap items-center justify-between gap-4 pb-2 border-b border-border">
        {/* Left: Switcher, Active Title, Group Tabs */}
        <div className="flex flex-wrap items-center gap-3">
          {/* Dashboard Switcher Dropdown */}
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <button
                type="button"
                disabled={layoutOperationPending}
                className="inline-flex items-center gap-2 px-3 py-1.5 rounded-lg border border-border bg-card text-foreground font-semibold text-sm min-h-11 hover:bg-primary/10 transition-colors"
              >
                <Layout className="w-4 h-4 text-primary" />
                <span className="text-sm">{t('switchDashboard')}</span>
                <ChevronDown className="w-4 h-4 text-muted-foreground ml-1" />
              </button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="start" className="w-56">
              {dashboards.map((dash) => (
                <DropdownMenuItem
                  key={dash.id}
                  disabled={layoutOperationPending}
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
                disabled={layoutOperationPending}
                onClick={() => setCreateDashboardOpen(true)}
                className="text-xs gap-2 font-medium"
              >
                <Plus className="w-3.5 h-3.5" />
                <span>{t('newDashboard')}</span>
              </DropdownMenuItem>

              {activeDashboard && (
                <>
                  <DropdownMenuItem
                    disabled={layoutOperationPending}
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
                    disabled={layoutOperationPending}
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
          {editMode && activeDashboard && (
            <button
              type="button"
              disabled={layoutOperationPending}
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
            disabled={layoutOperationPending}
            className="secondary text-xs h-8 px-2.5 gap-1.5"
            onClick={() => setPresetPickerOpen(true)}
          >
            <Sparkles className="w-3.5 h-3.5 text-primary" />
            <span>{t('presets')}</span>
          </Button>
        </div>
      </div>

      {editOrigin && editOrigin.dashboardId === selectedDashboardId && requestedBreakpoint !== editOrigin.breakpoint && (
        <p role="status" className="rounded-md border border-primary/30 bg-primary/5 px-3 py-2 text-sm text-muted-foreground">
          {t('mobileEditOrigin', {
            breakpoint: t(editOrigin.breakpoint === 'mobile' ? 'mobileLayout' : 'desktopLayout'),
            requested: t(requestedBreakpoint === 'mobile' ? 'mobileLayout' : 'desktopLayout'),
          })}
        </p>
      )}

      {mobileProjectionUnavailable && (
        <p role="alert" className="rounded-md border border-destructive/40 bg-destructive/5 px-3 py-2 text-sm text-destructive">
          {t('mobileLayoutUnavailable')}
        </p>
      )}

      {activeDashboard && currentBreakpoint === 'mobile' && !editMode && !mobileProjectionUnavailable && readablePlacements && (
        <p role="status" className="rounded-md border border-primary/30 bg-primary/5 px-3 py-2 text-sm text-muted-foreground">
          {t('mobileReadingProjection')}
        </p>
      )}

      {/* Layout Editor Toolbar (visible in Edit Mode) */}
      {editMode && (
        <LayoutEditor
          dashboardName={activeDashboard?.name ?? ''}
          editCount={isDirty ? Math.max(1, historyIndex) : 0}
          baseRevision={editOrigin?.baseRevision ?? activeDashboard?.revision}
          isDirty={isDirty}
          isSaving={layoutOperationPending}
          canUndo={historyIndex > 0}
          canRedo={historyIndex < history.length - 1}
          onSave={handleSave}
          onCancel={requestExitEdit}
          onUndo={handleUndo}
          onRedo={handleRedo}
          onUseReadableMobileSizes={() => {
            if (currentBreakpoint === 'mobile' && readablePlacements) pushHistory(readablePlacements);
          }}
          canUseReadableMobileSizes={currentBreakpoint === 'mobile' && readablePlacements !== null}
          onOpenPresetPicker={() => setPresetPickerOpen(true)}
          onAddGadgetInstance={handleAddGadgetInstance}
          onBeginAddGadgetInstance={beginAddGadgetInstance}
          onCancelPendingAdd={releaseAddReservation}
          groups={activeDashboard?.groups ?? []}
          activeGroupId={activeGroupId === 'all' ? undefined : activeGroupId}
          dirtyModalOpen={dirtyModalOpen}
          dirtyDescription={editOrigin && requestedBreakpoint !== editOrigin.breakpoint
            ? t('mobileBreakpointDirtyWarning', {
                breakpoint: t(editOrigin.breakpoint === 'mobile' ? 'mobileLayout' : 'desktopLayout'),
              }) : undefined}
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
          <Layout className="w-10 h-10 text-muted-foreground mb-2" aria-hidden="true" />
          <h2 className="text-lg font-bold text-foreground mb-1">{t('emptyTitle')}</h2>
          <p className="text-sm text-muted-foreground max-w-md mb-4">{t('emptyDesc')}</p>
          <div className="flex flex-wrap justify-center gap-2">
            <Button
              type="button"
              variant="outline"
              className="min-h-11"
              disabled={!canEditLayout}
              onClick={() => {
                setIsEditMode(true);
              }}
            >
              <Plus className="w-4 h-4" aria-hidden="true" />
              {t('startEmpty')}
            </Button>
            <Button type="button" className="min-h-11" onClick={() => setPresetPickerOpen(true)}>
              <Sparkles className="w-4 h-4" aria-hidden="true" />
              {t('choosePreset')}
            </Button>
          </div>
        </div>
      ) : (
        <DashboardGrid
          instances={activeDashboard.instances}
          placements={mobileProjectionUnavailable ? [] : draftPlacements}
          visibleInstanceIds={visibleInstances.map((instance) => instance.id)}
          isMobileView={currentBreakpoint === 'mobile'}
          onReadableProjectionChange={setReadablePlacements}
          onGridMetricsChange={setGridMetrics}
          columns={layoutColumns}
          breakpoint={currentBreakpoint}
          requestedBreakpoint={requestedBreakpoint}
          interactionPending={layoutOperationPending}
          isEditMode={editMode && !layoutOperationPending}
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

      {activeDashboard && staleSources.length > 0 && (
        <section aria-label={t('staleSourcesTitle')} className="space-y-2 rounded-md border border-border bg-card p-3 text-sm">
          <h2 className="text-sm font-semibold">{t('staleSourcesTitle')}</h2>
          <ul className="space-y-2">
            {staleSources.map((s) => (
              <li key={s.id} className="space-y-0.5">
                <p>{t('staleSourceLine', { name: s.name, code: s.last_error_code ?? s.collection_error_code ?? s.processing_error_code ?? '—' })}</p>
                <p className="text-xs text-muted-foreground">
                  {t('staleSourceTimes', {
                    success: s.last_success_at ? formatDateTime(s.last_success_at, display.locale, display.timezone) : t('staleSourceNever'),
                    error: formatDateTime(s.last_error_at!, display.locale, display.timezone),
                  })}
                </p>
              </li>
            ))}
          </ul>
          <Link href="/settings/sources" className="inline-flex min-h-11 items-center text-xs font-semibold text-primary underline underline-offset-4">
            {t('openSourceStatus')}
          </Link>
        </section>
      )}

      {activeDashboard && (
        <p className="flex flex-wrap items-center gap-x-3 text-xs text-muted-foreground">
          <span>{t('timesShownIn', { timezone: display.timezone })}</span>
          <Link href="/settings/sources" className="inline-flex min-h-11 items-center text-primary underline underline-offset-4">
            {t('openSourceStatus')}
          </Link>
        </p>
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
              disabled={!newGroupName.trim() || addGroupMutation.isPending || layoutOperationPending}
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
