'use client';

import React from 'react';
import type { GadgetInstance } from './api';
import type { DashboardPlacement } from './api';
import dynamic from 'next/dynamic';

/** Standard props supplied to every resolved gadget renderer. */
export interface GadgetRendererProps {
  /** The gadget instance with its configuration projection. */
  instance: GadgetInstance;
  /** Whether the parent grid is currently in layout edit mode. */
  isEditMode: boolean;
}

/** Loading placeholder shown while a gadget chunk loads; mirrors the legacy `.skeleton` surface. */
function GadgetChunkSkeleton(): React.ReactElement {
  return <div className="skeleton" aria-hidden="true" />;
}

/** Component type produced by `next/dynamic` for a gadget that accepts an instance (and maybe edit mode). */
type LazyGadget = React.ComponentType<{ instance: GadgetInstance; isEditMode?: boolean }>;

/**
 * Adapts a `next/dynamic` gadget component to GadgetRendererProps, forwarding only the props the gadget
 * accepts. Chunks are split per gadget type; SSR stays enabled so markup and hydration are unchanged.
 *
 * @param Lazy The dynamically imported gadget component.
 * @param forwardEditMode Whether the gadget also receives `isEditMode` (only the map gadget does).
 * @returns A renderer component matching GadgetRendererProps.
 */
function adaptGadget(Lazy: LazyGadget, forwardEditMode = false): React.ComponentType<GadgetRendererProps> {
  return function LazyGadgetRenderer(props: GadgetRendererProps) {
    return forwardEditMode
      ? <Lazy instance={props.instance} isEditMode={props.isEditMode} />
      : <Lazy instance={props.instance} />;
  };
}

/**
 * Registry mapping each renderer ID to a lazily loaded component. A new gadget type is added by adding
 * one entry here; no other code changes (open/closed). `dynamic()` calls stay inline so Next's compiler
 * can register each chunk for server rendering.
 */
const GADGET_REGISTRY: Record<string, React.ComponentType<GadgetRendererProps>> = {
  daily_brief: adaptGadget(dynamic(() => import('./gadgets/brief-gadget').then((m) => m.BriefGadget), { loading: GadgetChunkSkeleton })),
  tasks: adaptGadget(dynamic(() => import('./gadgets/tasks-gadget').then((m) => m.TasksGadget), { loading: GadgetChunkSkeleton })),
  goals: adaptGadget(dynamic(() => import('./gadgets/goals-gadget').then((m) => m.GoalsGadget), { loading: GadgetChunkSkeleton })),
  timeline: adaptGadget(dynamic(() => import('./gadgets/timeline-gadget').then((m) => m.TimelineGadget), { loading: GadgetChunkSkeleton })),
  entity: adaptGadget(dynamic(() => import('./gadgets/entity-gadget').then((m) => m.EntityGadget), { loading: GadgetChunkSkeleton })),
  personal_context: adaptGadget(dynamic(() => import('./gadgets/personal-context').then((m) => m.PersonalContext), { loading: GadgetChunkSkeleton })),
  news_feed: adaptGadget(dynamic(() => import('./gadgets/news-feed').then((m) => m.NewsFeed), { loading: GadgetChunkSkeleton })),
  telegram_feed: adaptGadget(dynamic(() => import('./gadgets/telegram-feed').then((m) => m.TelegramFeed), { loading: GadgetChunkSkeleton })),
  feed: adaptGadget(dynamic(() => import('./gadgets/feed-gadget').then((m) => m.FeedGadget), { loading: GadgetChunkSkeleton })),
  text_panel: adaptGadget(dynamic(() => import('./gadgets/text-panel').then((m) => m.TextPanel), { loading: GadgetChunkSkeleton })),
  table_panel: adaptGadget(dynamic(() => import('./gadgets/table-panel').then((m) => m.TablePanel), { loading: GadgetChunkSkeleton })),
  job_list: adaptGadget(dynamic(() => import('./gadgets/job-list').then((m) => m.JobListGadget), { loading: GadgetChunkSkeleton })),
  video_panel: adaptGadget(dynamic(() => import('./gadgets/video-panel').then((m) => m.VideoPanel), { loading: GadgetChunkSkeleton })),
  watch_rules: adaptGadget(dynamic(() => import('./gadgets/watchlist-gadget').then((m) => m.WatchlistGadget), { loading: GadgetChunkSkeleton })),
  highlights: adaptGadget(dynamic(() => import('./gadgets/watchlist-gadget').then((m) => m.WatchlistGadget), { loading: GadgetChunkSkeleton })),
  finance_chart: adaptGadget(dynamic(() => import('./gadgets/finance-chart').then((m) => m.FinanceChart), { loading: GadgetChunkSkeleton })),
  metrics_chart: adaptGadget(dynamic(() => import('./gadgets/metrics-chart').then((m) => m.MetricsChart), { loading: GadgetChunkSkeleton })),
  github_project: adaptGadget(dynamic(() => import('./gadgets/github-project-gadget').then((m) => m.GithubProjectGadget), { loading: GadgetChunkSkeleton })),
  weather: adaptGadget(dynamic(() => import('./gadgets/weather-panel').then((m) => m.WeatherPanel), { loading: GadgetChunkSkeleton })),
  provider_observation: adaptGadget(dynamic(() => import('./gadgets/provider-observation-gadget').then((m) => m.ProviderObservationGadget), { loading: GadgetChunkSkeleton })),
  map: adaptGadget(dynamic(() => import('./gadgets/map-gadget').then((m) => m.MapGadget), { loading: GadgetChunkSkeleton }), true),
  intelligence_panel: adaptGadget(dynamic(() => import('./gadgets/intelligence-panel').then((m) => m.IntelligencePanel), { loading: GadgetChunkSkeleton })),
};

/**
 * Readable body floors include bounded renderer chrome and the fixed plot/loading surfaces found
 * in each source renderer. They are local reading policy, not persisted/server minimum dimensions.
 */
export const GADGET_READING_BODY_FLOORS: Record<string, number> = {
  // The common floor matches the legacy `.skeleton { min-height: 180px }` surface in app/globals.css; DailyBrief adds its date row and two-action row.
  daily_brief: 240, tasks: 180, goals: 180,
  // TimelineGadget: 180px readable content baseline plus root p-3, two gaps, and fixed header.
  timeline: 264,
  // Entity/PersonalContext: 180px content baseline plus root padding, search header, and spacing.
  entity: 272, personal_context: 272,
  // FeedGadget: three h-16 skeletons plus loading gaps/padding, root p-3 and fixed action-bar chrome.
  news_feed: 280, telegram_feed: 280, feed: 280,
  // Fixed renderer roots reserve the common baseline plus bounded controls and outer padding.
  text_panel: 180, table_panel: 204, job_list: 204, video_panel: 180, watch_rules: 204,
  highlights: 204,
  // MetricsChart: h-[140px] plot + p-3, two space-y-3 gaps, fixed chart header and range controls.
  finance_chart: 276, metrics_chart: 276,
  github_project: 204,
  // MapGadget reserves 12rem for its plot plus 8rem for its range header, notices, list and footer; extra notices scroll.
  weather: 204, provider_observation: 240, map: 352, intelligence_panel: 180,
};

/** Resolves source-backed floors, scaling MapGadget's bounded controls and plot with root text size. */
export function getGadgetReadingBodyFloor(rendererId: string): number {
  if (rendererId === 'map') {
    const rootFontSize = typeof document === 'undefined'
      ? 16
      : Number.parseFloat(getComputedStyle(document.documentElement).fontSize) || 16;
    return 20 * rootFontSize + 32;
  }
  return GADGET_READING_BODY_FLOORS[rendererId] ?? 180;
}

/** True when the column count and measured sizes are usable (finite, positive, columns within 1..20). */
function measurementsValid(columns: number, cellSize: number, stride: number, frameHeaderHeight: number): boolean {
  return Number.isInteger(columns) && columns >= 1 && columns <= 20
    && Number.isFinite(cellSize) && cellSize > 0 && Number.isFinite(stride) && stride > 0
    && Number.isFinite(frameHeaderHeight) && frameHeaderHeight > 0;
}

/** True when one placement is an in-bounds integer rectangle (x/w within the columns, y/h within the row bound). */
function placementInBounds(placement: DashboardPlacement, columns: number): boolean {
  return Number.isInteger(placement.x) && Number.isInteger(placement.y)
    && Number.isInteger(placement.w) && Number.isInteger(placement.h)
    && placement.x >= 0 && placement.y >= 0 && placement.w >= 1 && placement.h >= 1
    && placement.x + placement.w <= columns && placement.y + placement.h <= 100_000;
}

/** True when placements and instances correspond one-to-one and every placement is an in-bounds integer rectangle. */
function placementsMatchInstances(
  placements: DashboardPlacement[],
  instances: GadgetInstance[],
  byId: Map<string, GadgetInstance>,
  columns: number,
): boolean {
  return placements.length === instances.length && byId.size === instances.length
    && new Set(placements.map((placement) => placement.instance_id)).size === placements.length
    && placements.every((placement) => byId.has(placement.instance_id) && placementInBounds(placement, columns));
}

/** Orders finite coordinates first; ties fall through y, then x, then instance id (deterministic reading order). */
function compareReadingOrder(a: DashboardPlacement, b: DashboardPlacement): number {
  const compare = (left: number, right: number) =>
    Number.isFinite(left) && Number.isFinite(right) ? left - right : Number.isFinite(left) ? -1 : Number.isFinite(right) ? 1 : 0;
  return compare(a.y, b.y) || compare(a.x, b.x) || a.instance_id.localeCompare(b.instance_id);
}

/** True when two rectangles overlap on a half-open basis. */
function rectsOverlap(a: DashboardPlacement, b: DashboardPlacement): boolean {
  return a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h;
}

/**
 * Converts saved square units to a readable local presentation without changing width or x.
 * Geometry is already contract-recovered by the page; failure leaves that saved layout visible.
 */
export function projectReadableMobilePlacements(
  placements: DashboardPlacement[],
  instances: GadgetInstance[],
  columns: number,
  cellSize: number,
  frameHeaderHeight: number,
): DashboardPlacement[] | null {
  const stride = cellSize + 12;
  if (!measurementsValid(columns, cellSize, stride, frameHeaderHeight)) return null;
  const byId = new Map(instances.map((instance) => [instance.id, instance]));
  if (!placementsMatchInstances(placements, instances, byId, columns)) return null;
  const ordered = [...placements].sort(compareReadingOrder);
  const placed: DashboardPlacement[] = [];
  for (const source of ordered) {
    const renderer = byId.get(source.instance_id)?.definition.renderer ?? '';
    // The frame header comes from its measured, stable two-row chrome; include the card border.
    const floor = getGadgetReadingBodyFloor(renderer) + frameHeaderHeight + 2;
    const h = Math.max(source.h, Math.ceil((floor + 12) / stride));
    const candidate = { ...source, h };
    while (placed.some((other) => rectsOverlap(candidate, other))) {
      const collisions = placed.filter((other) => rectsOverlap(candidate, other));
      const nextY = Math.max(...collisions.map((other) => other.y + other.h));
      if (nextY <= candidate.y) return null;
      candidate.y = nextY;
    }
    if (candidate.y + candidate.h > 100_000) return null;
    placed.push(candidate);
  }
  const byPlacementId = new Map(placed.map((placement) => [placement.instance_id, placement]));
  return placements.map((placement) => byPlacementId.get(placement.instance_id)!);
}

/** Tablet reading grid: 8 square tracks form two 4-track columns. */
export const TABLET_GRID_COLUMNS = 8;
/** Desktop width (of 20 columns) from which a gadget spans both tablet columns. */
export const TABLET_FULL_WIDTH_FROM = 12;

/**
 * Builds a read-only two-column tablet flow from the desktop layout (never persisted).
 * Gadgets keep desktop reading order (y, then x, then id); each spans one tablet column, or both
 * when its desktop width is at least TABLET_FULL_WIDTH_FROM. Heights keep the desktop pixel height
 * converted to tablet units, then the shared readable-floor pass (the mobile projection) raises
 * them to each renderer's reading floor and resolves collisions.
 *
 * @param placements Complete desktop placements (20-column units).
 * @param instances Dashboard instances owning those placements.
 * @param desktopCellSize Desktop unit size in px at the current width.
 * @param tabletCellSize Tablet unit size in px at the current width.
 * @param frameHeaderHeight Measured gadget frame header height in px.
 * @returns Tablet placements in input order, or null when geometry is invalid or unmeasured.
 */
export function projectTabletPlacements(
  placements: DashboardPlacement[],
  instances: GadgetInstance[],
  desktopCellSize: number,
  tabletCellSize: number,
  frameHeaderHeight: number,
): DashboardPlacement[] | null {
  if (!(desktopCellSize > 0) || !(tabletCellSize > 0)) return null;
  const half = TABLET_GRID_COLUMNS / 2;
  const ordered = [...placements].sort((a, b) =>
    a.y - b.y || a.x - b.x || a.instance_id.localeCompare(b.instance_id));
  const colHeights = [0, 0];
  const flowed = ordered.map((source): DashboardPlacement => {
    const px = source.h * (desktopCellSize + 12) - 12;
    const h = Math.max(2, Math.ceil((px + 12) / (tabletCellSize + 12)));
    if (source.w >= TABLET_FULL_WIDTH_FROM) {
      const y = Math.max(colHeights[0], colHeights[1]);
      colHeights[0] = colHeights[1] = y + h;
      return { ...source, x: 0, y, w: TABLET_GRID_COLUMNS, h };
    }
    const col = colHeights[1] < colHeights[0] ? 1 : 0;
    const y = colHeights[col];
    colHeights[col] += h;
    return { ...source, x: col * half, y, w: half, h };
  });
  const projected = projectReadableMobilePlacements(
    flowed, instances, TABLET_GRID_COLUMNS, tabletCellSize, frameHeaderHeight);
  if (!projected) return null;
  const byId = new Map(projected.map((p) => [p.instance_id, p]));
  return placements.map((p) => byId.get(p.instance_id)!);
}

/**
 * Resolves the React component responsible for rendering a given gadget renderer ID.
 *
 * @param rendererId Canonical identifier of the renderer (e.g. 'daily_brief', 'finance_chart').
 * @returns Component constructor if registered, or null for fallback planned rendering.
 */
export function resolveGadgetRenderer(
  rendererId: string,
): React.ComponentType<GadgetRendererProps> | null {
  return GADGET_REGISTRY[rendererId] ?? null;
}

/**
 * Checks whether a renderer has an active interactive component implementation.
 *
 * @param rendererId Canonical identifier of the renderer.
 * @returns True if registered in the widget registry.
 */
export function isGadgetRendererRegistered(rendererId: string): boolean {
  return rendererId in GADGET_REGISTRY;
}

/**
 * Renders the registered gadget for a renderer id, or nothing for an unregistered (planned) renderer.
 * Looking the component up inside a component of its own keeps each registry entry a stable, module-level
 * component type, so gadget state is never reset by the parent re-rendering.
 *
 * @param rendererId Canonical identifier of the renderer.
 * @param instance The gadget instance to render.
 * @param isEditMode Whether the parent grid is in layout edit mode.
 */
export function RegisteredGadget({ rendererId, instance, isEditMode }: GadgetRendererProps & { rendererId: string }): React.ReactElement | null {
  const Component = GADGET_REGISTRY[rendererId];
  return Component ? <Component instance={instance} isEditMode={isEditMode} /> : null;
}
