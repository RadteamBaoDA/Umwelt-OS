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
  const { setSaveError, setIsDirty, setEditOrigin, setHistory, setDraftPlacements, setHistoryIndex, pendingBreakpointRef, dismissedBreakpointRef } = session;
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
 * Resolves the dirty-layout modal (save / discard / stay) and releases the intents it was guarding.
 *
 * @param session The layout session.
 * @param deps Request capture, save mutation and dashboard navigation.
 * @returns Handler for the modal's resolution callback.
 */
function useDirtyModalResolution(session: LayoutSession, deps: DirtyResolutionDeps) {
  const { captureLayoutSaveRequest, saveLayoutMutation, navigateToDashboard } = deps;
  const guardedNavigation = useGuardedNavigation();
  const { savedPlacements, saveOperationPendingRef, membershipOperationCountRef, pendingExitEditRef, pendingDashboardSwitchRef, pendingRouteNavigationRef, pendingBreakpointRef, dismissedBreakpointRef } = session;
  const { setDirtyModalOpen, setIsEditMode, setEditOrigin, setActiveGroupId, setIsDirty, setDraftPlacements, setHistory, setHistoryIndex } = session;

  /**
   * Releases whatever the dirty modal was guarding once the draft was saved or discarded: a pending route
   * navigation wins over a dashboard switch and exit-edit; otherwise switch first, then exit edit.
   */
  const releasePendingIntents = useCallback((routeIntent: GuardedNavigationIntent | null) => {
    if (routeIntent) {
      pendingDashboardSwitchRef.current = null;
      pendingExitEditRef.current = false;
      guardedNavigation.continueNavigation(routeIntent);
      return;
    }
    if (pendingDashboardSwitchRef.current) {
      navigateToDashboard(pendingDashboardSwitchRef.current);
      pendingDashboardSwitchRef.current = null;
      setActiveGroupId('all');
    }
    if (pendingExitEditRef.current) {
      setIsEditMode(false);
      pendingExitEditRef.current = false;
    }
  }, [guardedNavigation, navigateToDashboard, pendingDashboardSwitchRef, pendingExitEditRef, setActiveGroupId, setIsEditMode]);

  /** Drops every pending intent and remembers the breakpoint the owner chose to stay on. */
  const stayOnDraft = useCallback(() => {
    pendingDashboardSwitchRef.current = null;
    pendingExitEditRef.current = false;
    pendingRouteNavigationRef.current = null;
    dismissedBreakpointRef.current = pendingBreakpointRef.current;
    pendingBreakpointRef.current = null;
  }, [dismissedBreakpointRef, pendingBreakpointRef, pendingDashboardSwitchRef, pendingExitEditRef, pendingRouteNavigationRef]);

  /** Discards the draft back to the saved layout, then releases pending intents. */
  const discardDraft = useCallback(() => {
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
  }, [dismissedBreakpointRef, pendingBreakpointRef, pendingRouteNavigationRef, releasePendingIntents, savedPlacements, setDraftPlacements, setEditOrigin, setHistory, setHistoryIndex, setIsDirty]);

  /** Saves the draft; on success releases pending intents, on failure keeps the draft (and the modal for route intents). */
  const saveDraftAndContinue = useCallback(async () => {
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
        pendingExitEditRef.current = false;
      }
    } finally {
      saveOperationPendingRef.current = false;
    }
  }, [captureLayoutSaveRequest, membershipOperationCountRef, pendingDashboardSwitchRef, pendingExitEditRef, pendingRouteNavigationRef, releasePendingIntents, saveLayoutMutation, saveOperationPendingRef, setDirtyModalOpen]);

  /** Resolves the dirty modal selection: save, discard, or stay. */
  return useCallback(async (action: DirtyResolution) => {
    setDirtyModalOpen(false);
    if (action === 'stay') stayOnDraft();
    else if (action === 'discard') discardDraft();
    else await saveDraftAndContinue();
  }, [discardDraft, saveDraftAndContinue, setDirtyModalOpen, stayOnDraft]);
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
  const t = useTranslations('dashboard');
  const saveLayoutMutation = useSaveLayoutMutation(session);
  const { activeDashboard, editOrigin, currentBreakpoint, layoutColumns, savedPlacements, draftPlacements, mobileProjectionUnavailable, isDirty, membershipMutationsPending } = session;
  const { saveOperationPendingRef, membershipOperationCountRef, pendingExitEditRef, pendingDashboardSwitchRef } = session;
  const { setSaveError, setDirtyModalOpen, setIsEditMode, setEditOrigin, setActiveGroupId } = session;
  const layoutOperationPending = saveLayoutMutation.isPending || membershipMutationsPending > 0;

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
  }, [activeDashboard, currentBreakpoint, draftPlacements, editOrigin, layoutColumns, mobileProjectionUnavailable, savedPlacements, setSaveError, t]);

  /** Triggered by Save button; the mutation never rereads viewport-derived identity. */
  const handleSave = useCallback(() => {
    if (saveOperationPendingRef.current || membershipOperationCountRef.current > 0) return;
    const request = captureLayoutSaveRequest();
    if (!request) return;
    saveOperationPendingRef.current = true;
    // The mutation's onError already surfaces the failure; the trailing catch only prevents an unhandled rejection.
    void saveLayoutMutation.mutateAsync(request)
      .finally(() => { saveOperationPendingRef.current = false; })
      .catch(() => undefined);
  }, [captureLayoutSaveRequest, membershipOperationCountRef, saveLayoutMutation, saveOperationPendingRef]);

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
  const requestSwitchDashboard = useCallback((newId: string) => {
    if (layoutOperationPending || membershipOperationCountRef.current > 0) return;
    if (newId === selectedDashboardId) return;
    if (isDirty) {
      pendingDashboardSwitchRef.current = newId;
      setDirtyModalOpen(true);
    } else {
      navigateToDashboard(newId);
      setActiveGroupId('all');
    }
  }, [isDirty, layoutOperationPending, membershipOperationCountRef, navigateToDashboard, pendingDashboardSwitchRef, selectedDashboardId, setActiveGroupId, setDirtyModalOpen]);

  const handleDirtyModalResolution = useDirtyModalResolution(session, {
    captureLayoutSaveRequest, saveLayoutMutation, navigateToDashboard,
  });

  return { layoutOperationPending, handleSave, requestExitEdit, requestSwitchDashboard, handleDirtyModalResolution };
}
