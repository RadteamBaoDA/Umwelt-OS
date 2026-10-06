'use client';

import { useTranslations } from 'next-intl';
import React, { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import type { DashboardPlacement, GadgetInstance } from './api';
import { GadgetFrame } from './gadget-frame';
import { projectReadableMobilePlacements, resolveGadgetRenderer } from './widget-registry';

/** Known minimum integer dimensions for standard gadget renderers. */
export const RENDERER_MIN_SIZES: Record<string, { minW: number; minH: number }> = {
  news_feed: { minW: 4, minH: 4 },
  telegram_feed: { minW: 4, minH: 4 },
  text_panel: { minW: 4, minH: 3 },
  table_panel: { minW: 6, minH: 4 },
  video_panel: { minW: 6, minH: 4 },
  finance_chart: { minW: 6, minH: 4 },
  personal_context: { minW: 4, minH: 4 },
  map: { minW: 8, minH: 6 },
  highlights: { minW: 4, minH: 3 },
  watch_rules: { minW: 4, minH: 3 },
  tasks: { minW: 4, minH: 4 },
  goals: { minW: 4, minH: 4 },
  daily_brief: { minW: 6, minH: 4 },
  weather: { minW: 4, minH: 3 },
  research: { minW: 4, minH: 4 },
  github_project: { minW: 6, minH: 4 },
};

/** Default fallback minimum size if renderer is unlisted. */
const DEFAULT_MIN_SIZE = { minW: 4, minH: 3 };

/** Shared spacing between square dashboard units in the grid and its guides. */
const GRID_GAP_PX = 12;
const MAX_LAYOUT_ROWS = 100_000;

/** Width measurements for the shared square-cell geometry used by rendering, projection and Add. */
export function getGridUnitMetrics(width: number, columns: number): { cellSize: number; stride: number } | null {
  if (!Number.isFinite(width) || !Number.isInteger(columns) || columns < 1 || columns > 20) return null;
  const cellSize = (width - GRID_GAP_PX * (columns - 1)) / columns;
  const stride = cellSize + GRID_GAP_PX;
  return Number.isFinite(cellSize) && cellSize > 0 && Number.isFinite(stride) && stride > 0
    ? { cellSize, stride } : null;
}

/** Tests whether two integer placements overlap on a half-open rectangle basis. */
export function doPlacementsOverlap(a: DashboardPlacement, b: DashboardPlacement): boolean {
  return a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h;
}

/**
 * Resolves rectangle collisions by predictably pushing down overlapping items.
 *
 * @param placements Placements to check and resolve.
 * @param activeId Identifier of the actively positioned item that has right-of-way.
 * @param columns Stored column count used to reject impossible horizontal geometry.
 * @param getMinimumSize Renderer-owned integer lower bounds for each instance.
 * @returns A complete non-overlapping snapshot, or null when it cannot fit the row bound.
 */
export function resolveCollisions(
  placements: DashboardPlacement[],
  activeId: string,
  columns = 20,
  getMinimumSize: (instanceId: string) => { minW: number; minH: number } = () => DEFAULT_MIN_SIZE,
): DashboardPlacement[] | null {
  const active = placements.find((placement) => placement.instance_id === activeId);
  if (!active || new Set(placements.map((placement) => placement.instance_id)).size !== placements.length
    || !Number.isInteger(columns) || columns < 1 || columns > 20
    || placements.some((placement) => {
      const minimum = getMinimumSize(placement.instance_id);
      return !Number.isInteger(placement.x) || !Number.isInteger(placement.y)
        || !Number.isInteger(placement.w) || !Number.isInteger(placement.h)
        || placement.x < 0 || placement.y < 0 || placement.w < minimum.minW || placement.h < minimum.minH
        || placement.x + placement.w > columns || placement.y + placement.h > MAX_LAYOUT_ROWS;
    })) return null;

  const placed: DashboardPlacement[] = [{ ...active }];
  const remaining = placements.filter((placement) => placement.instance_id !== activeId).sort(
    (a, b) => a.y - b.y || a.x - b.x || a.instance_id.localeCompare(b.instance_id),
  );
  for (const source of remaining) {
    const candidate = { ...source };
    while (true) {
      const collisions = placed.filter((other) => doPlacementsOverlap(candidate, other));
      if (collisions.length === 0) break;
      const nextY = Math.max(...collisions.map((other) => other.y + other.h));
      if (nextY <= candidate.y || nextY + candidate.h > MAX_LAYOUT_ROWS) return null;
      candidate.y = nextY;
    }
    placed.push(candidate);
  }
  const resolved = new Map(placed.map((placement) => [placement.instance_id, placement]));
  return placements.map((placement) => resolved.get(placement.instance_id)!);
}

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

/** Active pointer gesture state for drag to move or corner resize. */
interface ActiveGesture {
  type: 'move' | 'resize';
  direction?: 'e' | 's' | 'se';
  instanceId: string;
  /** Originating layout prevents a gesture draft from crossing desktop/mobile state. */
  breakpoint: 'desktop' | 'mobile';
  /** Column count at pointer-down; a breakpoint-width change cancels the gesture. */
  columnCount: number;
  /** Visible group identity at pointer-down; switching tabs cancels the gesture. */
  visibleGroupKey: string;
  startX: number;
  startY: number;
  /** Frozen grid stride keeps a pointer gesture stable across viewport resizes. */
  cellStride: number;
  initialPlacement: DashboardPlacement;
  currentPlacement: DashboardPlacement;
  backupPlacements: DashboardPlacement[];
  minW: number;
  minH: number;
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
export function DashboardGrid({
  instances,
  placements,
  visibleInstanceIds,
  isMobileView = false,
  onReadableProjectionChange,
  onGridMetricsChange,
  columns = 20,
  breakpoint,
  requestedBreakpoint = breakpoint,
  interactionPending = false,
  isEditMode,
  onLayoutChange,
  onRemoveInstance,
  onConfigureInstance,
  readingQueues = {},
  onApplyReadingQueue,
}: DashboardGridProps) {
  const t = useTranslations('dashboard');
  const gridRef = useRef<HTMLDivElement>(null);
  // A hidden grid remains unmeasured until ResizeObserver reports a positive width.
  const [gridWidth, setGridWidth] = useState(0);
  const columnCount = Math.max(1, Math.min(20, Math.floor(columns)));
  const visibleGroupKey = [...visibleInstanceIds].sort().join('\u0000');
  const layoutContextRef = useRef({ breakpoint, requestedBreakpoint, columnCount, visibleGroupKey, interactionEnabled: isEditMode && !interactionPending });
  useLayoutEffect(() => {
    layoutContextRef.current = { breakpoint, requestedBreakpoint, columnCount, visibleGroupKey, interactionEnabled: isEditMode && !interactionPending };
  });

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
  }, []);

  const [selectedInstanceId, setSelectedInstanceId] = useState<string | null>(null);
  const [gesture, setGesture] = useState<ActiveGesture | null>(null);
  const [interactionNotice, setInteractionNotice] = useState<string | null>(null);
  const [frameHeaderHeight, setFrameHeaderHeight] = useState(0);

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

  const [announcement, setAnnouncement] = useState<string>('');

  // Map instances by id for quick lookup
  const instancesById = useMemo(() => {
    return new Map(instances.map((inst) => [inst.id, inst]));
  }, [instances]);
  const visibleIds = useMemo(() => new Set(visibleInstanceIds), [visibleInstanceIds]);
  const hasCompleteCoverage = useMemo(() => {
    const placementIds = new Set(placements.map((placement) => placement.instance_id));
    return placementIds.size === instances.length && placements.length === instances.length
      && instances.every((instance) => placementIds.has(instance.id));
  }, [instances, placements]);

  // Merge placements with active gesture if any
  const effectivePlacements = useMemo(() => {
    if (!gesture || gesture.columnCount !== columnCount || gesture.breakpoint !== breakpoint) return placements;
    return placements.map((p) =>
      p.instance_id === gesture.instanceId ? gesture.currentPlacement : p,
    );
  }, [breakpoint, columnCount, gesture, placements]);

  /** Resolves renderer-specific integer width and height bounds. */
  const getMinSize = useCallback(
    (instanceId: string) => {
      const inst = instancesById.get(instanceId);
      const renderer = inst?.definition?.renderer ?? '';
      const minimum = RENDERER_MIN_SIZES[renderer] ?? DEFAULT_MIN_SIZE;
      return minimum;
    },
    [instancesById],
  );

  /** Keeps one measured fixed frame-header height for the current fixed two-row chrome. */
  const recordHeaderHeight = useCallback((height: number) => {
    if (Number.isFinite(height) && height > 0) {
      setFrameHeaderHeight((current) => current === height ? current : height);
    }
  }, []);

  /** Initiates drag to move gesture on header pointer down. */
  const handleDragStart = useCallback(
    (instanceId: string, event: React.PointerEvent) => {
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
        type: 'move',
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
    },
    [breakpoint, columnCount, getMinSize, hasCompleteCoverage, isEditMode, placements, t, visibleGroupKey],
  );

  /** Initiates resize gesture on handle pointer down. */
  const handleResizeStart = useCallback(
    (instanceId: string, direction: 'e' | 's' | 'se', event: React.PointerEvent) => {
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
        type: 'resize',
        direction,
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
    },
    [breakpoint, columnCount, getMinSize, hasCompleteCoverage, isEditMode, placements, t, visibleGroupKey],
  );

  // Global window listeners for pointermove, pointerup, and per-gesture Escape cancel
  useEffect(() => {
    if (!gesture) return;

    /** Window pointer move handler updating gesture draft. */
    const onPointerMove = (e: PointerEvent) => {
      const grid = gridRef.current;
      if (!grid || !layoutContextRef.current.interactionEnabled
        || layoutContextRef.current.requestedBreakpoint !== gesture.breakpoint
        || gesture.columnCount !== layoutContextRef.current.columnCount
        || gesture.breakpoint !== layoutContextRef.current.breakpoint
        || gesture.visibleGroupKey !== layoutContextRef.current.visibleGroupKey
        || gesture.cellStride <= 0) return;
      const width = grid.getBoundingClientRect().width;
      if (!getGridUnitMetrics(width, columnCount)) return;
      const dx = Math.round((e.clientX - gesture.startX) / gesture.cellStride);
      const dy = Math.round((e.clientY - gesture.startY) / gesture.cellStride);

      if (gesture.type === 'move') {
        const nextX = Math.max(0, Math.min(columnCount - gesture.initialPlacement.w, gesture.initialPlacement.x + dx));
        const nextY = Math.max(0, gesture.initialPlacement.y + dy);

        setGesture((prev) =>
          prev
            ? {
                ...prev,
                currentPlacement: {
                  ...prev.currentPlacement,
                  x: nextX,
                  y: nextY,
                },
              }
            : null,
        );
      } else if (gesture.type === 'resize') {
        let nextW = gesture.initialPlacement.w;
        let nextH = gesture.initialPlacement.h;

        if (gesture.direction === 'e' || gesture.direction === 'se') {
          nextW = Math.max(gesture.minW, Math.min(columnCount - gesture.initialPlacement.x, gesture.initialPlacement.w + dx));
        }
        if (gesture.direction === 's' || gesture.direction === 'se') {
          nextH = Math.max(gesture.minH, gesture.initialPlacement.h + dy);
        }

        setGesture((prev) =>
          prev
            ? {
                ...prev,
                currentPlacement: {
                  ...prev.currentPlacement,
                  w: nextW,
                  h: nextH,
                },
              }
            : null,
        );
      }
    };

    /** Window pointer up handler committing gesture with collision displacement. */
    const onPointerUp = () => {
      const width = gridRef.current?.getBoundingClientRect().width ?? 0;
      const metrics = getGridUnitMetrics(width, columnCount);
      if (!layoutContextRef.current.interactionEnabled
        || layoutContextRef.current.requestedBreakpoint !== gesture.breakpoint
        || gesture.columnCount !== layoutContextRef.current.columnCount
        || gesture.breakpoint !== layoutContextRef.current.breakpoint
        || gesture.visibleGroupKey !== layoutContextRef.current.visibleGroupKey
        || !metrics) {
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
      if (e.key === 'Escape') {
        e.preventDefault();
        if (!layoutContextRef.current.interactionEnabled
          || layoutContextRef.current.requestedBreakpoint !== gesture.breakpoint
          || gesture.columnCount !== layoutContextRef.current.columnCount
          || gesture.breakpoint !== layoutContextRef.current.breakpoint
          || gesture.visibleGroupKey !== layoutContextRef.current.visibleGroupKey) {
          setGesture(null);
          return;
        }
        // Pointer movement is transient; cancelling adds no history snapshot.
        setGesture(null);
      }
    };

    window.addEventListener('pointermove', onPointerMove);
    window.addEventListener('pointerup', onPointerUp);
    window.addEventListener('keydown', onKeyDown);

    return () => {
      window.removeEventListener('pointermove', onPointerMove);
      window.removeEventListener('pointerup', onPointerUp);
      window.removeEventListener('keydown', onKeyDown);
    };
  }, [breakpoint, columnCount, gesture, getMinSize, onLayoutChange, placements, t]);

  /** Keyboard alternative navigation in edit mode (Arrow keys move, Shift+Arrow resizes). */
  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent<HTMLDivElement>, instanceId: string) => {
      if (!isEditMode) return;
      if (!hasCompleteCoverage) {
        setInteractionNotice(t('layoutActionUnavailable'));
        return;
      }
      const current = placements.find((p) => p.instance_id === instanceId);
      if (!current) return;

      const minSize = getMinSize(instanceId);
      let changed = false;
      const next = { ...current };

      if (e.shiftKey) {
        // Shift + Arrows -> Resize
        if (e.key === 'ArrowRight' && next.x + next.w < columnCount) {
          next.w += 1;
          changed = true;
        } else if (e.key === 'ArrowLeft' && next.w > minSize.minW) {
          next.w -= 1;
          changed = true;
        } else if (e.key === 'ArrowDown') {
          next.h += 1;
          changed = true;
        } else if (e.key === 'ArrowUp' && next.h > minSize.minH) {
          next.h -= 1;
          changed = true;
        }
      } else {
        // Arrows -> Move
        if (e.key === 'ArrowRight' && next.x + next.w < columnCount) {
          next.x += 1;
          changed = true;
        } else if (e.key === 'ArrowLeft' && next.x > 0) {
          next.x -= 1;
          changed = true;
        } else if (e.key === 'ArrowDown') {
          next.y += 1;
          changed = true;
        } else if (e.key === 'ArrowUp' && next.y > 0) {
          next.y -= 1;
          changed = true;
        }
      }

      if (changed) {
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
    },
    [breakpoint, columnCount, getMinSize, hasCompleteCoverage, isEditMode, onLayoutChange, placements, t],
  );

  // Matching column width and row height makes every persisted unit square; gaps define the shared stride.
  const gridMetrics = useMemo(() => getGridUnitMetrics(gridWidth, columnCount), [columnCount, gridWidth]);
  const cellSize = gridMetrics?.cellSize ?? 0;
  const readableProjection = useMemo(() => isMobileView
    ? projectReadableMobilePlacements(placements, instances, columnCount, cellSize, frameHeaderHeight)
    : null,
  [cellSize, columnCount, frameHeaderHeight, instances, isMobileView, placements]);
  const projectionMeasurementReady = gridMetrics !== null && frameHeaderHeight > 0;
  const displayPlacements = isMobileView && !isEditMode
    ? readableProjection ?? effectivePlacements
    : effectivePlacements;

  // Calculate highest row to render adequate grid background in edit mode and view projection.
  const maxRow = useMemo(() => {
    let max = 12;
    for (const p of displayPlacements) {
      if (p.y + p.h > max) max = p.y + p.h;
    }
    return max + (isEditMode ? 4 : 0);
  }, [displayPlacements, isEditMode]);

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

  const projectionPending = isMobileView && !isEditMode && !projectionMeasurementReady;
  const projectionUnavailable = isMobileView && !isEditMode && projectionMeasurementReady && !readableProjection;
  const overlapCount = gesture
    ? placements.filter((p) => p.instance_id !== gesture.instanceId && doPlacementsOverlap(gesture.currentPlacement, p)).length
    : 0;
  const gestureNotice = gesture ? (overlapCount > 0 ? t('overlapNotice', { count: overlapCount }) : t('dropFits')) : '';
  const liveText = gesture
    ? `${t('positionLabel', { x: gesture.currentPlacement.x + 1, y: gesture.currentPlacement.y + 1, w: gesture.currentPlacement.w, h: gesture.currentPlacement.h })}. ${gestureNotice}`
    : announcement;
  const gridMinHeight = maxRow * cellSize + Math.max(0, maxRow - 1) * GRID_GAP_PX;

  return (
    <div
      ref={gridRef}
      className={`relative w-full ${isEditMode ? 'select-none' : ''}`}
      style={{
        display: 'grid',
        gridTemplateColumns: `repeat(${columnCount}, ${cellSize}px)`,
        gridAutoRows: `${cellSize}px`,
        gap: `${GRID_GAP_PX}px`,
        minHeight: `${gridMinHeight}px`,
      }}
    >
      {isEditMode && <p role="status" aria-live="polite" className="sr-only">{liveText}</p>}
      {interactionNotice && (
        <p role="status" className="absolute inset-x-0 top-0 z-20 rounded-md bg-card/95 px-3 py-2 text-sm text-destructive">
          {interactionNotice}
        </p>
      )}
      {projectionPending && (
        <p role="status" className="absolute inset-x-0 top-0 z-20 rounded-md bg-card/95 px-3 py-2 text-sm text-muted-foreground">
          {t('mobileProjectionPending')}
        </p>
      )}
      {projectionUnavailable && (
        <p role="status" className="absolute inset-x-0 top-0 z-20 rounded-md bg-card/95 px-3 py-2 text-sm text-destructive">
          {t('mobileLayoutUnavailable')}
        </p>
      )}
      {/* Background grid guides in edit mode */}
      {isEditMode && (
        <div
          aria-hidden="true"
          className="absolute inset-0 pointer-events-none rounded-xl border border-dashed border-border/40 grid"
          style={{
            gridTemplateColumns: `repeat(${columnCount}, ${cellSize}px)`,
            gridAutoRows: `${cellSize}px`,
            gap: `${GRID_GAP_PX}px`,
          }}
        >
          {Array.from({ length: columnCount * maxRow }).map((_, i) => (
            <div
              key={i}
              className="border border-border/20 rounded-md bg-primary/[0.015]"
            />
          ))}
        </div>
      )}

      {/* Render each gadget placement */}
      {displayPlacements.map((placement) => {
        if (!visibleIds.has(placement.instance_id)) return null;
        const instance = instancesById.get(placement.instance_id);
        if (!instance) return null;

        const isGestureTarget = gesture?.instanceId === placement.instance_id;
        const isSelected = selectedInstanceId === placement.instance_id;

        const RendererComponent = resolveGadgetRenderer(instance.definition.renderer);

        return (
          <div
            key={placement.instance_id}
            onKeyDown={(e) => handleKeyDown(e, placement.instance_id)}
            style={{
              gridColumn: `${placement.x + 1} / span ${placement.w}`,
              gridRow: `${placement.y + 1} / span ${placement.h}`,
              zIndex: isGestureTarget ? 30 : 10,
              visibility: projectionPending ? 'hidden' : undefined,
            }}
            className={`relative flex flex-col min-h-0 min-w-0 rounded-xl ${isGestureTarget ? (overlapCount > 0 ? 'outline outline-2 outline-destructive' : 'outline outline-2 outline-primary') : ''}`}
          >
            <GadgetFrame
              instance={instance}
              isEditMode={isEditMode}
              isDragging={isGestureTarget && gesture.type === 'move'}
              isResizing={isGestureTarget && gesture.type === 'resize'}
              isSelected={isSelected}
              unreadCount={0}
              readingQueueCount={readingQueues[placement.instance_id] ?? 0}
              onApplyReadingQueue={() => onApplyReadingQueue?.(placement.instance_id)}
              onSelect={() => setSelectedInstanceId(placement.instance_id)}
              onDragHandlePointerDown={(e) => handleDragStart(placement.instance_id, e)}
              onHeaderMeasured={recordHeaderHeight}
              onConfigure={() => onConfigureInstance?.(instance)}
              onRemove={() => onRemoveInstance?.(placement.instance_id)}
            >
              {RendererComponent ? (
                <RendererComponent instance={instance} isEditMode={isEditMode} />
              ) : null}
            </GadgetFrame>

            {/* Resize handles rendered only in edit mode */}
            {isEditMode && (
              <>
                {/* East / Right edge handle */}
                <div
                  role="separator"
                  aria-label={t('resizeWidth')}
                  onPointerDown={(e) => handleResizeStart(placement.instance_id, 'e', e)}
                  className="absolute right-0 top-3 bottom-3 w-2.5 cursor-ew-resize hover:bg-primary/40 active:bg-primary rounded-r-md transition-colors z-20"
                />

                {/* South / Bottom edge handle */}
                <div
                  role="separator"
                  aria-label={t('resizeHeight')}
                  onPointerDown={(e) => handleResizeStart(placement.instance_id, 's', e)}
                  className="absolute bottom-0 left-3 right-3 h-2.5 cursor-ns-resize hover:bg-primary/40 active:bg-primary rounded-b-md transition-colors z-20"
                />

                {/* South-East / Corner handle */}
                <div
                  role="separator"
                  aria-label={t('resizeCorner')}
                  onPointerDown={(e) => handleResizeStart(placement.instance_id, 'se', e)}
                  className="absolute right-0 bottom-0 w-4 h-4 cursor-nwse-resize flex items-end justify-end p-0.5 z-20 group"
                >
                  <div className="w-2.5 h-2.5 rounded-br-sm border-r-2 border-b-2 border-muted-foreground group-hover:border-primary" />
                </div>
              </>
            )}

            {/* Live tooltip showing integer coordinates during gesture */}
            {isGestureTarget && (
              <div className={`absolute -top-7 left-1/2 -translate-x-1/2 px-2 py-0.5 rounded text-[11px] shadow-md z-40 max-w-xs pointer-events-none ${overlapCount > 0 ? 'bg-destructive text-destructive-foreground' : 'bg-foreground text-background'}`}>
                <span className="font-mono">{t('positionLabel', {
                  x: placement.x + 1,
                  y: placement.y + 1,
                  w: placement.w,
                  h: placement.h,
                })}</span>
                <span className="ml-2">{gestureNotice}</span>
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}
