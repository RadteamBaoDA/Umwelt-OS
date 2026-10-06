'use client';

import React from 'react';
import type { GadgetInstance } from './api';
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
