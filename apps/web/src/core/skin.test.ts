import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { applySkin, readSkin, SKIN_INIT_SCRIPT, SKIN_STORAGE_KEY } from './skin';

describe('skin storage helpers', () => {
  beforeEach(() => {
    localStorage.clear();
    document.documentElement.removeAttribute('data-skin');
  });
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('applySkin terminal sets data-skin and persists it, readSkin reads it back', () => {
    applySkin('terminal');
    expect(document.documentElement.getAttribute('data-skin')).toBe('terminal');
    expect(localStorage.getItem(SKIN_STORAGE_KEY)).toBe('terminal');
    expect(readSkin()).toBe('terminal');
  });

  it('applySkin default removes the attribute and the stored key', () => {
    applySkin('terminal');
    applySkin('default');
    expect(document.documentElement.hasAttribute('data-skin')).toBe(false);
    expect(localStorage.getItem(SKIN_STORAGE_KEY)).toBeNull();
    expect(readSkin()).toBe('default');
  });

  it('still applies the skin when storage throws', () => {
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('blocked');
    });
    vi.spyOn(Storage.prototype, 'removeItem').mockImplementation(() => {
      throw new Error('blocked');
    });
    expect(() => applySkin('terminal')).not.toThrow();
    expect(document.documentElement.getAttribute('data-skin')).toBe('terminal');
    expect(() => applySkin('default')).not.toThrow();
    expect(document.documentElement.hasAttribute('data-skin')).toBe(false);
  });
});

describe('SKIN_INIT_SCRIPT', () => {
  beforeEach(() => {
    localStorage.clear();
    document.documentElement.removeAttribute('data-skin');
  });

  it('sets data-skin before paint only when the stored skin is terminal', () => {
    localStorage.setItem(SKIN_STORAGE_KEY, 'terminal');
    new Function(SKIN_INIT_SCRIPT)();
    expect(document.documentElement.getAttribute('data-skin')).toBe('terminal');
  });

  it('leaves the default skin untouched for unknown or missing values', () => {
    localStorage.setItem(SKIN_STORAGE_KEY, 'neon');
    new Function(SKIN_INIT_SCRIPT)();
    expect(document.documentElement.hasAttribute('data-skin')).toBe(false);
  });
});
