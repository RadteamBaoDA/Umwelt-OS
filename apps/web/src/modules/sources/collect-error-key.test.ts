import { describe, expect, it } from 'vitest';
import { sourcesMessages } from '@/core/messages/sources';
import { collectErrorKey } from './builtin-sources';

describe('collectErrorKey', () => {
  it('maps known run error codes to collectError_<code>', () => {
    expect(collectErrorKey('provider_rate_limited')).toBe('collectError_provider_rate_limited');
    expect(collectErrorKey('cancelled')).toBe('collectError_cancelled');
    for (const code of ['provider_timeout', 'provider_unavailable', 'provider_response_invalid', 'schema_changed', 'invalid_credential', 'credential_missing', 'collection_incomplete', 'destination_blocked']) {
      expect(collectErrorKey(code)).toBe(`collectError_${code}`);
    }
  });

  it('returns null for unknown, empty and missing codes', () => {
    expect(collectErrorKey('something_new')).toBeNull();
    expect(collectErrorKey('')).toBeNull();
    expect(collectErrorKey(null)).toBeNull();
    expect(collectErrorKey(undefined)).toBeNull();
  });

  it('has an en-us and vi-vi message for every mapped code', () => {
    const codes = Object.keys(sourcesMessages['en-us']).filter((key) => key.startsWith('collectError_'));
    expect(codes.length).toBeGreaterThanOrEqual(21);
    for (const key of codes) {
      expect(collectErrorKey(key.slice('collectError_'.length))).toBe(key);
      expect((sourcesMessages['vi-vi'] as unknown as Record<string, string>)[key]).toBeTruthy();
    }
  });
});
