import { afterEach, describe, expect, it, vi } from 'vitest';
import { announceAccountErasure, clearAccountBrowserStorage, subscribeToAccountErasure } from './account-erasure';

afterEach(() => { localStorage.clear(); sessionStorage.clear(); vi.unstubAllGlobals(); });

describe('account browser erasure', () => {
  it('removes app preferences and private drafts without removing unrelated origin data', () => {
    localStorage.setItem('theme', 'dark');
    localStorage.setItem('umwelt:private', 'private');
    localStorage.setItem('umwelt.genui.templates.v2:1', 'private template');
    localStorage.setItem('umwelt.genui.templates.v1', 'legacy private template');
    sessionStorage.setItem('bbd:settings:ai:draft:v2', 'private');
    sessionStorage.setItem('umwelt:workspace', 'private');
    sessionStorage.setItem('unrelated', 'keep');
    clearAccountBrowserStorage();
    expect(localStorage.length).toBe(0);
    expect(sessionStorage.getItem('bbd:settings:ai:draft:v2')).toBeNull();
    expect(sessionStorage.getItem('umwelt:workspace')).toBeNull();
    expect(sessionStorage.getItem('unrelated')).toBe('keep');
  });

  it('clears the current tab even if broadcasting and browser storage are unavailable', () => {
    vi.stubGlobal('BroadcastChannel', undefined);
    const erased = vi.fn();
    const stop = subscribeToAccountErasure(erased);
    announceAccountErasure();
    expect(erased).toHaveBeenCalledTimes(1);
    expect(localStorage.getItem('umwelt:account-erasure')).toBeNull();
    stop();
    announceAccountErasure();
    expect(erased).toHaveBeenCalledTimes(1);
  });

  it('responds to the other-tab storage signal and ignores unrelated storage changes', () => {
    vi.stubGlobal('BroadcastChannel', undefined);
    const erased = vi.fn();
    const stop = subscribeToAccountErasure(erased);
    window.dispatchEvent(new StorageEvent('storage', { key: 'theme', newValue: 'light' }));
    window.dispatchEvent(new StorageEvent('storage', { key: 'umwelt:account-erasure', newValue: 'signal' }));
    expect(erased).toHaveBeenCalledTimes(1);
    stop();
  });
});
