import { beforeEach, describe, expect, it } from 'vitest';
import { ACCENT_STORAGE_KEY, readAccent, syncServerAccent } from './accent';

describe('syncServerAccent', () => {
  beforeEach(() => { window.localStorage.clear(); document.documentElement.removeAttribute('data-accent'); });

  it('applies the server accent and updates localStorage when it differs (server wins)', () => {
    window.localStorage.setItem(ACCENT_STORAGE_KEY, 'moss');
    expect(syncServerAccent('plum')).toBe(true);
    expect(readAccent()).toBe('plum');
    expect(document.documentElement.getAttribute('data-accent')).toBe('plum');
  });
  it('does nothing when equal, null, or unknown', () => {
    window.localStorage.setItem(ACCENT_STORAGE_KEY, 'sea');
    expect(syncServerAccent('sea')).toBe(false);
    expect(syncServerAccent(null)).toBe(false);
    expect(syncServerAccent('neon')).toBe(false);
    expect(readAccent()).toBe('sea');
  });
});
