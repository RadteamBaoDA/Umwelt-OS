import { describe, expect, it } from 'vitest';
import { flatCatalogs } from '@locales';

/**
 * Regression for the semantic-index status line in `modules/search/search-page.tsx`. Those keys are read through a
 * `translate(...)` cast, which the static key-usage gate does not see, so they are pinned here in both catalogs.
 */
const INDEX_STATUS_KEYS = [
  'indexQueued',
  'indexRunning',
  'indexActive',
  'indexFailed',
  'indexUnavailableStatus',
  'indexUnknownStatus',
  'indexPreparing',
] as const;

describe('search index status keys in entities catalog', () => {
  it.each(['en-us', 'vi-vi'] as const)('%s defines every index status key', (locale) => {
    const entities = flatCatalogs[locale].entities as Record<string, string>;
    for (const key of INDEX_STATUS_KEYS) {
      expect(entities[key], key).toEqual(expect.any(String));
      expect(entities[key].length, key).toBeGreaterThan(0);
    }
  });
});
