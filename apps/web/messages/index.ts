import type { AppLocaleId } from '@/core/i18n';
import enChat from './en-us/chat.json';
import enCommon from './en-us/common.json';
import enShell from './en-us/shell.json';
import enTaskGoal from './en-us/taskGoal.json';
import viChat from './vi-vi/chat.json';
import viCommon from './vi-vi/common.json';
import viShell from './vi-vi/shell.json';
import viTaskGoal from './vi-vi/taskGoal.json';

/** JSON catalogs that are already migrated out of src/core/messages/*.ts, keyed by locale then top-level namespace. */
export const jsonCatalogs = {
  'en-us': {
    chat: enChat,
    common: enCommon,
    shell: enShell,
    taskGoal: enTaskGoal,
  },
  'vi-vi': {
    chat: viChat,
    common: viCommon,
    shell: viShell,
    taskGoal: viTaskGoal,
  },
} as const;

/** Namespaces served from JSON. */
export type JsonNamespace = keyof (typeof jsonCatalogs)['en-us'];

/**
 * Returns the message tree of one JSON-backed namespace.
 * @param locale Catalog locale id.
 * @param ns Top-level namespace (file name under messages/<locale>/).
 * @returns The nested feature -> key tree; typed from the en-us file.
 */
export function getCatalog<N extends JsonNamespace>(locale: AppLocaleId, ns: N): (typeof jsonCatalogs)['en-us'][N] {
  return jsonCatalogs[locale][ns] as (typeof jsonCatalogs)['en-us'][N];
}
