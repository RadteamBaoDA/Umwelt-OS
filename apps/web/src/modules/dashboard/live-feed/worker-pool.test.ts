import { beforeEach, describe, expect, it, vi } from 'vitest';
import { subscribeWorker, terminateAll, unionConfig, type PoolWorker, type Subscriber } from './worker-pool';

const sub = (providers: string[], active = true): Subscriber => ({ providers, scope: {}, active, onMessage: vi.fn() });
const fakeWorker = (): PoolWorker => ({ postMessage: vi.fn(), terminate: vi.fn(), onmessage: null });

describe('unionConfig', () => {
  it('unions providers and is active when any subscriber is', () => {
    expect(unionConfig([sub(['b', 'a'], false), sub(['c', 'a'])])).toEqual({ providers: ['a', 'b', 'c'], scope: {}, active: true });
  });
});

describe('subscribeWorker', () => {
  beforeEach(() => {
    // Pin visibility so the expected visibility messages do not depend on the test runner's document state.
    Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'visible' });
  });

  it('shares one worker per workspace and terminates after the last release', () => {
    const worker = fakeWorker();
    const create = vi.fn(() => worker);
    const first = subscribeWorker('w1', sub(['a']), create);
    const second = subscribeWorker('w1', sub(['b']), create);
    expect(create).toHaveBeenCalledTimes(1);
    // The second subscriber changes the provider union, so config is re-posted; visibility was already sent once.
    expect(worker.postMessage).toHaveBeenCalledWith({ type: 'visibility', visible: true });
    first.release();
    expect(worker.terminate).not.toHaveBeenCalled();
    second.release();
    expect(worker.terminate).toHaveBeenCalledTimes(1);
  });

  it('routes status only to subscribers of that provider', () => {
    const worker = fakeWorker();
    const a = sub(['a']);
    const b = sub(['b']);
    const ra = subscribeWorker('w2', a, () => worker);
    const rb = subscribeWorker('w2', b, () => worker);
    worker.onmessage?.({ data: { type: 'status', provider: 'a', ok: true, fetchedAt: 'x' } } as MessageEvent);
    expect(a.onMessage).toHaveBeenCalledTimes(1);
    expect(b.onMessage).not.toHaveBeenCalled();
    ra.release();
    rb.release();
  });
});

/** Count `config` messages a fake worker received. */
const configPosts = (worker: PoolWorker) =>
  vi.mocked(worker.postMessage).mock.calls.filter(([message]) => message.type === 'config').length;

describe('worker config churn', () => {
  beforeEach(() => {
    Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'visible' });
  });

  it('posts config only when the merged config changes', () => {
    const worker = fakeWorker();
    const first = subscribeWorker('churn-config', sub(['a']), () => worker);
    const second = subscribeWorker('churn-config', sub(['a']), () => worker);
    expect(configPosts(worker)).toBe(1);
    second.update({ providers: ['a'], scope: {}, active: true });
    first.update({ providers: ['a'], scope: {}, active: true });
    expect(configPosts(worker)).toBe(1);
    second.update({ providers: ['a', 'c'], scope: {}, active: true });
    expect(configPosts(worker)).toBe(2);
  });

  it('posts only visibility when just the activity changes', () => {
    const worker = fakeWorker();
    const handle = subscribeWorker('churn-visibility', sub(['a']), () => worker);
    vi.mocked(worker.postMessage).mockClear();
    handle.update({ providers: ['a'], scope: {}, active: false });
    expect(vi.mocked(worker.postMessage).mock.calls).toEqual([[{ type: 'visibility', visible: false }]]);
  });
});

describe('worker pool lifecycle', () => {
  it('ignores a repeated release so it cannot unmap or terminate a newer worker', () => {
    const first = fakeWorker();
    const second = fakeWorker();
    const oldHandle = subscribeWorker('lifecycle-stale', sub(['a']), () => first);
    oldHandle.release();
    const newHandle = subscribeWorker('lifecycle-stale', sub(['a']), () => second);
    vi.mocked(first.postMessage).mockClear();
    oldHandle.release();
    oldHandle.update({ providers: ['z'], scope: {}, active: true });
    expect(second.terminate).not.toHaveBeenCalled();
    expect(first.postMessage).not.toHaveBeenCalled();
    // The newer entry is still the one shared for this workspace.
    const reused = vi.fn(() => fakeWorker());
    const third = subscribeWorker('lifecycle-stale', sub(['a']), reused);
    expect(reused).not.toHaveBeenCalled();
    newHandle.release();
    third.release();
    expect(second.terminate).toHaveBeenCalledTimes(1);
  });

  it('terminateAll terminates every worker, forgets subscribers and lets later calls no-op', () => {
    const w1 = fakeWorker();
    const w2 = fakeWorker();
    const h1 = subscribeWorker('lifecycle-all-1', sub(['a']), () => w1);
    const h2 = subscribeWorker('lifecycle-all-2', sub(['a']), () => w2);
    terminateAll();
    expect(w1.terminate).toHaveBeenCalledTimes(1);
    expect(w2.terminate).toHaveBeenCalledTimes(1);
    h1.release();
    h2.release();
    expect(w1.terminate).toHaveBeenCalledTimes(1);
    expect(w2.terminate).toHaveBeenCalledTimes(1);
    const fresh = vi.fn(() => fakeWorker());
    subscribeWorker('lifecycle-all-1', sub(['a']), fresh);
    expect(fresh).toHaveBeenCalledTimes(1);
    terminateAll();
  });
});
