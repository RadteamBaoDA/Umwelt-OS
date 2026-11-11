import { describe, expect, it } from 'vitest';
import { normalizeCitation } from './api';
import { citationIdentity } from './citation-panel';

describe('normalizeCitation', () => {
  it('maps server snake_case document citations so each keeps a distinct identity', () => {
    const raw = (n: number) =>
      ({
        source_type: 'document',
        source_id: 's',
        document_id: `d${n}`,
        document_version_id: `v${n}`,
        chunk_id: `c${n}`,
        title: 't',
        url: null,
        observed_at: null,
        quote: 'q',
      }) as never;
    const keys = [1, 2, 3].map((n) => citationIdentity(normalizeCitation(raw(n))));
    expect(new Set(keys).size).toBe(3);
    expect(keys[0]).toBe('v1:c1');
  });

  it('passes web and camelCase citations through unchanged', () => {
    const web = { sourceType: 'web', url: 'https://a.test', title: 't', quote: '' } as never;
    const doc = { documentVersionId: 'v', chunkId: 'c' } as never;
    expect(normalizeCitation(web)).toBe(web);
    expect(normalizeCitation(doc)).toBe(doc);
  });
});
