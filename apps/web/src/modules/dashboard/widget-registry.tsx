'use client';

import React from 'react';
import type { GadgetInstance } from './api';
import type { DashboardPlacement } from './api';
import { BriefGadget } from './gadgets/brief-gadget';
import { EntityGadget } from './gadgets/entity-gadget';
import { FeedGadget } from './gadgets/feed-gadget';
import { FinanceChart } from './gadgets/finance-chart';
import { GithubProjectGadget } from './gadgets/github-project-gadget';
import { IntelligencePanel } from './gadgets/intelligence-panel';
import { MapGadget } from './gadgets/map-gadget';
import { GoalsGadget } from './gadgets/goals-gadget';
import { MetricsChart } from './gadgets/metrics-chart';
import { NewsFeed } from './gadgets/news-feed';
import { PersonalContext } from './gadgets/personal-context';
import { TablePanel } from './gadgets/table-panel';
import { TasksGadget } from './gadgets/tasks-gadget';
import { TelegramFeed } from './gadgets/telegram-feed';
import { TextPanel } from './gadgets/text-panel';
import { TimelineGadget } from './gadgets/timeline-gadget';
import { VideoPanel } from './gadgets/video-panel';
import { WeatherPanel } from './gadgets/weather-panel';
import { WatchlistGadget } from './gadgets/watchlist-gadget';

/** Standard props supplied to every resolved gadget renderer. */
export interface GadgetRendererProps {
  /** The gadget instance with its configuration projection. */
  instance: GadgetInstance;
  /** Whether the parent grid is currently in layout edit mode. */
  isEditMode: boolean;
}

/** Static registry mapping each renderer ID to its component implementation. */
const GADGET_REGISTRY: Record<string, React.ComponentType<GadgetRendererProps>> = {
  daily_brief: (props) => <BriefGadget instance={props.instance} />,
  tasks: (props) => <TasksGadget instance={props.instance} />,
  goals: (props) => <GoalsGadget instance={props.instance} />,
  timeline: (props) => <TimelineGadget instance={props.instance} />,
  entity: (props) => <EntityGadget instance={props.instance} />,
  personal_context: (props) => <PersonalContext instance={props.instance} />,
  news_feed: (props) => <NewsFeed instance={props.instance} />,
  telegram_feed: (props) => <TelegramFeed instance={props.instance} />,
  feed: (props) => <FeedGadget instance={props.instance} />,
  text_panel: (props) => <TextPanel instance={props.instance} />,
  table_panel: (props) => <TablePanel instance={props.instance} />,
  video_panel: (props) => <VideoPanel instance={props.instance} />,
  watch_rules: (props) => <WatchlistGadget instance={props.instance} />,
  highlights: (props) => <WatchlistGadget instance={props.instance} />,
  finance_chart: (props) => <FinanceChart instance={props.instance} />,
  metrics_chart: (props) => <MetricsChart instance={props.instance} />,
  github_project: (props) => <GithubProjectGadget instance={props.instance} />,
  weather: (props) => <WeatherPanel instance={props.instance} />,
  map: (props) => <MapGadget instance={props.instance} isEditMode={props.isEditMode} />,
  intelligence_panel: (props) => <IntelligencePanel instance={props.instance} />,
};

/**
 * Readable body floors include bounded renderer chrome and the fixed plot/loading surfaces found
 * in each source renderer. They are local reading policy, not persisted/server minimum dimensions.
 */
export const GADGET_READING_BODY_FLOORS: Record<string, number> = {
  // The common floor matches the legacy `.skeleton { min-height: 180px }` surface in app/globals.css.
  daily_brief: 180, tasks: 180, goals: 180,
  // TimelineGadget: 180px readable content baseline plus root p-3, two gaps, and fixed header.
  timeline: 264,
  // Entity/PersonalContext: 180px content baseline plus root padding, search header, and spacing.
  entity: 272, personal_context: 272,
  // FeedGadget: three h-16 skeletons plus loading gaps/padding, root p-3 and fixed action-bar chrome.
  news_feed: 280, telegram_feed: 280, feed: 280,
  // Fixed renderer roots reserve the common baseline plus bounded controls and outer padding.
  text_panel: 180, table_panel: 204, video_panel: 180, watch_rules: 204,
  highlights: 204,
  // MetricsChart: h-[140px] plot + p-3, two space-y-3 gaps, fixed chart header and range controls.
  finance_chart: 276, metrics_chart: 276,
  github_project: 204,
  // MapGadget uses a root-font-scaled 12rem plot and bounded header, status, evidence, and attribution chrome.
  weather: 204, map: 368, intelligence_panel: 180,
};

/** Resolves an explicit bounded reading floor; unknown renderers use the common body baseline. */
export function getGadgetReadingBodyFloor(rendererId: string): number {
  if (rendererId === 'map') {
    const rootFontSize = typeof document === 'undefined'
      ? 16
      : Number.parseFloat(getComputedStyle(document.documentElement).fontSize) || 16;
    return 12 * rootFontSize + 176;
  }
  return GADGET_READING_BODY_FLOORS[rendererId] ?? 180;
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
  if (!Number.isInteger(columns) || columns < 1 || columns > 20
    || !Number.isFinite(cellSize) || cellSize <= 0 || !Number.isFinite(stride) || stride <= 0
    || !Number.isFinite(frameHeaderHeight) || frameHeaderHeight <= 0) return null;
  const byId = new Map(instances.map((instance) => [instance.id, instance]));
  if (placements.length !== instances.length || byId.size !== instances.length
    || new Set(placements.map((placement) => placement.instance_id)).size !== placements.length
    || placements.some((placement) => !byId.has(placement.instance_id)
      || !Number.isInteger(placement.x) || !Number.isInteger(placement.y)
      || !Number.isInteger(placement.w) || !Number.isInteger(placement.h)
      || placement.x < 0 || placement.y < 0 || placement.w < 1 || placement.h < 1
      || placement.x + placement.w > columns || placement.y + placement.h > 100_000)) return null;
  const ordered = [...placements].sort((a, b) =>
    (Number.isFinite(a.y) && Number.isFinite(b.y) ? a.y - b.y : Number.isFinite(a.y) ? -1 : Number.isFinite(b.y) ? 1 : 0)
    || (Number.isFinite(a.x) && Number.isFinite(b.x) ? a.x - b.x : Number.isFinite(a.x) ? -1 : Number.isFinite(b.x) ? 1 : 0)
    || a.instance_id.localeCompare(b.instance_id),
  );
  const placed: DashboardPlacement[] = [];
  for (const source of ordered) {
    const renderer = byId.get(source.instance_id)?.definition.renderer ?? '';
    // The frame header comes from its measured, stable two-row chrome; include the card border.
    const floor = getGadgetReadingBodyFloor(renderer) + frameHeaderHeight + 2;
    const h = Math.max(source.h, Math.ceil((floor + 12) / stride));
    const candidate = { ...source, h };
    while (placed.some((other) => candidate.x < other.x + other.w
      && other.x < candidate.x + candidate.w
      && candidate.y < other.y + other.h
      && other.y < candidate.y + candidate.h)) {
      const collisions = placed.filter((other) => candidate.x < other.x + other.w
        && other.x < candidate.x + candidate.w
        && candidate.y < other.y + other.h
        && other.y < candidate.y + candidate.h);
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
