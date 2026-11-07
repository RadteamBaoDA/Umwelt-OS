'use client';

import { useEffect, useMemo } from 'react';
import type { DashboardPlacement, GadgetInstance } from './api';
import { getGridUnitMetrics, type ActiveGesture } from './grid-geometry';
import { projectReadableMobilePlacements, projectTabletPlacements, TABLET_GRID_COLUMNS } from './widget-registry';

/** Full-width reading flow for a panel too narrow to hold two readable gadget columns. */
function projectSingleColumnPlacements(placements: DashboardPlacement[], instances: GadgetInstance[],
  desktopCellSize: number, singleCellSize: number, frameHeaderHeight: number): DashboardPlacement[] | null {
  if (!(desktopCellSize > 0) || !(singleCellSize > 0)) return null;
  let y = 0;
  const ordered = [...placements].sort((a, b) => a.y - b.y || a.x - b.x || a.instance_id.localeCompare(b.instance_id));
  const flowed = ordered.map((source) => {
    const height = Math.max(2, Math.ceil((source.h * (desktopCellSize + 12)) / (singleCellSize + 12)));
    const next = { ...source, x: 0, y, w: TABLET_GRID_COLUMNS, h: height };
    y += height;
    return next;
  });
  const readable = projectReadableMobilePlacements(flowed, instances, TABLET_GRID_COLUMNS, singleCellSize, frameHeaderHeight);
  const byId = new Map(readable?.map((placement) => [placement.instance_id, placement]));
  return readable ? placements.map((placement) => byId.get(placement.instance_id)!) : null;
}

/** Inputs of {@link useGridProjection}. */
export interface GridProjectionInputs {
  gridWidth: number;
  columnCount: number;
  isMobileView: boolean;
  isTabletView: boolean;
  isEditMode: boolean;
  breakpoint: 'desktop' | 'mobile';
  placements: DashboardPlacement[];
  instances: GadgetInstance[];
  /** Measured frame header height in px (0 until measured). */
  frameHeaderHeight: number;
  gesture: ActiveGesture | null;
  /** Reports the current local readable projection for explicit owner adoption. */
  onReadableProjectionChange?: (placements: DashboardPlacement[] | null) => void;
  /** Reports root width-derived unit metrics for new mobile instance default heights. */
  onGridMetricsChange?: (metrics: { cellSize: number; stride: number; frameHeaderHeight: number } | null) => void;
}

/** Tablet reading flow: only a view-only desktop layout, never while editing. */
function useTabletProjection(inputs: GridProjectionInputs, gridMetrics: ReturnType<typeof getGridUnitMetrics>) {
  const { gridWidth, isTabletView, isEditMode, isMobileView, breakpoint, placements, instances, frameHeaderHeight } = inputs;
  // Tablet flow is view-only on the desktop layout; editing keeps the 20-column geometry.
  const tabletActive = isTabletView && !isEditMode && !isMobileView && breakpoint === 'desktop';
  const tabletMetrics = useMemo(
    () => (tabletActive ? getGridUnitMetrics(gridWidth, TABLET_GRID_COLUMNS) : null),
    [gridWidth, tabletActive],
  );
  const tabletProjection = useMemo(
    () => (tabletActive && tabletMetrics && gridMetrics && frameHeaderHeight > 0
      ? gridWidth >= 680
        ? projectTabletPlacements(placements, instances, gridMetrics.cellSize, tabletMetrics.cellSize, frameHeaderHeight)
        : projectSingleColumnPlacements(placements, instances, gridMetrics.cellSize, tabletMetrics.cellSize, frameHeaderHeight)
      : null),
    [frameHeaderHeight, gridMetrics, gridWidth, instances, placements, tabletActive, tabletMetrics],
  );
  const rendering = tabletActive && tabletProjection !== null && tabletMetrics !== null;
  return { rendering, tabletProjection, tabletMetrics };
}

/**
 * Derives what the grid renders: unit metrics, the mobile reading projection, the tablet flow and the final
 * display placements; publishes the readable projection and the width-derived metrics to the parent.
 *
 * Side effects: two effects call `onReadableProjectionChange` (null outside mobile view) and `onGridMetricsChange`
 * (null until both width and header height are measured).
 */
export function useGridProjection(inputs: GridProjectionInputs) {
  const { gridWidth, columnCount, isMobileView, isEditMode, breakpoint, placements, instances, frameHeaderHeight, gesture } = inputs;
  const { onReadableProjectionChange, onGridMetricsChange } = inputs;
  // Merge placements with active gesture if any
  const effectivePlacements = useMemo(() => {
    if (!gesture || gesture.columnCount !== columnCount || gesture.breakpoint !== breakpoint) return placements;
    return placements.map((p) => (p.instance_id === gesture.instanceId ? gesture.currentPlacement : p));
  }, [breakpoint, columnCount, gesture, placements]);
  // Matching column width and row height makes every persisted unit square; gaps define the shared stride.
  const gridMetrics = useMemo(() => getGridUnitMetrics(gridWidth, columnCount), [columnCount, gridWidth]);
  const cellSize = gridMetrics?.cellSize ?? 0;
  const readableProjection = useMemo(() => isMobileView
    ? projectReadableMobilePlacements(placements, instances, columnCount, cellSize, frameHeaderHeight)
    : null,
  [cellSize, columnCount, frameHeaderHeight, instances, isMobileView, placements]);
  const projectionMeasurementReady = gridMetrics !== null && frameHeaderHeight > 0;
  const tablet = useTabletProjection(inputs, gridMetrics);
  const displayPlacements = tablet.rendering
    ? tablet.tabletProjection!
    : isMobileView && !isEditMode
      ? readableProjection ?? effectivePlacements
      : effectivePlacements;

  /** Publishes only the measured local view projection; null means width or coverage is unavailable. */
  useEffect(() => {
    if (isMobileView) onReadableProjectionChange?.(readableProjection);
    else onReadableProjectionChange?.(null);
  }, [isMobileView, onReadableProjectionChange, readableProjection]);

  /** Shares the same root-width measurement used by square tracks with instance creation. */
  useEffect(() => {
    onGridMetricsChange?.(gridMetrics && frameHeaderHeight > 0
      ? { ...gridMetrics, frameHeaderHeight } : null);
  }, [frameHeaderHeight, gridMetrics, onGridMetricsChange]);

  return {
    cellSize,
    renderColumns: tablet.rendering ? TABLET_GRID_COLUMNS : columnCount,
    renderCell: tablet.rendering ? tablet.tabletMetrics!.cellSize : cellSize,
    displayPlacements,
    projectionPending: isMobileView && !isEditMode && !projectionMeasurementReady,
    projectionUnavailable: isMobileView && !isEditMode && projectionMeasurementReady && !readableProjection,
  };
}
