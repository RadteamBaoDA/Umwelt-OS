import { jsonCatalogs } from '@locales';

/**
 * The application message catalogs, keyed by locale then next-intl namespace (`messages['en-us'].chat`).
 *
 * Every namespace is authored as flat dotted JSON in `apps/web/locales/<locale>/<namespace>.json` and expanded to the
 * nested tree next-intl consumes by `unflatten` in `@locales`; this module is only the stable import path for the
 * ~40 callers (root layout, request config, lazy Vietnamese chunk, tests). Add a namespace by creating the two JSON files
 * and regenerating the `<gen:catalogs>` region of `locales/index.ts`; nothing needs editing here.
 */
export const messages = jsonCatalogs;
