import { beforeEach, describe, expect, it, vi } from 'vitest';

type Storage = typeof import('./live-feed-storage');

describe('live-feed teardown registry', () => {
  let storage: Storage;

  beforeEach(async () => {
    // Fresh module per test: the teardown latch is module state and must not leak between cases.
    vi.resetModules();
    storage = await import('./live-feed-storage');
  });

  it('runs every registered producer stop once per teardown and latches the flag', () => {
    const first = vi.fn();
    const second = vi.fn();
    expect(storage.isLiveFeedTeardownStarted()).toBe(false);
    storage.registerLiveFeedTeardown(first);
    storage.registerLiveFeedTeardown(second);
    storage.teardownLiveFeed();
    expect(first).toHaveBeenCalledTimes(1);
    expect(second).toHaveBeenCalledTimes(1);
    expect(storage.isLiveFeedTeardownStarted()).toBe(true);
  });

  it('keeps tearing down the other producers when one throws', () => {
    const after = vi.fn();
    storage.registerLiveFeedTeardown(() => {
      throw new Error('boom');
    });
    storage.registerLiveFeedTeardown(after);
    expect(() => storage.teardownLiveFeed()).not.toThrow();
    expect(after).toHaveBeenCalledTimes(1);
    expect(storage.isLiveFeedTeardownStarted()).toBe(true);
  });

  it('registering the same function twice runs it once', () => {
    const stop = vi.fn();
    storage.registerLiveFeedTeardown(stop);
    storage.registerLiveFeedTeardown(stop);
    storage.teardownLiveFeed();
    expect(stop).toHaveBeenCalledTimes(1);
  });
});
