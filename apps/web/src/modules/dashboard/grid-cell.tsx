'use client';

import { useQuery } from '@tanstack/react-query';
import { useTranslations } from 'next-intl';
import React, { useMemo } from 'react';
import type { DashboardPlacement, GadgetInstance } from './api';
import { listSources, sourceKeys } from '@/modules/sources';
import { computeFreshnessTick, type FreshnessTick } from './freshness-strip';
import { GadgetFrame } from './gadget-frame';
import type { ActiveGesture } from './grid-geometry';
import { isGadgetRendererRegistered, RegisteredGadget } from './widget-registry';

/** Props of one grid cell (a gadget frame plus its edit-mode handles and live gesture tooltip). */
export interface GridCellProps {
  placement: DashboardPlacement;
  instance: GadgetInstance;
  isEditMode: boolean;
  isSelected: boolean;
  /** The running gesture when it targets this cell, otherwise null. */
  gesture: ActiveGesture | null;
  /** Number of other placements the dragged rectangle currently overlaps. */
  overlapCount: number;
  /** Localized "fits / overlaps" status shown in the gesture tooltip. */
  gestureNotice: string;
  projectionPending: boolean;
  readingQueueCount: number;
  onKeyDown: (event: React.KeyboardEvent<HTMLDivElement>) => void;
  onSelect: () => void;
  onDragStart: (event: React.PointerEvent) => void;
  onResizeStart: (direction: 'e' | 's' | 'se', event: React.PointerEvent) => void;
  onHeaderMeasured: (height: number) => void;
  onConfigure: () => void;
  onRemove: () => void;
  onApplyReadingQueue: () => void;
}

/** The three resize handles (east edge, south edge, south-east corner) shown in edit mode. */
function ResizeHandles({ onResizeStart }: { onResizeStart: GridCellProps['onResizeStart'] }) {
  const t = useTranslations('dashboard');
  return (
    <>
      {/* East / Right edge handle */}
      <div
        role="separator"
        aria-label={t('resizeWidth')}
        onPointerDown={(e) => onResizeStart('e', e)}
        className="absolute right-0 top-3 bottom-3 w-2.5 cursor-ew-resize hover:bg-primary/40 active:bg-primary rounded-r-md transition-colors z-20"
      />

      {/* South / Bottom edge handle */}
      <div
        role="separator"
        aria-label={t('resizeHeight')}
        onPointerDown={(e) => onResizeStart('s', e)}
        className="absolute bottom-0 left-3 right-3 h-2.5 cursor-ns-resize hover:bg-primary/40 active:bg-primary rounded-b-md transition-colors z-20"
      />

      {/* South-East / Corner handle */}
      <div
        role="separator"
        aria-label={t('resizeCorner')}
        onPointerDown={(e) => onResizeStart('se', e)}
        className="absolute right-0 bottom-0 w-4 h-4 cursor-nwse-resize flex items-end justify-end p-0.5 z-20 group"
      >
        <div className="w-2.5 h-2.5 rounded-br-sm border-r-2 border-b-2 border-muted-foreground group-hover:border-primary" />
      </div>
    </>
  );
}

/** Live tooltip showing integer coordinates (and the size while resizing) during a gesture. */
function GestureTooltip({ gesture, placement, overlapCount, gestureNotice }: Pick<GridCellProps, 'placement' | 'overlapCount' | 'gestureNotice'> & { gesture: ActiveGesture }) {
  const t = useTranslations('dashboard');
  return (
    <div className={`absolute -top-7 left-1/2 -translate-x-1/2 px-2 py-0.5 rounded text-[11px] shadow-[var(--shadow-popover)] z-40 max-w-xs pointer-events-none ${overlapCount > 0 ? 'bg-destructive text-destructive-foreground' : 'bg-foreground text-background'}`}>
      {gesture.type === 'resize' ? (
        <span className="tabular-nums font-semibold">{t('resizingLabel', { w: placement.w, h: placement.h })}</span>
      ) : null}
      <span className={gesture.type === 'resize' ? 'ml-2 tabular-nums' : 'tabular-nums'}>{t('positionLabel', {
        x: placement.x + 1,
        y: placement.y + 1,
        w: placement.w,
        h: placement.h,
      })}</span>
      <span className="ml-2">{gestureNotice}</span>
    </div>
  );
}

/** Outline classes of the gesture target: destructive while overlapping another gadget, primary when it fits. */
function gestureOutline(isTarget: boolean, overlapCount: number): string {
  if (!isTarget) return '';
  return overlapCount > 0 ? 'outline outline-2 outline-destructive' : 'outline outline-2 outline-primary';
}

/**
 * Freshness ticks of the active sources an instance reads. Shares the dashboard's `[...sourceKeys.list,'dashboard-stale']`
 * cache entry, so it adds no request beyond the one `useDashboardData` already makes; disabled for instances without sources.
 *
 * @param instance Gadget instance whose `definition.source_ids` are looked up.
 * @returns Ticks for the bound, active sources; empty while loading or unbound.
 */
function useSourceHealth(instance: GadgetInstance): FreshnessTick[] {
  const ids = instance.definition.source_ids ?? [];
  const query = useQuery({ queryKey: [...sourceKeys.list, 'dashboard-stale'], queryFn: () => listSources(), enabled: ids.length > 0 });
  const items = query.data?.items;
  const idKey = ids.join(',');
  // Freshness is a wall-clock judgement, so it is recomputed whenever the source rows refresh (dataUpdatedAt), not on a timer.
  const asOf = query.dataUpdatedAt;
  return useMemo(
    () => (items ?? []).filter((s) => idKey.split(',').includes(s.id) && s.status === 'active').map((s) => computeFreshnessTick(s, asOf)),
    [items, idKey, asOf],
  );
}

/**
 * One gadget cell positioned on the grid: frame with the resolved renderer, edit-mode resize handles and the
 * gesture tooltip. Pure presentation; all geometry and handlers come from the grid.
 */
export function GridCell(props: GridCellProps) {
  const { placement, instance, isEditMode, gesture, overlapCount, projectionPending } = props;
  const isGestureTarget = gesture !== null;
  const sourceHealth = useSourceHealth(instance);
  return (
    <div
      onKeyDown={props.onKeyDown}
      style={{
        gridColumn: `${placement.x + 1} / span ${placement.w}`,
        gridRow: `${placement.y + 1} / span ${placement.h}`,
        zIndex: isGestureTarget ? 30 : 10,
        visibility: projectionPending ? 'hidden' : undefined,
      }}
      className={`relative flex flex-col min-h-0 min-w-0 rounded-lg ${gestureOutline(isGestureTarget, overlapCount)}`}
    >
      <GadgetFrame
        instance={instance}
        isEditMode={isEditMode}
        isDragging={isGestureTarget && gesture.type === 'move'}
        isResizing={isGestureTarget && gesture.type === 'resize'}
        isSelected={props.isSelected}
        unreadCount={0}
        sourceHealth={sourceHealth}
        readingQueueCount={props.readingQueueCount}
        onApplyReadingQueue={props.onApplyReadingQueue}
        onSelect={props.onSelect}
        onDragHandlePointerDown={props.onDragStart}
        onHeaderMeasured={props.onHeaderMeasured}
        onConfigure={props.onConfigure}
        onRemove={props.onRemove}
      >
        {/* null (not an element returning null) for planned renderers, so GadgetFrame shows its planned body. */}
        {isGadgetRendererRegistered(instance.definition.renderer)
          ? <RegisteredGadget rendererId={instance.definition.renderer} instance={instance} isEditMode={isEditMode} />
          : null}
      </GadgetFrame>

      {/* Resize handles rendered only in edit mode */}
      {isEditMode && <ResizeHandles onResizeStart={props.onResizeStart} />}

      {/* Live tooltip showing integer coordinates during gesture */}
      {isGestureTarget && <GestureTooltip gesture={gesture} placement={placement} overlapCount={overlapCount} gestureNotice={props.gestureNotice} />}
    </div>
  );
}
