import { describe, expect, it } from 'vitest';
import { getCatalog } from '@locales';
import { errorCodeLabel, KNOWN_ERROR_CODES, type ErrorCodeTranslator } from './error-codes';

const viCatalog = getCatalog('vi-vi', 'errorCodes') as Record<string, string>;
const fake: ErrorCodeTranslator = (k) => viCatalog[k];

describe('errorCodeLabel', () => {
  it('uses the catalog for known codes and humanizes unknown ones', () => {
    expect(errorCodeLabel(fake, 'timeout')).toBe('Hết thời gian chờ');
    expect(errorCodeLabel(fake, 'weird_new-code')).toBe('Weird new code');
  });
  it('lists exactly the catalog keys, in both locales', () => {
    expect([...KNOWN_ERROR_CODES].sort()).toEqual(Object.keys(getCatalog('en-us', 'errorCodes')).sort());
    expect(Object.keys(viCatalog).sort()).toEqual([...KNOWN_ERROR_CODES].sort());
  });
});
