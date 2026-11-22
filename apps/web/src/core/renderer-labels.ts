import type { useTranslations } from 'next-intl';

/** Generic template kind (`templateTypes.<kind>`) shown for each available renderer. */
export const TEMPLATE_KIND_BY_RENDERER: Record<string, string> = {
  media_feed: 'media_shelf',
  media_calendar: 'release_calendar',
  news_feed: 'feed',
  telegram_feed: 'messageList',
  text_panel: 'text',
  table_panel: 'table',
  job_list: 'jobs',
  video_panel: 'video',
  finance_chart: 'chart',
  map: 'map',
  intelligence_panel: 'relatedSignals',
  highlights: 'highlights',
  watch_rules: 'watchlist',
  tasks: 'tasks',
  goals: 'goals',
  daily_brief: 'dailyBrief',
  weather: 'weather',
  provider_observation: 'metric',
  github_project: 'activity',
};

/**
 * Literal `gadgetSettings` catalog keys per renderer id / template kind, so no key is built by string concatenation.
 * Ids outside a table (new renderers) fall back to the raw id or an empty description.
 */
const RENDERER_NAME_KEY = {
  media_feed: 'renderers.media_feed',
  media_calendar: 'renderers.media_calendar',
  news_feed: 'renderers.news_feed',
  telegram_feed: 'renderers.telegram_feed',
  text_panel: 'renderers.text_panel',
  table_panel: 'renderers.table_panel',
  job_list: 'renderers.job_list',
  video_panel: 'renderers.video_panel',
  finance_chart: 'renderers.finance_chart',
  personal_context: 'renderers.personal_context',
  map: 'renderers.map',
  intelligence_panel: 'renderers.intelligence_panel',
  highlights: 'renderers.highlights',
  watch_rules: 'renderers.watch_rules',
  tasks: 'renderers.tasks',
  goals: 'renderers.goals',
  daily_brief: 'renderers.daily_brief',
  weather: 'renderers.weather',
  provider_observation: 'renderers.provider_observation',
  research: 'renderers.research',
  github_project: 'renderers.github_project',
} as const;
const TEMPLATE_NAME_KEY = {
  media_shelf: 'templateTypes.media_shelf.name',
  release_calendar: 'templateTypes.release_calendar.name',
  feed: 'templateTypes.feed.name',
  messageList: 'templateTypes.messageList.name',
  text: 'templateTypes.text.name',
  table: 'templateTypes.table.name',
  video: 'templateTypes.video.name',
  jobs: 'templateTypes.jobs.name',
  chart: 'templateTypes.chart.name',
  map: 'templateTypes.map.name',
  relatedSignals: 'templateTypes.relatedSignals.name',
  highlights: 'templateTypes.highlights.name',
  watchlist: 'templateTypes.watchlist.name',
  tasks: 'templateTypes.tasks.name',
  goals: 'templateTypes.goals.name',
  dailyBrief: 'templateTypes.dailyBrief.name',
  weather: 'templateTypes.weather.name',
  metric: 'templateTypes.metric.name',
  activity: 'templateTypes.activity.name',
} as const;
const TEMPLATE_DESCRIPTION_KEY = {
  media_shelf: 'templateTypes.media_shelf.description',
  release_calendar: 'templateTypes.release_calendar.description',
  feed: 'templateTypes.feed.description',
  messageList: 'templateTypes.messageList.description',
  text: 'templateTypes.text.description',
  table: 'templateTypes.table.description',
  video: 'templateTypes.video.description',
  jobs: 'templateTypes.jobs.description',
  chart: 'templateTypes.chart.description',
  map: 'templateTypes.map.description',
  relatedSignals: 'templateTypes.relatedSignals.description',
  highlights: 'templateTypes.highlights.description',
  watchlist: 'templateTypes.watchlist.description',
  tasks: 'templateTypes.tasks.description',
  goals: 'templateTypes.goals.description',
  dailyBrief: 'templateTypes.dailyBrief.description',
  weather: 'templateTypes.weather.description',
  metric: 'templateTypes.metric.description',
  activity: 'templateTypes.activity.description',
} as const;

/**
 * Localized display name of a renderer id (`renderers.<id>`); unknown ids fall back to the raw id.
 *
 * @param t Translator scoped to the gadgetSettings namespace.
 * @param id Renderer id from the renderer catalog.
 */
export function rendererName(t: ReturnType<typeof useTranslations<'gadgetSettings'>>, id: string): string {
  return id in RENDERER_NAME_KEY ? t(RENDERER_NAME_KEY[id as keyof typeof RENDERER_NAME_KEY]) : id;
}

/** Resolves the generic, localized template name for an available renderer (falls back to the renderer name). */
export function templateName(renderer: string, t: ReturnType<typeof useTranslations<'gadgetSettings'>>): string {
  const kind = TEMPLATE_KIND_BY_RENDERER[renderer];
  return kind && kind in TEMPLATE_NAME_KEY ? t(TEMPLATE_NAME_KEY[kind as keyof typeof TEMPLATE_NAME_KEY]) : rendererName(t, renderer);
}

/** Resolves the short localized template description for a renderer, or '' when its kind has none. */
export function templateDescription(renderer: string, t: ReturnType<typeof useTranslations<'gadgetSettings'>>): string {
  const kind = TEMPLATE_KIND_BY_RENDERER[renderer];
  return kind && kind in TEMPLATE_DESCRIPTION_KEY ? t(TEMPLATE_DESCRIPTION_KEY[kind as keyof typeof TEMPLATE_DESCRIPTION_KEY]) : '';
}
