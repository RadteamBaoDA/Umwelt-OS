'use client';

import { useTranslations } from 'next-intl';
import React, { useCallback, useEffect, useLayoutEffect, useRef, useState, type RefObject } from 'react';
import type { DashboardPlacement } from './api';
import {
  getGridUnitMetrics, isGestureStale, nudgePlacement, placementForPointer, resolveCollisions,
  type ActiveGesture, type GridLayoutContext,
} from './grid-geometry';

/** Inputs shared by the gesture hooks. */
export interface GridGestureInputs {
  gridRef: RefObject<HTMLDivElement | null>;
  isEditMode: boolean;
  interactionPending: boolean;
  breakpoint: 'desktop' | 'mobile';
  requestedBreakpoint: 'desktop' | 'mobile';
  columnCount: number;
  visibleGroupKey: string;
  placements: DashboardPlacement[];
  /** Renderer-specific integer width and height lower bounds. */
  getMinSize: (instanceId: string) => { minW: number; minH: number };
  /** True when placements cover every instance; incomplete coverage blocks all edits. */
  hasCompleteCoverage: boolean;
  onLayoutChange?: (nextPlacements: DashboardPlacement[]) => void;
  /** Shows a transient interaction notice. */
  setInteractionNotice: (notice: string | null) => void;
  /** Marks a gadget as selected (pointer-down and Escape). */
  setSelectedInstanceId: (id: string | null) => void;
}

/**
 * Keeps the latest layout facts in a ref so window-level pointer handlers (registered once per gesture) judge
 * staleness against the current render, not the render that started the gesture.
 */
function useGridLayoutContext(inputs: Pick<GridGestureInputs, 'breakpoint' | 'requestedBreakpoint' | 'columnCount' | 'visibleGroupKey' | 'isEditMode' | 'interactionPending'>) {
  const { breakpoint, requestedBreakpoint, columnCount, visibleGroupKey, isEditMode, interactionPending } = inputs;
  const ref = useRef<GridLayoutContext>({ breakpoint, requestedBreakpoint, columnCount, visibleGroupKey, interactionEnabled: isEditMode && !interactionPending });
  useLayoutEffect(() => {
    ref.current = { breakpoint, requestedBreakpoint, columnCount, visibleGroupKey, interactionEnabled: isEditMode && !interactionPending };
  });
  return ref;
}

/**
 * Owns the active gesture state and cancels it when its owner changes (editing disabled, pending operation, pending
 * viewport choice, other breakpoint / column count / visible group).
 */
function useGestureState(inputs: Pick<GridGestureInputs, 'breakpoint' | 'requestedBreakpoint' | 'columnCount' | 'visibleGroupKey' | 'isEditMode' | 'interactionPending'>) {
  const { breakpoint, requestedBreakpoint, columnCount, visibleGroupKey, isEditMode, interactionPending } = inputs;
  const [gesture, setGesture] = useState<ActiveGesture | null>(null);
  /** Cancels transient gestures on owner changes, pending viewport choice, or disabled editing. */
  /* eslint-disable react-hooks/set-state-in-effect -- gesture is transient pointer state that must reset when its owner changes */
  useEffect(() => {
    if (!isEditMode || interactionPending || requestedBreakpoint !== breakpoint) {
      setGesture(null);
      return;
    }
    setGesture((current) => current?.columnCount === columnCount
      && current.breakpoint === breakpoint
      && current.visibleGroupKey === visibleGroupKey ? current : null);
  }, [breakpoint, columnCount, interactionPending, isEditMode, requestedBreakpoint, visibleGroupKey]);
  /* eslint-enable react-hooks/set-state-in-effect */
  return [gesture, setGesture] as const;
}

/**
 * Pointer-down starters for drag-to-move and the resize handles. Both validate edit mode, primary button, complete
 * coverage and measurable grid width, then freeze the stride and a backup of the placements in the gesture.
 */
function useGestureStarters(inputs: GridGestureInputs, setGesture: (gesture: ActiveGesture) => void) {
  const t = useTranslations('dashboard');
  const { gridRef, isEditMode, breakpoint, columnCount, visibleGroupKey, placements, getMinSize, hasCompleteCoverage, setInteractionNotice, setSelectedInstanceId } = inputs;

  /** Starts a gesture of `type` on pointer down; stops propagation only when a gesture actually began. */
  const begin = useCallback((type: 'move' | 'resize', direction: 'e' | 's' | 'se' | undefined, instanceId: string, event: React.PointerEvent) => {
    if (!isEditMode || event.button !== 0) return;
    if (!hasCompleteCoverage) {
      setInteractionNotice(t('layoutActionUnavailable'));
      return;
    }
    const initial = placements.find((p) => p.instance_id === instanceId);
    if (!initial) return;
    const width = gridRef.current?.getBoundingClientRect().width ?? 0;
    const metrics = getGridUnitMetrics(width, columnCount);
    if (!metrics) return;

    const minSize = getMinSize(instanceId);
    setSelectedInstanceId(instanceId);
    setGesture({
      type,
      ...(direction ? { direction } : {}),
      instanceId,
      breakpoint,
      columnCount,
      visibleGroupKey,
      startX: event.clientX,
      startY: event.clientY,
      cellStride: metrics.stride,
      initialPlacement: { ...initial },
      currentPlacement: { ...initial },
      backupPlacements: placements.map((p) => ({ ...p })),
      minW: minSize.minW,
      minH: minSize.minH,
    });

    event.preventDefault();
    event.stopPropagation();
  }, [breakpoint, columnCount, getMinSize, gridRef, hasCompleteCoverage, isEditMode, placements, setGesture, setInteractionNotice, setSelectedInstanceId, t, visibleGroupKey]);

  /** Initiates drag to move gesture on header pointer down. */
  const handleDragStart = useCallback((instanceId: string, event: React.PointerEvent) => begin('move', undefined, instanceId, event), [begin]);
  /** Initiates resize gesture on handle pointer down. */
  const handleResizeStart = useCallback(
    (instanceId: string, direction: 'e' | 's' | 'se', event: React.PointerEvent) => begin('resize', direction, instanceId, event),
    [begin],
  );
  return { handleDragStart, handleResizeStart };
}

/**
 * Registers window pointermove / pointerup / Escape listeners for the running gesture.
 *
 * Move updates the draft rectangle; up commits through `resolveCollisions` (or shows the unavailable notice) and is
 * dropped without a commit when the gesture became stale; Escape cancels without creating history.
 */
function useGestureListeners(
  inputs: GridGestureInputs,
  gesture: ActiveGesture | null,
  setGesture: React.Dispatch<React.SetStateAction<ActiveGesture | null>>,
  layoutContextRef: RefObject<GridLayoutContext>,
) {
  const t = useTranslations('dashboard');
  const { gridRef, columnCount, placements, getMinSize, onLayoutChange, setInteractionNotice, breakpoint } = inputs;
  useEffect(() => {
    if (!gesture) return;

    /** Window pointer move handler updating gesture draft. */
    const onPointerMove = (e: PointerEvent) => {
      const grid = gridRef.current;
      if (!grid || isGestureStale(gesture, layoutContextRef.current) || gesture.cellStride <= 0) return;
      if (!getGridUnitMetrics(grid.getBoundingClientRect().width, columnCount)) return;
      const currentPlacement = placementForPointer(gesture, e.clientX, e.clientY, columnCount);
      setGesture((prev) => (prev ? { ...prev, currentPlacement: { ...prev.currentPlacement, ...currentPlacement } } : null));
    };

    /** Window pointer up handler committing gesture with collision displacement. */
    const onPointerUp = () => {
      const width = gridRef.current?.getBoundingClientRect().width ?? 0;
      const metrics = getGridUnitMetrics(width, columnCount);
      if (isGestureStale(gesture, layoutContextRef.current) || !metrics) {
        setGesture(null);
        return;
      }
      const updated = placements.map((p) =>
        p.instance_id === gesture.instanceId ? gesture.currentPlacement : p,
      );
      const resolved = resolveCollisions(updated, gesture.instanceId, columnCount, getMinSize);
      setGesture(null);
      if (resolved) onLayoutChange?.(resolved);
      else setInteractionNotice(t('layoutActionUnavailable'));
    };

    /** Per-gesture Escape drops transient pointer geometry without creating a parent history entry. */
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return;
      e.preventDefault();
      // Pointer movement is transient; cancelling adds no history snapshot, stale or not.
      setGesture(null);
    };

    window.addEventListener('pointermove', onPointerMove);
    window.addEventListener('pointerup', onPointerUp);
    window.addEventListener('keydown', onKeyDown);

    return () => {
      window.removeEventListener('pointermove', onPointerMove);
      window.removeEventListener('pointerup', onPointerUp);
      window.removeEventListener('keydown', onKeyDown);
    };
  }, [breakpoint, columnCount, getMinSize, gesture, gridRef, layoutContextRef, onLayoutChange, placements, setGesture, setInteractionNotice, t]);
}

/**
 * Keyboard alternative in edit mode (Arrow keys move, Shift+Arrow resizes, Escape deselects). A successful change
 * commits the collision-resolved layout and announces the new position for assistive technology.
 */
function useKeyboardNudge(inputs: GridGestureInputs, setAnnouncement: (text: string) => void) {
  const t = useTranslations('dashboard');
  const { isEditMode, hasCompleteCoverage, placements, getMinSize, columnCount, onLayoutChange, setInteractionNotice, setSelectedInstanceId } = inputs;
  return useCallback((e: React.KeyboardEvent<HTMLDivElement>, instanceId: string) => {
    if (!isEditMode) return;
    if (!hasCompleteCoverage) {
      setInteractionNotice(t('layoutActionUnavailable'));
      return;
    }
    const current = placements.find((p) => p.instance_id === instanceId);
    if (!current) return;
    const next = nudgePlacement(current, e.key, e.shiftKey, columnCount, getMinSize(instanceId));
    if (next) {
      e.preventDefault();
      const updated = placements.map((p) => (p.instance_id === instanceId ? next : p));
      const resolved = resolveCollisions(updated, instanceId, columnCount, getMinSize);
      if (resolved) {
        onLayoutChange?.(resolved);
        setAnnouncement(t('positionLabel', { x: next.x + 1, y: next.y + 1, w: next.w, h: next.h }));
      } else setInteractionNotice(t('layoutActionUnavailable'));
    } else if (e.key === 'Escape') {
      setSelectedInstanceId(null);
    }
  }, [columnCount, getMinSize, hasCompleteCoverage, isEditMode, onLayoutChange, placements, setAnnouncement, setInteractionNotice, setSelectedInstanceId, t]);
}

/**
 * Pointer and keyboard editing gestures of the dashboard grid.
 *
 * @param inputs Grid facts and callbacks (see {@link GridGestureInputs}).
 * @returns The running gesture, the live-region announcement and the start / keyboard handlers.
 */
export function useGridGestures(inputs: GridGestureInputs) {
  const layoutContextRef = useGridLayoutContext(inputs);
  const [gesture, setGesture] = useGestureState(inputs);
  const [announcement, setAnnouncement] = useState<string>('');
  const starters = useGestureStarters(inputs, setGesture);
  useGestureListeners(inputs, gesture, setGesture, layoutContextRef);
  const handleKeyDown = useKeyboardNudge(inputs, setAnnouncement);
  return { gesture, announcement, handleKeyDown, ...starters };
}
