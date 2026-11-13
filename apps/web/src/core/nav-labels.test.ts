import { describe, expect, it } from 'vitest';
import { getCatalog } from '@messages';
import { commandDestinations, settingsExtraLinks, sourcesSubNavigation } from './module-registry';

/** Walks a dotted message key (`group.key`) through a nested catalog; undefined when any segment is missing. */
function resolveKey(tree: unknown, key: string): unknown {
  return key
    .split('.')
    .reduce<unknown>((node, part) => (node && typeof node === 'object' ? (node as Record<string, unknown>)[part] : undefined), tree);
}

describe('navigation labels', () => {
  it.each(['en-us', 'vi-vi'] as const)('every destination has a %s shell label', (locale) => {
    const messages = getCatalog(locale, 'shell');
    for (const d of [...commandDestinations, ...settingsExtraLinks, ...sourcesSubNavigation])
      expect(resolveKey(messages, d.messageKey), d.id).toBeTruthy();
  });
});
