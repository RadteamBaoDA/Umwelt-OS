import { describe, expect, it } from 'vitest';
import { errorCodeLabel, type ErrorCodeTranslator } from './error-codes';
import { errorCodesMessages } from './messages/error-codes';

const fake = ((k: string) => (errorCodesMessages['vi-vi'] as Record<string, string>)[k]) as ErrorCodeTranslator;
fake.has = (k) => k in errorCodesMessages['vi-vi'];

describe('errorCodeLabel', () => {
  it('uses the catalog for known codes and humanizes unknown ones', () => {
    expect(errorCodeLabel(fake, 'timeout')).toBe('Hết thời gian chờ');
    expect(errorCodeLabel(fake, 'weird_new-code')).toBe('Weird new code');
  });
  it('keeps en and vi keys in sync', () => {
    expect(Object.keys(errorCodesMessages['vi-vi']).sort()).toEqual(Object.keys(errorCodesMessages['en-us']).sort());
  });
});
