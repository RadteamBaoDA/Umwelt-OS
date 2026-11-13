'use client';

import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslations } from 'next-intl';
import { useCallback } from 'react';
import { useWorkspaceSession } from '@/core/app-shell/workspace-shell';
import { useGuardedNavigation, type GuardedNavigationIntent } from '@/core/guarded-navigation';
import { dashboardKeys, replaceDashboardLayout } from './api';
import { isValidLayoutSnapshot, type LayoutSaveRequest } from './layout-geometry';
import type { LayoutSession } from './use-layout-session';

/** Choice offered by the dirty-layout confirmation modal. */
export type DirtyResolution = 'save' | 'discard' | 'stay';

/**
 * Builds the layout Save mutation. It uses only the captured request (origin identity and geometry), so a media
 * change while the request is in flight cannot rebind it. A failed save keeps the draft and shows the error.
 *
 * Side effects: PUT layout; on success replaces the dashboard detail cache, invalidates the dashboard list and
 * rebaselines the session to the saved items.
 */
function useSaveLayoutMutation(session: LayoutSession) {
  const t = useTranslations('dashboard');
  const workspace = useWorkspaceSession();
  const queryClient = useQueryClient();
  const {
    setSaveError,
    setIsDirty,
    setEditOrigin,
    setHistory,
    setDraftPlacements,
    setHistoryIndex,
    pendingBreakpointRef,
    dismissedBreakpointRef,
  } = session;
  return useMutation({
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
        workspace.csrfToken,
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
}

/** Collaborators of the dirty-modal resolution flow. */
interface DirtyResolutionDeps {
  captureLayoutSaveRequest: () => LayoutSaveRequest | null;
  saveLayoutMutation: ReturnType<typeof useSaveLayoutMutation>;
  navigateToDashboard: (id: string, replace?: boolean) => void;
}

/**
 * Builds the callback that releases whatever the dirty modal was guarding once the draft was saved or discarded.
 *
 * @param session The layout session (pending-intent refs and setters).
 * @param navigateToDashboard Navigates to another dashboard.
 * @returns Callback taking the pending route intent (or null); a route intent wins over a dashboard switch and
 * exit-edit, otherwise the switch runs first, then exit edit. Clears each pending ref it consumes.
 */
function useReleasePendingIntents(session: LayoutSession, navigateToDashboard: DirtyResolutionDeps['navigateToDashboard']) {
  const guardedNavigation = useGuardedNavigation();
  const { pendingExitEditRef, pendingDashboardSwitchRef, pendingGroupSwitchRef, setActiveGroupId, setIsEditMode } = session;
  /**
   * Releases whatever the dirty modal was guarding once the draft was saved or discarded: a pending route
   * navigation wins over a dashboard switch and exit-edit; otherwise switch first, then exit edit.
   */
  return useCallback(
    (routeIntent: GuardedNavigationIntent | null) => {
      if (routeIntent) {
        pendingDashboardSwitchRef.current = null;
        pendingGroupSwitchRef.current = null;
        pendingExitEditRef.current = false;
        guardedNavigation.continueNavigation(routeIntent);
        return;
      }
      if (pendingDashboardSwitchRef.current) {
        navigateToDashboard(pendingDashboardSwitchRef.current);
        pendingDashboardSwitchRef.current = null;
        setActiveGroupId('');
      }
      if (pendingGroupSwitchRef.current) {
        setActiveGroupId(pendingGroupSwitchRef.current);
        pendingGroupSwitchRef.current = null;
      }
      if (pendingExitEditRef.current) {
        setIsEditMode(false);
        pendingExitEditRef.current = false;
      }
    },
    [
      guardedNavigation,
      navigateToDashboard,
      pendingDashboardSwitchRef,
      pendingGroupSwitchRef,
      pendingExitEditRef,
      setActiveGroupId,
      setIsEditMode,
    ],
  );
}

/**
 * Builds the callback that discards the draft back to the saved layout and then releases pending intents.
 *
 * @param session The layout session.
 * @param releasePendingIntents Release callback from {@link useReleasePendingIntents}.
 * @returns Stable callback; resets dirty/origin/history state and the breakpoint refs.
 */
function useDiscardDraft(session: LayoutSession, releasePendingIntents: ReturnType<typeof useReleasePendingIntents>) {
  const { savedPlacements, pendingRouteNavigationRef, pendingBreakpointRef, dismissedBreakpointRef } = session;
  const { setEditOrigin, setIsDirty, setDraftPlacements, setHistory, setHistoryIndex } = session;
  return useCallback(() => {
    const routeIntent = pendingRouteNavigationRef.current;
    pendingRouteNavigationRef.current = null;
    setIsDirty(false);
    setEditOrigin(null);
    setDraftPlacements(savedPlacements);
    setHistory([savedPlacements]);
    setHistoryIndex(0);
    pendingBreakpointRef.current = null;
    dismissedBreakpointRef.current = null;
    releasePendingIntents(routeIntent);
  }, [
    dismissedBreakpointRef,
    pendingBreakpointRef,
    pendingRouteNavigationRef,
    releasePendingIntents,
    savedPlacements,
    setDraftPlacements,
    setEditOrigin,
    setHistory,
    setHistoryIndex,
    setIsDirty,
  ]);
}

/**
 * Builds the callback that saves the draft and, on success, releases pending intents.
 *
 * @param session The layout session.
 * @param deps Request capture and save mutation.
 * @param releasePendingIntents Release callback from {@link useReleasePendingIntents}.
 * @returns Stable async callback; ignored while a save or membership operation is in flight; on failure keeps
 * the draft (and reopens the modal for route intents).
 */
function useSaveDraftAndContinue(
  session: LayoutSession,
  deps: DirtyResolutionDeps,
  releasePendingIntents: ReturnType<typeof useReleasePendingIntents>,
) {
  const { captureLayoutSaveRequest, saveLayoutMutation } = deps;
  const { saveOperationPendingRef, membershipOperationCountRef, pendingExitEditRef, pendingDashboardSwitchRef } = session;
  const { pendingGroupSwitchRef, pendingRouteNavigationRef, setDirtyModalOpen } = session;
  return useCallback(async () => {
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
      releasePendingIntents(routeIntent);
    } catch {
      // If save fails, retain draft and remain on page
      if (pendingRouteNavigationRef.current) setDirtyModalOpen(true);
      else {
        pendingDashboardSwitchRef.current = null;
        pendingGroupSwitchRef.current = null;
        pendingExitEditRef.current = false;
      }
    } finally {
      saveOperationPendingRef.current = false;
    }
  }, [
    captureLayoutSaveRequest,
    membershipOperationCountRef,
    pendingDashboardSwitchRef,
    pendingGroupSwitchRef,
    pendingExitEditRef,
    pendingRouteNavigationRef,
    releasePendingIntents,
    saveLayoutMutation,
    saveOperationPendingRef,
    setDirtyModalOpen,
  ]);
}

/**
 * Resolves the dirty-layout modal (save / discard / stay) and releases the intents it was guarding.
 *
 * @param session The layout session.
 * @param deps Request capture, save mutation and dashboard navigation.
 * @returns Handler for the modal's resolution callback.
 */
function useDirtyModalResolution(session: LayoutSession, deps: DirtyResolutionDeps) {
  const { navigateToDashboard } = deps;
  const { pendingExitEditRef, pendingDashboardSwitchRef, pendingGroupSwitchRef, pendingRouteNavigationRef } = session;
  const { pendingBreakpointRef, dismissedBreakpointRef, setDirtyModalOpen } = session;

  const releasePendingIntents = useReleasePendingIntents(session, navigateToDashboard);

  /** Drops every pending intent and remembers the breakpoint the owner chose to stay on. */
  const stayOnDraft = useCallback(() => {
    pendingDashboardSwitchRef.current = null;
    pendingGroupSwitchRef.current = null;
    pendingExitEditRef.current = false;
    pendingRouteNavigationRef.current = null;
    dismissedBreakpointRef.current = pendingBreakpointRef.current;
    pendingBreakpointRef.current = null;
  }, [
    dismissedBreakpointRef,
    pendingBreakpointRef,
    pendingDashboardSwitchRef,
    pendingGroupSwitchRef,
    pendingExitEditRef,
    pendingRouteNavigationRef,
  ]);

  const discardDraft = useDiscardDraft(session, releasePendingIntents);

  const saveDraftAndContinue = useSaveDraftAndContinue(session, deps, releasePendingIntents);

  /** Resolves the dirty modal selection: save, discard, or stay. */
  return useCallback(
    async (action: DirtyResolution) => {
      setDirtyModalOpen(false);
      if (action === 'stay') stayOnDraft();
      else if (action === 'discard') discardDraft();
      else await saveDraftAndContinue();
    },
    [discardDraft, saveDraftAndContinue, setDirtyModalOpen, stayOnDraft],
  );
}

/**
 * Builds the capture function that snapshots the owner layout session and exact geometry before an async save.
 *
 * @param session The layout session.
 * @returns Stable callback returning a save request (origin identity, saved snapshot, draft items), or null after
 * setting an "unavailable" save error when there is no dashboard, the projection is unavailable, the origin no
 * longer matches the active dashboard/group, or the draft is not a valid snapshot. Copies placements so later
 * edits cannot mutate the request.
 */
function useCaptureLayoutSaveRequest(session: LayoutSession) {
  const t = useTranslations('dashboard');
  const {
    activeDashboard,
    activeGroupId,
    editOrigin,
    currentBreakpoint,
    layoutColumns,
    savedPlacements,
    draftPlacements,
    mobileProjectionUnavailable,
    setSaveError,
  } = session;
  return useCallback((): LayoutSaveRequest | null => {
    if (!activeDashboard || mobileProjectionUnavailable) {
      setSaveError(t('layoutActionUnavailable'));
      return null;
    }
    const origin = editOrigin ?? {
      dashboardId: activeDashboard.id,
      groupId: activeGroupId,
      breakpoint: currentBreakpoint,
      columns: layoutColumns,
      baseRevision: activeDashboard.revision,
      savedSnapshot: savedPlacements.map((placement) => ({ ...placement })),
    };
    if (
      origin.dashboardId !== activeDashboard.id ||
      origin.groupId !== activeGroupId ||
      !isValidLayoutSnapshot(draftPlacements, activeDashboard.instances, origin.columns)
    ) {
      setSaveError(t('layoutActionUnavailable'));
      return null;
    }
    return {
      ...origin,
      savedSnapshot: origin.savedSnapshot.map((placement) => ({ ...placement })),
      items: draftPlacements.map((placement) => ({ ...placement })),
    };
  }, [
    activeDashboard,
    activeGroupId,
    currentBreakpoint,
    draftPlacements,
    editOrigin,
    layoutColumns,
    mobileProjectionUnavailable,
    savedPlacements,
    setSaveError,
    t,
  ]);
}

/**
 * Builds the guarded requests that leave edit mode or change dashboard/panel.
 *
 * @param session The layout session.
 * @param layoutOperationPending True while a save or membership mutation is in flight (requests are ignored).
 * @param navigateToDashboard Navigates to another dashboard after the dirty guard passed.
 * @param selectedDashboardId Currently addressed dashboard.
 * @returns Stable callbacks; each ignores calls during pending operations, and when the draft is dirty records the
 * pending intent and opens the dirty modal instead of acting.
 */
function useLayoutRequests(
  session: LayoutSession,
  layoutOperationPending: boolean,
  navigateToDashboard: (id: string, replace?: boolean) => void,
  selectedDashboardId: string | null,
) {
  const { activeDashboard, activeGroupId, isDirty, membershipOperationCountRef } = session;
  const { pendingExitEditRef, pendingDashboardSwitchRef, pendingGroupSwitchRef } = session;
  const { setDirtyModalOpen, setIsEditMode, setEditOrigin, setActiveGroupId } = session;

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
  }, [isDirty, layoutOperationPending, membershipOperationCountRef, pendingExitEditRef, setDirtyModalOpen, setEditOrigin, setIsEditMode]);

  /** Request switching to another dashboard (with dirty protection). */
  const requestSwitchDashboard = useCallback(
    (newId: string) => {
      if (layoutOperationPending || membershipOperationCountRef.current > 0) return;
      if (newId === selectedDashboardId) return;
      if (isDirty) {
        pendingDashboardSwitchRef.current = newId;
        setDirtyModalOpen(true);
      } else {
        navigateToDashboard(newId);
        setActiveGroupId('');
      }
    },
    [
      isDirty,
      layoutOperationPending,
      membershipOperationCountRef,
      navigateToDashboard,
      pendingDashboardSwitchRef,
      selectedDashboardId,
      setActiveGroupId,
      setDirtyModalOpen,
    ],
  );

  /** Switches the active panel only after the current panel's dirty draft is saved or discarded. */
  const requestSwitchGroup = useCallback(
    (groupId: string) => {
      if (
        !activeDashboard?.groups.some((group) => group.id === groupId) ||
        groupId === activeGroupId ||
        layoutOperationPending ||
        membershipOperationCountRef.current > 0
      )
        return;
      if (isDirty) {
        pendingGroupSwitchRef.current = groupId;
        setDirtyModalOpen(true);
      } else {
        setActiveGroupId(groupId);
      }
    },
    [
      activeDashboard,
      activeGroupId,
      isDirty,
      layoutOperationPending,
      membershipOperationCountRef,
      pendingGroupSwitchRef,
      setActiveGroupId,
      setDirtyModalOpen,
    ],
  );
  return { requestExitEdit, requestSwitchDashboard, requestSwitchGroup };
}

/**
 * Save, exit-edit, dashboard-switch and dirty-modal flows of the layout session.
 *
 * Ordering invariants: a save marks `saveOperationPendingRef` for its whole duration; every request is ignored
 * while a save or a membership operation is in flight; pending intents (route, dashboard switch, exit edit) are
 * only released after the save or discard they were waiting on.
 *
 * @param session The layout session.
 * @param navigateToDashboard Navigates to another dashboard after the dirty guard passed.
 * @param selectedDashboardId Currently addressed dashboard.
 */
export function useLayoutSave(
  session: LayoutSession,
  navigateToDashboard: (id: string, replace?: boolean) => void,
  selectedDashboardId: string | null,
) {
  const saveLayoutMutation = useSaveLayoutMutation(session);
  const { membershipMutationsPending, saveOperationPendingRef, membershipOperationCountRef } = session;
  const layoutOperationPending = saveLayoutMutation.isPending || membershipMutationsPending > 0;

  const captureLayoutSaveRequest = useCaptureLayoutSaveRequest(session);

  /** Triggered by Save button; the mutation never rereads viewport-derived identity. */
  const handleSave = useCallback(() => {
    if (saveOperationPendingRef.current || membershipOperationCountRef.current > 0) return;
    const request = captureLayoutSaveRequest();
    if (!request) return;
    saveOperationPendingRef.current = true;
    // The mutation's onError already surfaces the failure; the trailing catch only prevents an unhandled rejection.
    void saveLayoutMutation
      .mutateAsync(request)
      .finally(() => {
        saveOperationPendingRef.current = false;
      })
      .catch(() => undefined);
  }, [captureLayoutSaveRequest, membershipOperationCountRef, saveLayoutMutation, saveOperationPendingRef]);

  const { requestExitEdit, requestSwitchDashboard, requestSwitchGroup } = useLayoutRequests(
    session,
    layoutOperationPending,
    navigateToDashboard,
    selectedDashboardId,
  );

  const handleDirtyModalResolution = useDirtyModalResolution(session, {
    captureLayoutSaveRequest,
    saveLayoutMutation,
    navigateToDashboard,
  });

  return { layoutOperationPending, handleSave, requestExitEdit, requestSwitchDashboard, requestSwitchGroup, handleDirtyModalResolution };
}
