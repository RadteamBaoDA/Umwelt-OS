import { describe, expect, it } from 'vitest';
import { getCatalog } from '@locales';
import { collectErrorKey } from './builtin-sources';

describe('collectErrorKey', () => {
  it('maps known run error codes to collectError.<code>', () => {
    expect(collectErrorKey('provider_rate_limited')).toBe('collectError.providerRateLimited');
    expect(collectErrorKey('cancelled')).toBe('collectError.cancelled');
    expect(collectErrorKey('provider_timeout')).toBe('collectError.providerTimeout');
    expect(collectErrorKey('provider_unavailable')).toBe('collectError.providerUnavailable');
    expect(collectErrorKey('provider_response_invalid')).toBe('collectError.providerResponseInvalid');
    expect(collectErrorKey('schema_changed')).toBe('collectError.schemaChanged');
    expect(collectErrorKey('invalid_credential')).toBe('collectError.invalidCredential');
    expect(collectErrorKey('credential_missing')).toBe('collectError.credentialMissing');
    expect(collectErrorKey('collection_incomplete')).toBe('collectError.collectionIncomplete');
    expect(collectErrorKey('destination_blocked')).toBe('collectError.destinationBlocked');
  });

  it('returns null for unknown, empty and missing codes', () => {
    expect(collectErrorKey('something_new')).toBeNull();
    expect(collectErrorKey('')).toBeNull();
    expect(collectErrorKey(null)).toBeNull();
    expect(collectErrorKey(undefined)).toBeNull();
  });

  it('has an en-us and vi-vi message for every mapped code', () => {
    const en = getCatalog('en-us', 'sources').collectError;
    const vi = getCatalog('vi-vi', 'sources').collectError;
    const names = Object.keys(en);
    expect(names.length).toBeGreaterThanOrEqual(21);
    for (const name of names) {
      expect(vi[name as keyof typeof vi]).toBeTruthy();
      expect(en[name as keyof typeof en]).toBeTruthy();
    }
  });
});
