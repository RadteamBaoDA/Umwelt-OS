'use client';

import { useTranslations } from 'next-intl';
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { useGuardedNavigation, type GuardedNavigationIntent } from '@/core/guarded-navigation';
import type { Dashboard, DashboardPlacement } from './api';
import {
  isValidLayoutSnapshot,
  packPlacements,
  projectMobilePlacements,
  sameLayout,
  type AddReservation,
  type LayoutEditOrigin,
} from './layout-geometry';

/** Measured grid metrics reported by the grid, used to size newly added gadgets. */
export type GridMetrics = { cellSize: number; stride: number; frameHeaderHeight: number };

/** Layout identity captured every render so asynchronous membership results can detect stale ownership. */
export interface LayoutIdentity {
  dashboardId: string | null;
  breakpoint: 'desktop' | 'mobile';
  columns: number;
  baseRevision: number | null;
}

/**
 * Creates the layout-session state and refs (edit mode, draft, history, dirty tracking, pending intents).
 *
 * Pure bookkeeping: no effects and no network. Refs hold cross-callback flags that must not trigger renders
 * (in-flight save, membership operation count, pending dirty-modal intents).
 */
function useLayoutState() {
  const [isEditMode, setIsEditMode] = useState<boolean>(false);
  const [activeGroupId, setActiveGroupId] = useState<string>('all');
  const [isDirty, setIsDirty] = useState<boolean>(false);
  const [dirtyModalOpen, setDirtyModalOpen] = useState<boolean>(false);
  const [editOrigin, setEditOrigin] = useState<LayoutEditOrigin | null>(null);
  const [draftPlacements, setDraftPlacements] = useState<DashboardPlacement[]>([]);
  const [readablePlacements, setReadablePlacements] = useState<DashboardPlacement[] | null>(null);
  const [gridMetrics, setGridMetrics] = useState<GridMetrics | null>(null);
  const [history, setHistory] = useState<DashboardPlacement[][]>([]);
  const [historyIndex, setHistoryIndex] = useState<number>(-1);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [membershipMutationsPending, setMembershipMutationsPending] = useState(0);
  // Reading stability queues (queued item count per gadget instance)
  const [readingQueues, setReadingQueues] = useState<Record<string, number>>({});
  const pendingDashboardSwitchRef = useRef<string | null>(null);
  const pendingExitEditRef = useRef<boolean>(false);
  const pendingRouteNavigationRef = useRef<GuardedNavigationIntent | null>(null);
  const addReservationsRef = useRef(new Map<string, AddReservation>());
  const addReservationSequenceRef = useRef(0);
  const saveOperationPendingRef = useRef(false);
  const membershipOperationCountRef = useRef(0);
  const pendingBreakpointRef = useRef<'desktop' | 'mobile' | null>(null);
  const dismissedBreakpointRef = useRef<'desktop' | 'mobile' | null>(null);
  return {
    isEditMode, setIsEditMode, activeGroupId, setActiveGroupId, isDirty, setIsDirty, dirtyModalOpen, setDirtyModalOpen,
    editOrigin, setEditOrigin, draftPlacements, setDraftPlacements, readablePlacements, setReadablePlacements,
    gridMetrics, setGridMetrics, history, setHistory, historyIndex, setHistoryIndex, saveError, setSaveError,
    membershipMutationsPending, setMembershipMutationsPending, readingQueues, setReadingQueues,
    pendingDashboardSwitchRef, pendingExitEditRef, pendingRouteNavigationRef, addReservationsRef,
    addReservationSequenceRef, saveOperationPendingRef, membershipOperationCountRef, pendingBreakpointRef,
    dismissedBreakpointRef,
  };
}

type LayoutState = ReturnType<typeof useLayoutState>;

/** Inputs shared by the layout-session sub-hooks. */
interface LayoutInputs {
  selectedDashboardId: string | null;
  activeDashboard: Dashboard | null;
  isMobile: boolean;
}

/**
 * Derives the effective breakpoint, columns and saved placements for the layout session, and keeps the
 * live identity/layout refs current (layout effects run before passive effects, as before).
 *
 * A dirty edit session freezes its origin breakpoint/columns so media changes cannot rebind the draft.
 */
function useLayoutDerived(state: LayoutState, { selectedDashboardId, activeDashboard, isMobile }: LayoutInputs) {
  const { isDirty, editOrigin, draftPlacements, isEditMode } = state;
  const requestedBreakpoint: 'desktop' | 'mobile' = isMobile ? 'mobile' : 'desktop';
  const currentBreakpoint = isDirty && editOrigin?.dashboardId === selectedDashboardId
    ? editOrigin.breakpoint : requestedBreakpoint;
  const layoutColumns = (editOrigin?.dashboardId === selectedDashboardId
    ? editOrigin.columns : activeDashboard?.layouts[currentBreakpoint].columns) ?? 20;
  const layoutIdentityRef = useRef<LayoutIdentity>({
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
  return {
    requestedBreakpoint, currentBreakpoint, layoutColumns, layoutIdentityRef, liveLayoutRef,
    mobileProjectionUnavailable, canEditLayout, editMode, savedPlacements,
  };
}

type LayoutDerived = ReturnType<typeof useLayoutDerived>;

/**
 * Registers the three session effects in their original order: freeze-on-media-change, clean-history rebaseline
 * and the GuardedNavigation leave guard for dirty layouts.
 */
function useLayoutSessionEffects(state: LayoutState, derived: LayoutDerived, selectedDashboardId: string | null): void {
  const guardedNavigation = useGuardedNavigation();
  const { isDirty, editOrigin, dirtyModalOpen, setDirtyModalOpen, setEditOrigin, setIsDirty, setIsEditMode } = state;
  const { requestedBreakpoint, layoutColumns, savedPlacements } = derived;
  const { pendingBreakpointRef, dismissedBreakpointRef, pendingRouteNavigationRef } = state;
  const { setDraftPlacements, setHistory, setHistoryIndex } = state;

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
  }, [dirtyModalOpen, editOrigin, isDirty, requestedBreakpoint, selectedDashboardId, pendingBreakpointRef, dismissedBreakpointRef, setDirtyModalOpen]);

  // Reset clean history only when its owner or saved baseline actually changes.
  useEffect(() => {
    if (isDirty) return;
    const originMatches = editOrigin?.dashboardId === selectedDashboardId
      && editOrigin.breakpoint === requestedBreakpoint
      && editOrigin.columns === layoutColumns
      && sameLayout(editOrigin.savedSnapshot, savedPlacements);
    if (originMatches) return;
    // Re-baselines the draft when its owner or saved layout changes; derived state cannot hold undo history; the multi-field draft must rebaseline atomically.
    setDraftPlacements(savedPlacements);
    setHistory([savedPlacements.map((placement) => ({ ...placement }))]);
    setHistoryIndex(0);
    if (editOrigin) setEditOrigin(null);
  }, [editOrigin, isDirty, layoutColumns, requestedBreakpoint, savedPlacements, selectedDashboardId, setDraftPlacements, setEditOrigin, setHistory, setHistoryIndex]);

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
  }, [guardedNavigation, isDirty, pendingRouteNavigationRef, setDirtyModalOpen, setEditOrigin, setIsDirty, setIsEditMode]);
}

/**
 * Builds the undo/redo/tidy operations over the draft history.
 *
 * All operations are ignored while a save or membership mutation is in flight. `pushHistory` rejects snapshots
 * that fail geometry validation by surfacing the `layoutActionUnavailable` banner instead of changing state.
 */
function useLayoutHistory(state: LayoutState, derived: LayoutDerived, activeDashboard: Dashboard | null) {
  const t = useTranslations('dashboard');
  const { editOrigin, history, historyIndex, draftPlacements, setSaveError, setHistory, setHistoryIndex, setDraftPlacements, setIsDirty, setEditOrigin } = state;
  const { saveOperationPendingRef, membershipOperationCountRef } = state;
  const { currentBreakpoint, layoutColumns, savedPlacements } = derived;

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
  }, [activeDashboard, currentBreakpoint, editOrigin, historyIndex, layoutColumns, savedPlacements, t, saveOperationPendingRef, membershipOperationCountRef, setSaveError, setHistory, setHistoryIndex, setDraftPlacements, setIsDirty, setEditOrigin]);

  /** Moves the history cursor by `delta` when the target exists and recomputes the dirty flag against the origin. */
  const moveHistory = useCallback((delta: -1 | 1) => {
    if (saveOperationPendingRef.current || membershipOperationCountRef.current > 0) return;
    const nextIndex = historyIndex + delta;
    if (nextIndex < 0 || nextIndex > history.length - 1) return;
    setHistoryIndex(nextIndex);
    setDraftPlacements(history[nextIndex]);
    setIsDirty(editOrigin ? !sameLayout(history[nextIndex], editOrigin.savedSnapshot) : true);
  }, [editOrigin, history, historyIndex, saveOperationPendingRef, membershipOperationCountRef, setHistoryIndex, setDraftPlacements, setIsDirty]);
  /** Handles Undo action from layout editor toolbar. */
  const handleUndo = useCallback(() => moveHistory(-1), [moveHistory]);
  /** Handles Redo action from layout editor toolbar. */
  const handleRedo = useCallback(() => moveHistory(1), [moveHistory]);

  const tidiedPlacements = useMemo(() => packPlacements(draftPlacements, layoutColumns), [draftPlacements, layoutColumns]);
  const canTidyLayout = currentBreakpoint === 'desktop' && !sameLayout(tidiedPlacements, draftPlacements);
  /** Applies the packed layout through the normal draft/history path so Undo works. */
  const handleTidyLayout = useCallback(() => {
    if (canTidyLayout) pushHistory(tidiedPlacements);
  }, [canTidyLayout, pushHistory, tidiedPlacements]);

  return { pushHistory, handleUndo, handleRedo, canTidyLayout, handleTidyLayout };
}

/**
 * Owns the whole layout editing session of the dashboard page: edit mode, draft placements, undo/redo history,
 * dirty tracking against the saved baseline, breakpoint freezing and the leave guard.
 *
 * Network operations (save, add/remove gadget) live in `useLayoutSave` and `useGadgetMembership`, which receive
 * this session object.
 *
 * @param inputs Selected dashboard id, loaded dashboard and the phone media flag.
 * @returns State, refs, derived layout facts and history operations.
 */
export function useLayoutSession(inputs: LayoutInputs) {
  const state = useLayoutState();
  const derived = useLayoutDerived(state, inputs);
  useLayoutSessionEffects(state, derived, inputs.selectedDashboardId);
  const historyOps = useLayoutHistory(state, derived, inputs.activeDashboard);
  const { setReadingQueues } = state;
  /** Revealing / clearing reading updates for an instance. */
  const handleApplyReadingQueue = useCallback((instanceId: string) => {
    setReadingQueues((prev) => {
      const next = { ...prev };
      delete next[instanceId];
      return next;
    });
  }, [setReadingQueues]);
  return { ...state, ...derived, ...historyOps, handleApplyReadingQueue, activeDashboard: inputs.activeDashboard };
}

/** Return type of {@link useLayoutSession}. */
export type LayoutSession = ReturnType<typeof useLayoutSession>;
