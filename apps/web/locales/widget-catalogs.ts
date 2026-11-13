import type { AppLocaleId } from '@/core/i18n';
import enChat from './en-us/chat.json';
import enDashboard from './en-us/dashboard.json';
import enUiSweep from './en-us/uiSweep.json';
import viChat from './vi-vi/chat.json';
import viDashboard from './vi-vi/dashboard.json';
import viUiSweep from './vi-vi/uiSweep.json';
import { unflatten } from './unflatten';

/**
 * Nested catalogs for the namespaces the chat generative-UI widgets read directly (outside a next-intl provider).
 * Deliberately separate from `index.ts`: importing the full catalog set from chat code adds every namespace for both
 * locales (about 160 KB gzip) to the chat route's first-load JS. Keep this list to what widgets actually use.
 */
const widgetCatalogs = {
  'en-us': { chat: unflatten(enChat), dashboard: unflatten(enDashboard), uiSweep: unflatten(enUiSweep) },
  'vi-vi': { chat: unflatten(viChat), dashboard: unflatten(viDashboard), uiSweep: unflatten(viUiSweep) },
};

/**
 * Returns one widget namespace's nested message tree.
 * @param locale Catalog locale id.
 * @param ns One of `chat`, `dashboard`, `uiSweep`.
 * @returns The nested tree, typed from the en-us file (identical to `getCatalog` in `index.ts`).
 */
export function getWidgetCatalog<N extends keyof (typeof widgetCatalogs)['en-us']>(
  locale: AppLocaleId,
  ns: N,
): (typeof widgetCatalogs)['en-us'][N] {
  return widgetCatalogs[locale][ns];
}
