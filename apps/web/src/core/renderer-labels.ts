import type { useTranslations } from 'next-intl';

/** Generic template kind (`templateTypes.<kind>`) shown for each available renderer. */
export const TEMPLATE_KIND_BY_RENDERER: Record<string, string> = {
  news_feed: 'feed', telegram_feed: 'messageList', text_panel: 'text', table_panel: 'table', video_panel: 'video',
  finance_chart: 'chart', map: 'map', intelligence_panel: 'relatedSignals', highlights: 'highlights',
  watch_rules: 'watchlist', tasks: 'tasks', goals: 'goals', daily_brief: 'dailyBrief', weather: 'weather',
  provider_observation: 'metric', github_project: 'activity',
};

/**
 * Localized display name of a renderer id (`renderers.<id>`); unknown ids fall back to the raw id.
 *
 * @param t Translator scoped to the gadgetSettings namespace.
 * @param id Renderer id from the renderer catalog.
 */
export function rendererName(t: ReturnType<typeof useTranslations>, id: string): string {
  return t.has(`renderers.${id}`) ? t(`renderers.${id}` as 'name') : id;
}

/** Resolves the generic, localized template name for an available renderer (falls back to the renderer name). */
export function templateName(renderer: string, t: ReturnType<typeof useTranslations>): string {
  const kind = TEMPLATE_KIND_BY_RENDERER[renderer];
  return kind && t.has(`templateTypes.${kind}.name`) ? t(`templateTypes.${kind}.name` as 'name') : rendererName(t, renderer);
}
