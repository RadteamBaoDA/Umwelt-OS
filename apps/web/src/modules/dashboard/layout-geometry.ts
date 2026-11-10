import { doPlacementsOverlap, RENDERER_MIN_SIZES } from './grid-geometry';
import type { DashboardPlacement, GadgetInstance } from './api';

/** Maximum number of grid rows a saved or draft layout may occupy. */
export const MAX_LAYOUT_ROWS = 100_000;

/** Result of projecting stored mobile geometry: valid/recovered placements or an unavailable marker. */
export type MobileProjection = { placements: DashboardPlacement[]; unavailable: boolean };

/** Immutable identity and baseline owned by one dirty dashboard layout session. */
export interface LayoutEditOrigin {
  dashboardId: string;
  groupId: string;
  breakpoint: 'desktop' | 'mobile';
  columns: number;
  baseRevision: number;
  savedSnapshot: DashboardPlacement[];
}

/** Exact values captured when a layout save begins; later viewport state cannot rebind the request. */
export interface LayoutSaveRequest extends LayoutEditOrigin {
  items: DashboardPlacement[];
}

/** Identity reserved across quick definition creation and dashboard instance creation. */
export interface AddReservation {
  dashboardId: string;
  groupId: string;
  breakpoint: 'desktop' | 'mobile';
  columns: number;
  baseRevision: number;
  renderer: string;
  width: number;
  height: number;
}

/** Renderer minimum used when a renderer has no registered minimum size. */
const FALLBACK_MINIMUM = { minW: 4, minH: 3 };

/**
 * Finds the first free non-overlapping integer rectangle on the 20-column grid.
 *
 * @param existing Current placements on the grid.
 * @param w Width of the new gadget.
 * @param h Height of the new gadget.
 * @param columns Total columns count.
 * @returns Valid free coordinates, or null when bounded space is exhausted or invalid.
 */
export function findFreeCoordinates(
  existing: DashboardPlacement[],
  w: number,
  h: number,
  columns = 20,
): { x: number; y: number } | null {
  if (!Number.isInteger(columns) || columns < 1 || columns > 20
    || !Number.isInteger(w) || !Number.isInteger(h) || w < 1 || w > columns || h < 1) return null;
  let y = 0;
  while (y + h <= MAX_LAYOUT_ROWS) {
    for (let x = 0; x <= columns - w; x++) {
      const candidate: DashboardPlacement = { instance_id: '', x, y, w, h };
      const collides = existing.some((p) => doPlacementsOverlap(candidate, p));
      if (!collides) {
        return { x, y };
      }
    }
    y++;
  }
  return null;
}

/** Layout frames offered for new custom pages; `free` keeps the legacy first-free-slot placement. */
export const LAYOUT_FRAMES = ['free', 'three_columns', 'main_rail', 'hero_grid', 'board'] as const;

/** One of {@link LAYOUT_FRAMES}. */
export type LayoutFrame = (typeof LAYOUT_FRAMES)[number];

/** Fixed row height of every board-frame gadget. */
export const BOARD_ROW_HEIGHT = 8;

/** Zone widths (summing to the 20-column grid) per frame; `free` has none. */
const FRAME_ZONE_WIDTHS: Record<LayoutFrame, readonly number[]> = {
  free: [],
  three_columns: [7, 7, 6],
  main_rail: [14, 6],
  hero_grid: [7, 7, 6],
  board: [5, 5, 5, 5],
};

/** A gadget slot chosen inside a frame zone. */
export interface FrameSpot { x: number; y: number; w: number; h: number }

/**
 * Picks the slot for a new gadget in a layout frame: the zone with the lowest current bottom (shortest column,
 * leftmost on ties) whose width fits `minW`, placed at the zone's width. `hero_grid` gives the first gadget the
 * full width; `board` ignores `h` for a fixed row height. Falls back to the first free slot (width `minW`) when no
 * zone fits or the zone target collides after free dragging. Pure; desktop 20-column grid only.
 *
 * @param existing Placements already in the group.
 * @param frame Frame chosen for the group.
 * @param minW Renderer minimum width.
 * @param h Preferred height.
 * @param columns Grid columns (frames require 20).
 * @returns The slot, or null when neither the frame nor the fallback has room.
 */
export function findFrameCoordinates(
  existing: DashboardPlacement[],
  frame: LayoutFrame,
  minW: number,
  h: number,
  columns = 20,
): FrameSpot | null {
  const fallback = (): FrameSpot | null => {
    const spot = findFreeCoordinates(existing, minW, h, columns);
    return spot && { ...spot, w: minW, h };
  };
  if (frame === 'free' || columns !== 20) return fallback();
  const height = frame === 'board' ? BOARD_ROW_HEIGHT : h;
  const fits = (spot: FrameSpot) => spot.y + spot.h <= MAX_LAYOUT_ROWS
    && !existing.some((p) => doPlacementsOverlap({ instance_id: '', ...spot }, p));
  if (frame === 'hero_grid' && existing.length === 0) {
    const hero = { x: 0, y: 0, w: columns, h: height };
    return fits(hero) ? hero : fallback();
  }
  let x = 0;
  let best: FrameSpot | null = null;
  let bestBottom = Infinity;
  for (const w of FRAME_ZONE_WIDTHS[frame]) {
    const bottom = existing.reduce((max, p) => (p.x < x + w && p.x + p.w > x ? Math.max(max, p.y + p.h) : max), 0);
    if (w >= minW && bottom < bestBottom) { best = { x, y: bottom, w, h: height }; bestBottom = bottom; }
    x += w;
  }
  return best && fits(best) ? best : fallback();
}

/**
 * Packs placements top-left without holes in reading order (y, then x), keeping each w/h.
 * Pure: returns a new array in the input's order; a placement that cannot be re-seated keeps its coordinates.
 *
 * @param placements Current draft placements.
 * @param columns Grid column count.
 */
export function packPlacements(placements: DashboardPlacement[], columns: number): DashboardPlacement[] {
  const packed: DashboardPlacement[] = [];
  const moved = new Map<string, DashboardPlacement>();
  for (const p of [...placements].sort((a, b) => a.y - b.y || a.x - b.x)) {
    const spot = findFreeCoordinates(packed, p.w, p.h, columns);
    const next = spot ? { ...p, ...spot } : { ...p };
    packed.push(next);
    moved.set(p.instance_id, next);
  }
  return placements.map((p) => moved.get(p.instance_id)!);
}

/** Compares complete geometry snapshots independently of array order. */
export function sameLayout(a: DashboardPlacement[], b: DashboardPlacement[]): boolean {
  if (a.length !== b.length) return false;
  const left = new Map(a.map((placement) => [placement.instance_id, placement]));
  return left.size === b.length && b.every((placement) => {
    const saved = left.get(placement.instance_id);
    return saved !== undefined && saved.x === placement.x && saved.y === placement.y
      && saved.w === placement.w && saved.h === placement.h;
  });
}

/** True when `columns` is a usable grid width (integer within 1..20). */
function validColumns(columns: number): boolean {
  return Number.isInteger(columns) && columns >= 1 && columns <= 20;
}

/**
 * True when placements and instances correspond one-to-one (same count, unique placement ids, every
 * instance placed).
 */
function ownsEveryInstance(placements: DashboardPlacement[], instances: GadgetInstance[]): boolean {
  if (placements.length !== instances.length) return false;
  const placementIds = new Set(placements.map((placement) => placement.instance_id));
  return placementIds.size === placements.length && instances.every((instance) => placementIds.has(instance.id));
}

/** True when a rectangle has integer coordinates inside the bounded canvas and meets the renderer minimum. */
function rectWithinBounds(
  placement: DashboardPlacement,
  minimum: { minW: number; minH: number },
  columns: number,
): boolean {
  return [placement.x, placement.y, placement.w, placement.h].every(Number.isInteger)
    && placement.x >= 0 && placement.y >= 0 && placement.w >= minimum.minW
    && placement.h >= minimum.minH && placement.x + placement.w <= columns
    && placement.y + placement.h <= MAX_LAYOUT_ROWS;
}

/** Validates a complete owner snapshot before it enters history or reaches the Save mutation. */
export function isValidLayoutSnapshot(
  placements: DashboardPlacement[],
  instances: GadgetInstance[],
  columns: number,
): boolean {
  if (!validColumns(columns) || placements.length !== instances.length) return false;
  const instancesById = new Map(instances.map((instance) => [instance.id, instance]));
  if (instancesById.size !== instances.length || !ownsEveryInstance(placements, instances)) return false;
  return placements.every((placement, index) => {
    const instance = instancesById.get(placement.instance_id);
    if (!instance) return false;
    const minimum = RENDERER_MIN_SIZES[instance.definition.renderer] ?? FALLBACK_MINIMUM;
    return rectWithinBounds(placement, minimum, columns)
      && !placements.slice(index + 1).some((other) =>
        instancesById.get(other.instance_id)?.group_id === instance.group_id && doPlacementsOverlap(placement, other));
  });
}

/** Orders finite coordinates before malformed values for deterministic local recovery. */
function compareCoordinate(left: number, right: number): number {
  const leftFinite = Number.isFinite(left);
  const rightFinite = Number.isFinite(right);
  return leftFinite && rightFinite ? left - right : leftFinite ? -1 : rightFinite ? 1 : 0;
}

/** Clamps one malformed mobile rectangle to the nearest valid x/w/h/y without resolving overlaps. */
function repairRect(
  source: DashboardPlacement,
  minimum: { minW: number; minH: number },
  columns: number,
): DashboardPlacement {
  const xValid = Number.isInteger(source.x) && source.x >= 0;
  const wValid = Number.isInteger(source.w) && source.w >= minimum.minW
    && source.w <= 20 && xValid && source.x + source.w <= columns;
  return {
    ...source,
    x: wValid ? source.x : 0,
    w: wValid ? source.w : minimum.minW,
    h: Number.isInteger(source.h) && source.h >= minimum.minH && source.h <= MAX_LAYOUT_ROWS
      ? source.h : minimum.minH,
    y: Number.isInteger(source.y) && source.y >= 0 && source.y <= MAX_LAYOUT_ROWS
      ? source.y : 0,
  };
}

/**
 * Pushes `candidate` downward until it no longer overlaps any placed rectangle.
 *
 * @returns False when the candidate cannot move further down inside the bounded canvas.
 */
function resolveOverlapDownward(candidate: DashboardPlacement, placed: DashboardPlacement[]): boolean {
  while (placed.some((other) => doPlacementsOverlap(candidate, other))) {
    const colliding = placed.filter((other) => doPlacementsOverlap(candidate, other));
    const nextY = Math.max(...colliding.map((other) => other.y + other.h));
    if (nextY <= candidate.y || nextY + candidate.h > MAX_LAYOUT_ROWS) return false;
    candidate.y = nextY;
  }
  return true;
}

/** Repairs every rectangle in deterministic order; returns null when any cannot be seated. */
function repairAll(
  placements: DashboardPlacement[],
  minimumSizes: Map<string, { minW: number; minH: number }>,
  columns: number,
): DashboardPlacement[] | null {
  const ordered = [...placements].sort((a, b) => compareCoordinate(a.y, b.y)
    || compareCoordinate(a.x, b.x) || a.instance_id.localeCompare(b.instance_id));
  const repaired: DashboardPlacement[] = [];
  for (const source of ordered) {
    const minimum = minimumSizes.get(source.instance_id);
    if (!minimum) return null;
    const candidate = repairRect(source, minimum, columns);
    if (!resolveOverlapDownward(candidate, repaired)) return null;
    if (candidate.x + candidate.w > columns || candidate.y + candidate.h > MAX_LAYOUT_ROWS) return null;
    repaired.push(candidate);
  }
  return repaired;
}

/**
 * Repairs malformed mobile rectangles locally while preserving independently valid x/w and y
 * priority; overlapping repaired rectangles move only downward. Valid owner layouts pass through
 * unchanged, and no recovery is persisted until an explicit Save.
 *
 * @param placements Saved or draft mobile rectangles.
 * @param instances Dashboard instances and their renderer minimum sizes.
 * @param columns Stored mobile column count.
 * @returns Valid recovered placements, unchanged valid placements, or an unavailable result.
 */
export function projectMobilePlacements(
  placements: DashboardPlacement[],
  instances: GadgetInstance[],
  columns: number,
): MobileProjection {
  const minimumSizes = new Map(instances.map((instance) => [
    instance.id,
    RENDERER_MIN_SIZES[instance.definition.renderer] ?? FALLBACK_MINIMUM,
  ]));
  /** Marks mobile projection as unavailable when ownership or bounded geometry is invalid. */
  const unavailable = (): MobileProjection => ({ placements: [], unavailable: true });
  if (!validColumns(columns) || [...minimumSizes.values()].some(({ minW }) => minW > columns)) return unavailable();
  if (!ownsEveryInstance(placements, instances)) return unavailable();

  const minimumRows = placements.reduce(
    (total, placement) => total + (minimumSizes.get(placement.instance_id)?.minH ?? 3), 0,
  );
  if (minimumRows > MAX_LAYOUT_ROWS) return unavailable();
  const validGeometry = (placement: DashboardPlacement) => {
    const minimum = minimumSizes.get(placement.instance_id);
    return minimum !== undefined && rectWithinBounds(placement, minimum, columns);
  };
  const groupsById = new Map(instances.map((instance) => [instance.id, instance.group_id]));
  const noOverlap = placements.every((placement, index) =>
    placements.slice(index + 1).every((other) => groupsById.get(other.instance_id) !== groupsById.get(placement.instance_id)
      || !doPlacementsOverlap(placement, other)),
  );
  if (placements.every(validGeometry) && noOverlap) return { placements, unavailable: false };

  // Each panel owns its own coordinate plane; repair only collisions inside that panel.
  const repaired = [...new Set(instances.map((instance) => instance.group_id))].flatMap((groupId) =>
    repairAll(placements.filter((placement) => groupsById.get(placement.instance_id) === groupId), minimumSizes, columns) ?? []);
  if (repaired.length !== placements.length) return unavailable();
  return {
    placements: placements.map((placement) => repaired.find((item) => item.instance_id === placement.instance_id)!),
    unavailable: false,
  };
}
