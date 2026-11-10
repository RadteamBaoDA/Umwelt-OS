'use client';

import { useQueryClient } from '@tanstack/react-query';
import { useTranslations } from 'next-intl';
import { useCallback } from 'react';
import { useWorkspaceSession } from '@/core/app-shell/workspace-shell';
import { createGadgetInstance, dashboardKeys, deleteGadgetInstance, type Dashboard, type DashboardPlacement } from './api';
import { RENDERER_DEFAULT_SIZES, RENDERER_MIN_SIZES } from './grid-geometry';
import { getGroupFrame } from './group-frame';
import {
  findFrameCoordinates,
  findFreeCoordinates,
  type LayoutFrame,
  projectMobilePlacements,
  sameLayout,
  type AddReservation,
  type LayoutEditOrigin,
  type MobileProjection,
} from './layout-geometry';
import type { LayoutIdentity, LayoutSession } from './use-layout-session';
import { getGadgetReadingBodyFloor } from './widget-registry';

/** Parameters of a gadget instance creation requested by the layout editor. */
export interface AddGadgetParams {
  definitionId: string;
  groupId?: string;
  title?: string;
  renderer: string;
}

/**
 * True when the live layout identity still matches the identity captured at request time. A response may also
 * be accepted when the identity's base revision equals the server's new revision (the page already refetched).
 */
function identityMatches(
  current: LayoutIdentity,
  expected: Omit<LayoutIdentity, 'dashboardId'> & { dashboardId: string },
  acceptRevision?: number,
): boolean {
  return current.dashboardId === expected.dashboardId
    && current.groupId === expected.groupId
    && current.breakpoint === expected.breakpoint
    && current.columns === expected.columns
    && (current.baseRevision === expected.baseRevision
      || (acceptRevision !== undefined && current.baseRevision === acceptRevision));
}

/** Projects the server layout of the requested breakpoint (mobile geometry may be unavailable). */
function serverSnapshotFor(updated: Dashboard, breakpoint: 'desktop' | 'mobile'): MobileProjection {
  return breakpoint === 'mobile'
    ? projectMobilePlacements(updated.layouts.mobile.items, updated.instances, updated.layouts.mobile.columns)
    : { placements: updated.layouts.desktop.items, unavailable: false };
}

/**
 * Resolves the rectangle a new gadget will occupy: the roomy renderer default when it fits the draft,
 * otherwise the true minimum. Mobile spans all columns with max(computed floor, default) height.
 *
 * @returns The chosen size, or null when not even the minimum fits.
 */
function resolveSizeForRenderer(
  renderer: string,
  ctx: {
    breakpoint: 'desktop' | 'mobile';
    columns: number;
    draft: DashboardPlacement[];
    metrics: { frameHeaderHeight: number; stride: number } | null;
    /** Layout frame of the target page (desktop only). */
    frame?: LayoutFrame;
  },
): { width: number; height: number } | null {
  const { breakpoint, columns, draft, metrics } = ctx;
  const minimum = RENDERER_MIN_SIZES[renderer] ?? { minW: 4, minH: 3 };
  const preferred = RENDERER_DEFAULT_SIZES[renderer] ?? { defaultW: minimum.minW, defaultH: minimum.minH };
  const defaultW = Math.max(preferred.defaultW, minimum.minW);
  const defaultH = Math.max(preferred.defaultH, minimum.minH);
  const floorHeight = metrics
    ? Math.max(minimum.minH, Math.ceil((getGadgetReadingBodyFloor(renderer) + metrics.frameHeaderHeight + 2 + 12) / metrics.stride))
    : minimum.minH;
  // Desktop pages with a layout frame use the zone width/height; anything else keeps the roomy-default/minimum ladder.
  const framed = breakpoint === 'desktop' && ctx.frame && ctx.frame !== 'free'
    ? findFrameCoordinates(draft, ctx.frame, minimum.minW, defaultH, columns) : null;
  if (framed) return { width: framed.w, height: framed.h };
  const candidates = breakpoint === 'mobile'
    ? [{ width: columns, height: Math.max(floorHeight, defaultH) }]
    : [
      { width: Math.min(defaultW, columns), height: defaultH },
      { width: Math.min(minimum.minW, columns), height: minimum.minH },
    ];
  return candidates.find((size) => findFreeCoordinates(draft, size.width, size.height, columns)) ?? null;
}

/**
 * Final coordinates of a reserved add. Framed desktop pages use the zone (the reserved width already is that
 * zone's width, or the free fallback); everything else takes the first free slot.
 */
function placeReserved(occupied: DashboardPlacement[], r: AddReservation): { x: number; y: number } | null {
  const frame = r.breakpoint === 'desktop' ? getGroupFrame(r.groupId) : 'free';
  const framed = frame === 'free' ? null : findFrameCoordinates(occupied, frame, r.width, r.height, r.columns);
  return framed && framed.w >= r.width ? { x: framed.x, y: framed.y } : findFreeCoordinates(occupied, r.width, r.height, r.columns);
}

/**
 * Add-reservation bookkeeping: size resolution, reservation begin/release and the free-space precheck.
 *
 * A reservation counts as a membership operation (blocking save/undo/other membership changes) from `begin`
 * until `release`, which is idempotent.
 */
function useAddReservations(session: LayoutSession, layoutOperationPending: boolean) {
  const t = useTranslations('dashboard');
  const { activeDashboard, activeGroupId, currentBreakpoint, layoutColumns, draftPlacements, gridMetrics } = session;
  const { addReservationsRef, addReservationSequenceRef, membershipOperationCountRef, setMembershipMutationsPending, setSaveError } = session;

  const resolveAddSize = useCallback((renderer: string) => resolveSizeForRenderer(renderer, {
    breakpoint: currentBreakpoint, columns: layoutColumns,
    draft: draftPlacements.filter((placement) => activeDashboard?.instances.some((instance) =>
      instance.id === placement.instance_id && instance.group_id === activeGroupId)), metrics: gridMetrics,
    frame: getGroupFrame(activeGroupId),
  }), [activeDashboard, activeGroupId, currentBreakpoint, draftPlacements, gridMetrics, layoutColumns]);

  /** Checks bounded space before a quick definition or instance can be created. */
  const canAddGadgetInstance = useCallback((renderer: string): boolean => {
    if (!activeDashboard || !activeGroupId || layoutOperationPending || membershipOperationCountRef.current > 0) return false;
    if (resolveAddSize(renderer)) return true;
    setSaveError(t('noFreeDashboardSpace'));
    return false;
  }, [activeDashboard, activeGroupId, layoutOperationPending, membershipOperationCountRef, resolveAddSize, setSaveError, t]);

  /** Reserves owner identity and a unique operation before quick-definition network work begins. */
  const beginAddGadgetInstance = useCallback((renderer: string): string | null => {
    if (!activeDashboard || !canAddGadgetInstance(renderer) || membershipOperationCountRef.current > 0) return null;
    const size = resolveAddSize(renderer);
    if (!size) return null;
    const { width, height } = size;
    const reservationId = `dashboard-add-${++addReservationSequenceRef.current}`;
    addReservationsRef.current.set(reservationId, {
      dashboardId: activeDashboard.id,
      groupId: activeGroupId,
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
  }, [activeDashboard, activeGroupId, addReservationSequenceRef, addReservationsRef, canAddGadgetInstance, currentBreakpoint, layoutColumns, membershipOperationCountRef, resolveAddSize, setMembershipMutationsPending]);

  /** Releases one Add reservation idempotently after either stage of creation finishes. */
  const releaseAddReservation = useCallback((reservationId: string) => {
    if (!addReservationsRef.current.delete(reservationId)) return;
    membershipOperationCountRef.current = Math.max(0, membershipOperationCountRef.current - 1);
    setMembershipMutationsPending((pending) => Math.max(0, pending - 1));
  }, [addReservationsRef, membershipOperationCountRef, setMembershipMutationsPending]);

  return { beginAddGadgetInstance, canAddGadgetInstance, releaseAddReservation };
}

/**
 * Shared membership plumbing: reconciles a committed server response into the query cache and rebaselines the
 * draft after an accepted add/remove.
 */
function useMembershipPlumbing(session: LayoutSession) {
  const queryClient = useQueryClient();
  const { setDraftPlacements, setHistory, setHistoryIndex, setIsDirty, setEditOrigin } = session;

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

  /** Replaces the draft, history and dirty state after the server accepted a membership change. */
  const rebaseDraft = useCallback((next: DashboardPlacement[], origin: LayoutEditOrigin) => {
    const dirty = !sameLayout(next, origin.savedSnapshot);
    setDraftPlacements(next);
    setHistory([next]);
    setHistoryIndex(0);
    setIsDirty(dirty);
    setEditOrigin(dirty ? origin : null);
  }, [setDraftPlacements, setEditOrigin, setHistory, setHistoryIndex, setIsDirty]);

  return { reconcileMembershipDashboard, rebaseDraft };
}

type Plumbing = ReturnType<typeof useMembershipPlumbing>;

/**
 * Add-gadget flow: re-validates the reservation and layout identity, POSTs the instance, then rebaselines the draft.
 *
 * @param session The layout session.
 * @param releaseAddReservation Idempotent reservation release, always called when the flow ends.
 * @param plumbing Cache reconciliation and draft rebasing helpers.
 * @returns Handler resolving true only when the instance was created and applied.
 */
function useAddGadget(session: LayoutSession, releaseAddReservation: (id: string) => void, plumbing: Plumbing) {
  const t = useTranslations('dashboard');
  const workspace = useWorkspaceSession();
  const { reconcileMembershipDashboard, rebaseDraft } = plumbing;
  const { layoutIdentityRef, liveLayoutRef, addReservationsRef, setSaveError } = session;

  /** Pre-flight + POST + draft application for one reserved add; throws on network failure. */
  const createAndApply = useCallback(async (reservation: AddReservation, params: AddGadgetParams): Promise<boolean> => {
    const live = liveLayoutRef.current;
    if (!live.activeDashboard || live.activeDashboard.id !== reservation.dashboardId
      || params.groupId !== reservation.groupId
      || !identityMatches(layoutIdentityRef.current, reservation)
      || live.activeDashboard.revision !== reservation.baseRevision) return false;
    // Recheck live occupancy after asynchronous definition creation, immediately before POST.
    const occupied = live.draftPlacements.filter((placement) => live.activeDashboard!.instances.some((instance) =>
      instance.id === placement.instance_id && instance.group_id === reservation.groupId));
    const coords = placeReserved(occupied, reservation);
    if (!coords) {
      setSaveError(t('noFreeDashboardSpace'));
      return false;
    }
    const updatedDashboard = await createGadgetInstance(
      reservation.dashboardId,
      {
        expected_revision: reservation.baseRevision,
        group_id: reservation.groupId,
        definition_id: params.definitionId,
        title: params.title,
      },
      workspace.csrfToken,
    );
    reconcileMembershipDashboard(reservation.dashboardId, updatedDashboard);
    if (updatedDashboard.id !== reservation.dashboardId
      || !identityMatches(layoutIdentityRef.current, reservation, updatedDashboard.revision)) return false;
    // Find newly added instance
    const newInstance = updatedDashboard.instances.find(
      (inst) => !live.activeDashboard!.instances.some((old) => old.id === inst.id),
    );
    if (!newInstance || newInstance.group_id !== reservation.groupId) return false;
    const nextPlacements = [...live.draftPlacements, {
      instance_id: newInstance.id, x: coords.x, y: coords.y, w: reservation.width, h: reservation.height,
    }];
    const serverSnapshot = serverSnapshotFor(updatedDashboard, reservation.breakpoint);
    if (serverSnapshot.unavailable) {
      setSaveError(t('mobileLayoutUnavailable'));
      return false;
    }
    rebaseDraft(nextPlacements, {
      dashboardId: updatedDashboard.id,
      groupId: reservation.groupId,
      breakpoint: reservation.breakpoint,
      columns: reservation.columns,
      baseRevision: updatedDashboard.revision,
      savedSnapshot: serverSnapshot.placements.map((placement) => ({ ...placement })),
    });
    return true;
  }, [layoutIdentityRef, liveLayoutRef, rebaseDraft, reconcileMembershipDashboard, setSaveError, t, workspace.csrfToken]);

  /**
   * Captures owner identity before membership creation; late results cannot rebind another layout.
   * Resolves true only when the instance was created and applied to the draft; false on every stale,
   * no-space or failed path (failures also set the page banner) so callers can compensate.
   */
  const handleAddGadgetInstance = useCallback(
    async (params: AddGadgetParams, reservationId: string): Promise<boolean> => {
      const reservation = addReservationsRef.current.get(reservationId);
      if (!reservation || reservation.renderer !== params.renderer) {
        releaseAddReservation(reservationId);
        return false;
      }
      try {
        return await createAndApply(reservation, params);
      } catch (err) {
        setSaveError(err instanceof Error ? err.message : t('saveFailed'));
        return false;
      } finally {
        releaseAddReservation(reservationId);
      }
    },
    [addReservationsRef, createAndApply, releaseAddReservation, setSaveError, t],
  );

  return handleAddGadgetInstance;
}

/**
 * Remove-gadget flow: DELETEs the instance under the captured identity and rebaselines the draft when accepted.
 *
 * @param session The layout session.
 * @param layoutOperationPending True while a save or membership mutation is pending.
 * @param plumbing Cache reconciliation and draft rebasing helpers.
 * @returns Handler removing one instance by id.
 */
function useRemoveGadget(session: LayoutSession, layoutOperationPending: boolean, plumbing: Plumbing) {
  const t = useTranslations('dashboard');
  const workspace = useWorkspaceSession();
  const { reconcileMembershipDashboard, rebaseDraft } = plumbing;
  const { activeDashboard, activeGroupId, currentBreakpoint, layoutColumns, draftPlacements, layoutIdentityRef } = session;
  const { membershipOperationCountRef, setMembershipMutationsPending, setSaveError } = session;

  /** Captures owner identity before removal; accepted server membership resets layout history. */
  const handleRemoveInstance = useCallback(
    async (instanceId: string) => {
      if (!activeDashboard) return;
      if (!activeDashboard.instances.some((instance) => instance.id === instanceId && instance.group_id === activeGroupId)) return;
      if (layoutOperationPending || membershipOperationCountRef.current > 0) return;
      const requested = {
        dashboardId: activeDashboard.id,
        groupId: activeGroupId,
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
          workspace.csrfToken,
        );
        reconcileMembershipDashboard(requested.dashboardId, updatedDashboard);
        if (updatedDashboard.id !== requested.dashboardId
          || !identityMatches(layoutIdentityRef.current, requested, updatedDashboard.revision)) return;
        const filtered = draftPlacements.filter((p) => p.instance_id !== instanceId);
        const serverSnapshot = serverSnapshotFor(updatedDashboard, requested.breakpoint);
        if (serverSnapshot.unavailable) {
          setSaveError(t('mobileLayoutUnavailable'));
          return;
        }
        rebaseDraft(filtered, {
          dashboardId: updatedDashboard.id,
          groupId: requested.groupId,
          breakpoint: requested.breakpoint,
          columns: requested.columns,
          baseRevision: updatedDashboard.revision,
          savedSnapshot: serverSnapshot.placements.filter((placement) => placement.instance_id !== instanceId).map((placement) => ({ ...placement })),
        });
      } catch (err) {
        setSaveError(err instanceof Error ? err.message : t('saveFailed'));
      } finally {
        membershipOperationCountRef.current = Math.max(0, membershipOperationCountRef.current - 1);
        setMembershipMutationsPending((pending) => Math.max(0, pending - 1));
      }
    },
    [activeDashboard, activeGroupId, currentBreakpoint, draftPlacements, layoutColumns, layoutIdentityRef, layoutOperationPending, membershipOperationCountRef, reconcileMembershipDashboard, rebaseDraft, setMembershipMutationsPending, setSaveError, t, workspace.csrfToken],
  );

  return handleRemoveInstance;
}

/**
 * Gadget membership operations (add / remove) of the dashboard page.
 *
 * Each operation captures the owner's layout identity up front; late results cannot rebind another layout, and
 * accepted responses reset layout history to the server membership. Operations are mutually exclusive through
 * `membershipOperationCountRef` and are blocked while a save is pending.
 *
 * @param session The layout session.
 * @param layoutOperationPending True while a save or membership mutation is pending.
 * @returns Add/remove handlers plus the reservation helpers used by the layout editor.
 */
export function useGadgetMembership(session: LayoutSession, layoutOperationPending: boolean) {
  const reservations = useAddReservations(session, layoutOperationPending);
  const plumbing = useMembershipPlumbing(session);
  const handleAddGadgetInstance = useAddGadget(session, reservations.releaseAddReservation, plumbing);
  const handleRemoveInstance = useRemoveGadget(session, layoutOperationPending, plumbing);
  return { ...reservations, handleAddGadgetInstance, handleRemoveInstance };
}
