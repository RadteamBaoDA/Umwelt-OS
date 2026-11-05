import type { DashboardPlacement } from './api';

/** Known minimum integer dimensions for standard gadget renderers. */
export const RENDERER_MIN_SIZES: Record<string, { minW: number; minH: number }> = {
  news_feed: { minW: 4, minH: 4 },
  telegram_feed: { minW: 4, minH: 4 },
  text_panel: { minW: 4, minH: 3 },
  table_panel: { minW: 6, minH: 4 },
  job_list: { minW: 6, minH: 4 },
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
  provider_observation: { minW: 4, minH: 3 },
  intelligence_panel: { minW: 8, minH: 6 },
};

/** Preferred first-placement size for new gadgets (mirrors server DEFAULT_SIZES); never below the minimum. */
export const RENDERER_DEFAULT_SIZES: Record<string, { defaultW: number; defaultH: number }> = {
  news_feed: { defaultW: 6, defaultH: 8 },
  telegram_feed: { defaultW: 6, defaultH: 8 },
  text_panel: { defaultW: 6, defaultH: 5 },
  table_panel: { defaultW: 10, defaultH: 7 },
  job_list: { defaultW: 10, defaultH: 8 },
  video_panel: { defaultW: 8, defaultH: 7 },
  finance_chart: { defaultW: 8, defaultH: 6 },
  personal_context: { defaultW: 5, defaultH: 5 },
  map: { defaultW: 12, defaultH: 9 },
  intelligence_panel: { defaultW: 10, defaultH: 7 },
  highlights: { defaultW: 5, defaultH: 5 },
  watch_rules: { defaultW: 5, defaultH: 5 },
  tasks: { defaultW: 5, defaultH: 6 },
  goals: { defaultW: 5, defaultH: 6 },
  daily_brief: { defaultW: 8, defaultH: 7 },
  weather: { defaultW: 5, defaultH: 5 },
  provider_observation: { defaultW: 5, defaultH: 4 },
  research: { defaultW: 5, defaultH: 5 },
  github_project: { defaultW: 8, defaultH: 6 },
};

/** Default fallback minimum size if renderer is unlisted. */
export const DEFAULT_MIN_SIZE = { minW: 4, minH: 3 };

/** Shared spacing between square dashboard units in the grid and its guides. */
export const GRID_GAP_PX = 12;
export const MAX_LAYOUT_ROWS = 100_000;

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

/** Active pointer gesture state for drag to move or corner resize. */
export interface ActiveGesture {
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

/** Latest layout facts a running gesture must still match to commit (read through a ref at event time). */
export interface GridLayoutContext {
  breakpoint: 'desktop' | 'mobile';
  requestedBreakpoint: 'desktop' | 'mobile';
  columnCount: number;
  visibleGroupKey: string;
  interactionEnabled: boolean;
}

/**
 * True when the gesture no longer belongs to the live layout: editing was disabled, the physical viewport asks for
 * another breakpoint, or the column count, breakpoint or visible group changed since pointer-down.
 */
export function isGestureStale(gesture: ActiveGesture, context: GridLayoutContext): boolean {
  return !context.interactionEnabled
    || context.requestedBreakpoint !== gesture.breakpoint
    || gesture.columnCount !== context.columnCount
    || gesture.breakpoint !== context.breakpoint
    || gesture.visibleGroupKey !== context.visibleGroupKey;
}

/**
 * Applies a pointer position to a gesture and returns the dragged placement. Moves clamp x to the columns and y to
 * non-negative rows; resizes clamp width to the renderer minimum and the right edge, height to the minimum.
 *
 * @param gesture The running gesture (initial placement, frozen stride, minimums).
 * @param clientX Pointer x in viewport pixels.
 * @param clientY Pointer y in viewport pixels.
 * @param columnCount Current column count.
 * @returns The changed coordinates (x/y for a move, w/h for a resize) to merge into the current placement.
 */
export function placementForPointer(gesture: ActiveGesture, clientX: number, clientY: number, columnCount: number): Pick<DashboardPlacement, 'x' | 'y'> | Pick<DashboardPlacement, 'w' | 'h'> {
  const dx = Math.round((clientX - gesture.startX) / gesture.cellStride);
  const dy = Math.round((clientY - gesture.startY) / gesture.cellStride);
  const { initialPlacement: initial } = gesture;
  if (gesture.type === 'move') {
    return {
      x: Math.max(0, Math.min(columnCount - initial.w, initial.x + dx)),
      y: Math.max(0, initial.y + dy),
    };
  }
  let nextW = initial.w;
  let nextH = initial.h;
  if (gesture.direction === 'e' || gesture.direction === 'se') {
    nextW = Math.max(gesture.minW, Math.min(columnCount - initial.x, initial.w + dx));
  }
  if (gesture.direction === 's' || gesture.direction === 'se') {
    nextH = Math.max(gesture.minH, initial.h + dy);
  }
  return { w: nextW, h: nextH };
}

/** Shift+arrow: resize by one unit; null when the key does nothing or would go below the minimum / past the edge. */
function resizeByKey(next: DashboardPlacement, key: string, columnCount: number, minSize: { minW: number; minH: number }): boolean {
  if (key === 'ArrowRight' && next.x + next.w < columnCount) next.w += 1;
  else if (key === 'ArrowLeft' && next.w > minSize.minW) next.w -= 1;
  else if (key === 'ArrowDown') next.h += 1;
  else if (key === 'ArrowUp' && next.h > minSize.minH) next.h -= 1;
  else return false;
  return true;
}

/** Arrow: move by one unit; false when the key does nothing or would leave the grid. */
function moveByKey(next: DashboardPlacement, key: string, columnCount: number): boolean {
  if (key === 'ArrowRight' && next.x + next.w < columnCount) next.x += 1;
  else if (key === 'ArrowLeft' && next.x > 0) next.x -= 1;
  else if (key === 'ArrowDown') next.y += 1;
  else if (key === 'ArrowUp' && next.y > 0) next.y -= 1;
  else return false;
  return true;
}

/**
 * Keyboard alternative to dragging: arrows move by one unit, Shift+arrows resize by one unit.
 *
 * @returns The adjusted placement, or null when the key does nothing (or the move would leave the grid / go below the minimum).
 */
export function nudgePlacement(
  current: DashboardPlacement,
  key: string,
  shift: boolean,
  columnCount: number,
  minSize: { minW: number; minH: number },
): DashboardPlacement | null {
  const next = { ...current };
  const changed = shift ? resizeByKey(next, key, columnCount, minSize) : moveByKey(next, key, columnCount);
  return changed ? next : null;
}
