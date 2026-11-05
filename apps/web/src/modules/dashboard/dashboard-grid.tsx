'use client';

import { useTranslations } from 'next-intl';
import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { DashboardPlacement, GadgetInstance } from './api';
import { GridCell } from './grid-cell';
import {
  DEFAULT_MIN_SIZE, doPlacementsOverlap, GRID_GAP_PX, RENDERER_MIN_SIZES,
  type ActiveGesture,
} from './grid-geometry';
import { GridBackdrop, GridNotices } from './grid-overlays';
import { useGridGestures } from './use-grid-gestures';
import { useGridProjection } from './use-grid-projection';

export {
  doPlacementsOverlap, getGridUnitMetrics, RENDERER_DEFAULT_SIZES, RENDERER_MIN_SIZES, resolveCollisions,
} from './grid-geometry';

/** Props for the DashboardGrid renderer. */
export interface DashboardGridProps {
  /** All dashboard instances, including hidden groups, for complete geometry checks. */
  instances: GadgetInstance[];
  /** Complete draft placement snapshot; hidden-group rectangles remain collision obstacles. */
  placements: DashboardPlacement[];
  /** IDs currently rendered; visibility never changes history or collision membership. */
  visibleInstanceIds: string[];
  /** Shows the width-derived local mobile reading projection while viewing. */
  isMobileView?: boolean;
  /** Shows the read-only two-column tablet flow of the desktop layout while viewing. */
  isTabletView?: boolean;
  /** Reports the current local readable projection for explicit owner adoption. */
  onReadableProjectionChange?: (placements: DashboardPlacement[] | null) => void;
  /** Reports root width-derived unit metrics for new mobile instance default heights. */
  onGridMetricsChange?: (metrics: { cellSize: number; stride: number; frameHeaderHeight: number } | null) => void;
  /** Requested column count, clamped to the supported range of one through 20. */
  columns?: number;
  /** Layout identity used to cancel gestures when the parent switches breakpoints. */
  breakpoint: 'desktop' | 'mobile';
  /** Physical viewport request, including a breakpoint currently awaiting dirty-session choice. */
  requestedBreakpoint?: 'desktop' | 'mobile';
  /** Parent operation state that disables edit commits before the React update is painted. */
  interactionPending?: boolean;
  /** Whether edit mode is currently enabled (shows grid lines, handles, drag). */
  isEditMode: boolean;
  /** Callback fired when user moves, resizes, or nudges gadgets in edit mode. */
  onLayoutChange?: (nextPlacements: DashboardPlacement[]) => void;
  /** Callback fired when removing an instance in edit mode. */
  onRemoveInstance?: (instanceId: string) => void;
  /** Callback fired when opening instance configuration modal. */
  onConfigureInstance?: (instance: GadgetInstance) => void;
  /** Reading updates mapping by instance identifier. */
  readingQueues?: Record<string, number>;
  /** Callback fired when applying queued reading updates for an instance. */
  onApplyReadingQueue?: (instanceId: string) => void;
}

/** Tracks the width of the grid container with a ResizeObserver; 0 until a positive width is reported. */
function useGridWidth(gridRef: React.RefObject<HTMLDivElement | null>): number {
  // A hidden grid remains unmeasured until ResizeObserver reports a positive width.
  const [gridWidth, setGridWidth] = useState(0);
  /** Tracks the visible container so unit tracks follow its actual width. */
  useEffect(() => {
    const node = gridRef.current;
    if (!node) return;

    const observer = new ResizeObserver(([entry]) => {
      if (!entry) return;
      setGridWidth((current) => current === entry.contentRect.width ? current : entry.contentRect.width);
    });
    observer.observe(node);
    return () => observer.disconnect();
  }, [gridRef]);
  return gridWidth;
}

/** Live-region text and gesture status for the running gesture (or the last keyboard announcement). */
function describeGesture(
  t: ReturnType<typeof useTranslations>,
  gesture: ActiveGesture | null,
  placements: DashboardPlacement[],
  announcement: string,
) {
  const overlapCount = gesture
    ? placements.filter((p) => p.instance_id !== gesture.instanceId && doPlacementsOverlap(gesture.currentPlacement, p)).length
    : 0;
  const gestureNotice = gesture ? (overlapCount > 0 ? t('overlapNotice', { count: overlapCount }) : t('dropFits')) : '';
  const liveText = gesture
    ? `${t('positionLabel', { x: gesture.currentPlacement.x + 1, y: gesture.currentPlacement.y + 1, w: gesture.currentPlacement.w, h: gesture.currentPlacement.h })}. ${gestureNotice}`
    : announcement;
  return { overlapCount, gestureNotice, liveText };
}

/** Highest row to render adequate grid background in edit mode and view projection (at least 12, plus 4 spare rows when editing). */
function computeMaxRow(displayPlacements: DashboardPlacement[], isEditMode: boolean): number {
  let max = 12;
  for (const p of displayPlacements) {
    if (p.y + p.h > max) max = p.y + p.h;
  }
  return max + (isEditMode ? 4 : 0);
}

/**
 * Grid renderer for Umwelt-OS dashboards.
 * Operates on square integer units with at most 20 columns without moving during streaming. All
 * draft rectangles remain collision participants while visible IDs filter rendered cards only.
 * Mobile reading geometry is local; edit commits return complete validated layouts.
 *
 * @param props Grid instances, integer placements, edit mode flag, and interaction handlers.
 * @returns Responsive accessible grid with full-layout gesture resolution and local reading projection.
 */
export function DashboardGrid(props: DashboardGridProps) {
  const {
    instances, placements, visibleInstanceIds, isMobileView = false, isTabletView = false, columns = 20, breakpoint,
    requestedBreakpoint = breakpoint, interactionPending = false, isEditMode, readingQueues = {},
  } = props;
  const t = useTranslations('dashboard');
  const gridRef = useRef<HTMLDivElement>(null);
  const gridWidth = useGridWidth(gridRef);
  const columnCount = Math.max(1, Math.min(20, Math.floor(columns)));
  const visibleGroupKey = [...visibleInstanceIds].sort().join('\u0000');
  const [selectedInstanceId, setSelectedInstanceId] = useState<string | null>(null);
  const [interactionNotice, setInteractionNotice] = useState<string | null>(null);
  const [frameHeaderHeight, setFrameHeaderHeight] = useState(0);

  // Map instances by id for quick lookup
  const instancesById = useMemo(() => new Map(instances.map((inst) => [inst.id, inst])), [instances]);
  const visibleIds = useMemo(() => new Set(visibleInstanceIds), [visibleInstanceIds]);
  const hasCompleteCoverage = useMemo(() => {
    const placementIds = new Set(placements.map((placement) => placement.instance_id));
    return placementIds.size === instances.length && placements.length === instances.length
      && instances.every((instance) => placementIds.has(instance.id));
  }, [instances, placements]);

  /** Resolves renderer-specific integer width and height bounds. */
  const getMinSize = useCallback((instanceId: string) => {
    const renderer = instancesById.get(instanceId)?.definition?.renderer ?? '';
    return RENDERER_MIN_SIZES[renderer] ?? DEFAULT_MIN_SIZE;
  }, [instancesById]);
  /** Keeps one measured fixed frame-header height for the current fixed two-row chrome. */
  const recordHeaderHeight = useCallback((height: number) => {
    if (Number.isFinite(height) && height > 0) {
      setFrameHeaderHeight((current) => current === height ? current : height);
    }
  }, []);

  const { gesture, announcement, handleKeyDown, handleDragStart, handleResizeStart } = useGridGestures({
    gridRef, isEditMode, interactionPending, breakpoint, requestedBreakpoint, columnCount, visibleGroupKey, placements,
    getMinSize, hasCompleteCoverage, onLayoutChange: props.onLayoutChange, setInteractionNotice, setSelectedInstanceId,
  });
  const projection = useGridProjection({
    gridWidth, columnCount, isMobileView, isTabletView, isEditMode, breakpoint, placements, instances, frameHeaderHeight,
    gesture, onReadableProjectionChange: props.onReadableProjectionChange, onGridMetricsChange: props.onGridMetricsChange,
  });
  const { renderColumns, renderCell, displayPlacements, projectionPending, projectionUnavailable, cellSize } = projection;
  const maxRow = useMemo(() => computeMaxRow(displayPlacements, isEditMode), [displayPlacements, isEditMode]);
  const { overlapCount, gestureNotice, liveText } = describeGesture(t, gesture, placements, announcement);
  const gridMinHeight = maxRow * renderCell + Math.max(0, maxRow - 1) * GRID_GAP_PX;

  return (
    <div
      ref={gridRef}
      className={`relative w-full ${isEditMode ? 'select-none' : ''}`}
      style={{
        display: 'grid',
        gridTemplateColumns: `repeat(${renderColumns}, ${renderCell}px)`,
        gridAutoRows: `${renderCell}px`,
        gap: `${GRID_GAP_PX}px`,
        minHeight: `${gridMinHeight}px`,
      }}
    >
      <GridNotices
        isEditMode={isEditMode} liveText={liveText} interactionNotice={interactionNotice}
        projectionPending={projectionPending} projectionUnavailable={projectionUnavailable}
      />
      {isEditMode && <GridBackdrop columnCount={columnCount} cellSize={cellSize} maxRow={maxRow} />}

      {/* Render each gadget placement */}
      {displayPlacements.map((placement) => {
        const instance = instancesById.get(placement.instance_id);
        if (!visibleIds.has(placement.instance_id) || !instance) return null;
        const id = placement.instance_id;
        return (
          <GridCell
            key={id}
            placement={placement}
            instance={instance}
            isEditMode={isEditMode}
            isSelected={selectedInstanceId === id}
            gesture={gesture?.instanceId === id ? gesture : null}
            overlapCount={overlapCount}
            gestureNotice={gestureNotice}
            projectionPending={projectionPending}
            readingQueueCount={readingQueues[id] ?? 0}
            onKeyDown={(e) => handleKeyDown(e, id)}
            onSelect={() => setSelectedInstanceId(id)}
            onDragStart={(e) => handleDragStart(id, e)}
            onResizeStart={(direction, e) => handleResizeStart(id, direction, e)}
            onHeaderMeasured={recordHeaderHeight}
            onConfigure={() => props.onConfigureInstance?.(instance)}
            onRemove={() => props.onRemoveInstance?.(id)}
            onApplyReadingQueue={() => props.onApplyReadingQueue?.(id)}
          />
        );
      })}
    </div>
  );
}
