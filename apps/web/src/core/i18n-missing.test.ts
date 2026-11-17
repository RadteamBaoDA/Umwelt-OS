import { afterEach, describe, expect, it, vi } from 'vitest';
import { missingKeyHandlers } from './i18n-missing';

describe('missingKeyHandlers', () => {
  afterEach(() => vi.unstubAllEnvs());

  it('is inert when the detector is off', () => {
    expect(missingKeyHandlers()).toEqual({});
    const h = missingKeyHandlers((ns, key) => `en:${ns}.${key}`);
    expect(h.onError).toBeUndefined();
    expect(h.getMessageFallback?.({ namespace: 'chat', key: 'x', error: null })).toBe('en:chat.x');
  });

  it('logs the marker and keeps the fallback text when enabled', () => {
    vi.stubEnv('NEXT_PUBLIC_E2E_FAIL_ON_MISSING_I18N', '1');
    const spy = vi.spyOn(console, 'error').mockImplementation(() => undefined);
    const h = missingKeyHandlers();
    expect(h.getMessageFallback?.({ namespace: 'chat', key: 'send', error: null })).toBe('chat.send');
    expect(spy).toHaveBeenCalledWith('[i18n-missing] chat.send');
    spy.mockRestore();
  });
});
