'use client';

import { useTranslations } from 'next-intl';
import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { DashboardPlacement, GadgetInstance } from './api';
import { GadgetFrame } from './gadget-frame';
import { resolveGadgetRenderer } from './widget-registry';

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

/** Tests whether two integer placements overlap on a half-open rectangle basis. */
export function doPlacementsOverlap(a: DashboardPlacement, b: DashboardPlacement): boolean {
  return a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h;
}

/**
 * Resolves rectangle collisions by predictably pushing down overlapping items.
 *
 * @param placements Placements to check and resolve.
 * @param activeId Identifier of the actively positioned item that has right-of-way.
 * @returns Non-overlapping array of integer placements.
 */
export function resolveCollisions(
  placements: DashboardPlacement[],
  activeId: string,
): DashboardPlacement[] {
  const result: DashboardPlacement[] = placements.map((p) => ({ ...p }));
  const active = result.find((p) => p.instance_id === activeId);
  if (!active) return result;

  // Cascade push down
  let hasCollision = true;
  let iterations = 0;
  const maxIterations = 50;

  while (hasCollision && iterations < maxIterations) {
    hasCollision = false;
    iterations++;

    for (let i = 0; i < result.length; i++) {
      const item = result[i];
      for (let j = 0; j < result.length; j++) {
        if (i === j) continue;
        const other = result[j];
        if (doPlacementsOverlap(item, other)) {
          hasCollision = true;
          // Push down the one that is not active or has higher/equal y
          if (item.instance_id === activeId) {
            other.y = Math.max(other.y, item.y + item.h);
          } else if (other.instance_id === activeId) {
            item.y = Math.max(item.y, other.y + other.h);
          } else if (item.y <= other.y) {
            other.y = Math.max(other.y, item.y + item.h);
          } else {
            item.y = Math.max(item.y, other.y + other.h);
          }
        }
      }
    }
  }

  return result;
}

/** Props for the DashboardGrid renderer. */
export interface DashboardGridProps {
  /** Array of gadget instances to render. */
  instances: GadgetInstance[];
  /** Integer placements mapping each gadget instance to its grid coordinates. */
  placements: DashboardPlacement[];
  /** Requested column count, clamped to the supported range of one through 20. */
  columns?: number;
  /** Layout identity used to cancel gestures when the parent switches breakpoints. */
  breakpoint: 'desktop' | 'mobile';
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
 * Operates on square integer units with at most 20 columns without moving during streaming.
 * In edit mode, supports title drag-to-move, edge/corner resizing, collision displacement,
 * per-gesture cancel via Escape, and keyboard alternatives.
 *
 * @param props Grid instances, integer placements, edit mode flag, and interaction handlers.
 * @returns Fully responsive, accessible dashboard grid container.
 */
export function DashboardGrid({
  instances,
  placements,
  columns = 20,
  breakpoint,
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
  const layoutContextRef = useRef({ breakpoint, columnCount });
  layoutContextRef.current = { breakpoint, columnCount };

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

  /** Drops pointer drafts when the dashboard switches between independent breakpoint layouts. */
  useEffect(() => {
    setGesture((current) => current?.columnCount === columnCount && current.breakpoint === breakpoint ? current : null);
  }, [breakpoint, columnCount]);

  // Map instances by id for quick lookup
  const instancesById = useMemo(() => {
    return new Map(instances.map((inst) => [inst.id, inst]));
  }, [instances]);

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

  /** Initiates drag to move gesture on header pointer down. */
  const handleDragStart = useCallback(
    (instanceId: string, event: React.PointerEvent) => {
      if (!isEditMode || event.button !== 0) return;
      const initial = placements.find((p) => p.instance_id === instanceId);
      if (!initial) return;
      const width = gridRef.current?.getBoundingClientRect().width ?? 0;
      const cellSize = (width - GRID_GAP_PX * (columnCount - 1)) / columnCount;
      if (cellSize <= 0) return;

      const minSize = getMinSize(instanceId);
      setSelectedInstanceId(instanceId);
      setGesture({
        type: 'move',
        instanceId,
        breakpoint,
        columnCount,
        startX: event.clientX,
        startY: event.clientY,
        cellStride: cellSize + GRID_GAP_PX,
        initialPlacement: { ...initial },
        currentPlacement: { ...initial },
        backupPlacements: placements.map((p) => ({ ...p })),
        minW: minSize.minW,
        minH: minSize.minH,
      });

      event.preventDefault();
      event.stopPropagation();
    },
    [breakpoint, columnCount, getMinSize, isEditMode, placements],
  );

  /** Initiates resize gesture on handle pointer down. */
  const handleResizeStart = useCallback(
    (instanceId: string, direction: 'e' | 's' | 'se', event: React.PointerEvent) => {
      if (!isEditMode || event.button !== 0) return;
      const initial = placements.find((p) => p.instance_id === instanceId);
      if (!initial) return;
      const width = gridRef.current?.getBoundingClientRect().width ?? 0;
      const cellSize = (width - GRID_GAP_PX * (columnCount - 1)) / columnCount;
      if (cellSize <= 0) return;

      const minSize = getMinSize(instanceId);
      setSelectedInstanceId(instanceId);
      setGesture({
        type: 'resize',
        direction,
        instanceId,
        breakpoint,
        columnCount,
        startX: event.clientX,
        startY: event.clientY,
        cellStride: cellSize + GRID_GAP_PX,
        initialPlacement: { ...initial },
        currentPlacement: { ...initial },
        backupPlacements: placements.map((p) => ({ ...p })),
        minW: minSize.minW,
        minH: minSize.minH,
      });

      event.preventDefault();
      event.stopPropagation();
    },
    [breakpoint, columnCount, getMinSize, isEditMode, placements],
  );

  // Global window listeners for pointermove, pointerup, and per-gesture Escape cancel
  useEffect(() => {
    if (!gesture) return;

    /** Window pointer move handler updating gesture draft. */
    const onPointerMove = (e: PointerEvent) => {
      const grid = gridRef.current;
      if (!grid
        || gesture.columnCount !== layoutContextRef.current.columnCount
        || gesture.breakpoint !== layoutContextRef.current.breakpoint
        || gesture.cellStride <= 0) return;
      const width = grid.getBoundingClientRect().width;
      const cellSize = (width - GRID_GAP_PX * (columnCount - 1)) / columnCount;
      if (cellSize <= 0) return;
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
      const cellSize = (width - GRID_GAP_PX * (columnCount - 1)) / columnCount;
      if (gesture.columnCount !== layoutContextRef.current.columnCount
        || gesture.breakpoint !== layoutContextRef.current.breakpoint
        || cellSize <= 0) {
        setGesture(null);
        return;
      }
      const updated = placements.map((p) =>
        p.instance_id === gesture.instanceId ? gesture.currentPlacement : p,
      );
      const resolved = resolveCollisions(updated, gesture.instanceId);
      setGesture(null);
      onLayoutChange?.(resolved);
    };

    /** Per-gesture cancel via Escape key restoring exact pre-gesture placement snapshot. */
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault();
        if (gesture.columnCount !== layoutContextRef.current.columnCount
          || gesture.breakpoint !== layoutContextRef.current.breakpoint) {
          setGesture(null);
          return;
        }
        // Restore pre-gesture placements immediately
        onLayoutChange?.(gesture.backupPlacements);
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
  }, [breakpoint, columnCount, gesture, onLayoutChange, placements]);

  /** Keyboard alternative navigation in edit mode (Arrow keys move, Shift+Arrow resizes). */
  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent<HTMLDivElement>, instanceId: string) => {
      if (!isEditMode) return;
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
        const resolved = resolveCollisions(updated, instanceId);
        onLayoutChange?.(resolved);
      } else if (e.key === 'Escape') {
        setSelectedInstanceId(null);
      }
    },
    [breakpoint, columnCount, getMinSize, isEditMode, onLayoutChange, placements],
  );

  // Calculate highest row to render adequate grid background in edit mode
  const maxRow = useMemo(() => {
    let max = 12;
    for (const p of effectivePlacements) {
      if (p.y + p.h > max) max = p.y + p.h;
    }
    return max + (isEditMode ? 4 : 0);
  }, [effectivePlacements, isEditMode]);

  // Matching column width and row height makes every persisted unit square; gaps define the shared stride.
  const cellSize = Math.max(0, (gridWidth - GRID_GAP_PX * (columnCount - 1)) / columnCount);
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
      {effectivePlacements.map((placement) => {
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
            }}
            className="relative flex flex-col min-h-0 min-w-0"
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
              <div className="absolute -top-7 left-1/2 -translate-x-1/2 px-2 py-0.5 rounded bg-foreground text-background text-[11px] font-mono shadow-md z-40 whitespace-nowrap pointer-events-none">
                {t('positionLabel', {
                  x: placement.x + 1,
                  y: placement.y + 1,
                  w: placement.w,
                  h: placement.h,
                })}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}
