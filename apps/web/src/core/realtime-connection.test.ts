import { QueryClient } from '@tanstack/react-query';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { RealtimeConnection, type RealtimeConnectionDeps } from '@/core/realtime-connection';

const UUID = '11111111-1111-4111-8111-111111111111';
const CURSOR = `${UUID}:5`;

type Listener = (event: Event) => void;

class FakeEventSource {
  static instances: FakeEventSource[] = [];
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  listeners = new Map<string, Listener[]>();
  close = vi.fn();
  constructor(readonly url: string) { FakeEventSource.instances.push(this); }
  addEventListener(name: string, fn: Listener) { this.listeners.set(name, [...(this.listeners.get(name) ?? []), fn]); }
  /** Delivers a named server event with the given SSE id and payload. */
  emit(name: string, data: string, id = '') {
    for (const fn of this.listeners.get(name) ?? []) fn(new MessageEvent(name, { data, lastEventId: id }));
  }
}

/** Builds a JSON Response for the stubbed fetch. */
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });

/** Creates a connection with spy dependencies and a shared cursor ref. */
function build() {
  const deps = {
    client: new QueryClient(),
    isCurrentAuthGeneration: () => true,
    setStatus: vi.fn(),
    markBarrier: vi.fn(),
    cursorRef: { current: '' },
    retryRef: { current: null as ReturnType<typeof setTimeout> | null },
    invalidateOperationalQueries: vi.fn(async () => {}),
    queueDocumentUpdate: vi.fn(),
    resetDocumentState: vi.fn(),
  } satisfies RealtimeConnectionDeps;
  return { deps, connection: new RealtimeConnection(deps) };
}

describe('RealtimeConnection', () => {
  let snapshot: () => Response;
  let session: () => Response;
  beforeEach(() => {
    FakeEventSource.instances = [];
    snapshot = () => json({ cursor: CURSOR, floor_sequence: '0' });
    session = () => json({ authenticated: true });
    vi.stubGlobal('EventSource', FakeEventSource);
    vi.stubGlobal('fetch', vi.fn(async (path: string) => (path === '/api/v1/auth/session' ? session() : snapshot())));
  });
  afterEach(() => { vi.unstubAllGlobals(); vi.useRealTimers(); });

  it('opens a stream at the snapshot cursor, releases the barrier and reports connected on open', async () => {
    const { deps, connection } = build();
    connection.start();
    await vi.waitFor(() => expect(FakeEventSource.instances).toHaveLength(1));
    expect(deps.cursorRef.current).toBe(CURSOR);
    expect(deps.markBarrier).toHaveBeenCalled();
    FakeEventSource.instances[0].onopen?.();
    expect(deps.setStatus).toHaveBeenLastCalledWith('connected');
    connection.dispose();
  });

  it('resyncs through a fresh snapshot when a domain event payload is malformed', async () => {
    const { deps, connection } = build();
    connection.start();
    await vi.waitFor(() => expect(FakeEventSource.instances).toHaveLength(1));
    const first = FakeEventSource.instances[0];
    first.emit('source.changed', 'not json', `${UUID}:6`);
    expect(first.close).toHaveBeenCalled();
    expect(deps.setStatus).toHaveBeenCalledWith('reconnecting');
    await vi.waitFor(() => expect(FakeEventSource.instances).toHaveLength(2));
    expect(deps.invalidateOperationalQueries).toHaveBeenCalled();
    expect(deps.queueDocumentUpdate).toHaveBeenCalledWith(null);
    connection.dispose();
  });

  it('ignores stale or repeated cursors of the same stream without resyncing', async () => {
    const { deps, connection } = build();
    connection.start();
    await vi.waitFor(() => expect(FakeEventSource.instances).toHaveLength(1));
    FakeEventSource.instances[0].emit('source.changed', 'not json', `${UUID}:5`);
    expect(FakeEventSource.instances).toHaveLength(1);
    expect(deps.cursorRef.current).toBe(CURSOR);
    connection.dispose();
  });

  it('resyncs when the cursor belongs to a different stream epoch', async () => {
    const { connection } = build();
    connection.start();
    await vi.waitFor(() => expect(FakeEventSource.instances).toHaveLength(1));
    FakeEventSource.instances[0].emit('source.changed', '{}', '22222222-2222-4222-8222-222222222222:1');
    await vi.waitFor(() => expect(FakeEventSource.instances).toHaveLength(2));
    connection.dispose();
  });

  it('reports expired and dispatches umwelt:unauthorized when the snapshot answers 401', async () => {
    snapshot = () => json({ error: { message: 'no' } }, 401);
    const onUnauthorized = vi.fn();
    window.addEventListener('umwelt:unauthorized', onUnauthorized);
    const { deps, connection } = build();
    connection.start();
    await vi.waitFor(() => expect(deps.setStatus).toHaveBeenCalledWith('expired'));
    expect(onUnauthorized).toHaveBeenCalledTimes(1);
    window.removeEventListener('umwelt:unauthorized', onUnauthorized);
    connection.dispose();
  });

  it('reports expired when a stream error is followed by a 401 session probe', async () => {
    const { deps, connection } = build();
    connection.start();
    await vi.waitFor(() => expect(FakeEventSource.instances).toHaveLength(1));
    session = () => json({ error: { message: 'no' } }, 401);
    FakeEventSource.instances[0].onerror?.();
    expect(deps.setStatus).toHaveBeenCalledWith('reconnecting');
    await vi.waitFor(() => expect(deps.setStatus).toHaveBeenLastCalledWith('expired'));
    connection.dispose();
  });

  it('backs off snapshot retries exponentially and resets after the stream opens', async () => {
    vi.useFakeTimers();
    snapshot = () => json({ cursor: 'bad', floor_sequence: '0' });
    const { deps, connection } = build();
    connection.start();
    await vi.advanceTimersByTimeAsync(0);
    expect(deps.setStatus).toHaveBeenCalledWith('unavailable');
    const fetchCalls = () => vi.mocked(fetch).mock.calls.length;
    expect(fetchCalls()).toBe(1);
    await vi.advanceTimersByTimeAsync(999);
    expect(fetchCalls()).toBe(1);
    await vi.advanceTimersByTimeAsync(1);
    expect(fetchCalls()).toBe(2);
    await vi.advanceTimersByTimeAsync(1999);
    expect(fetchCalls()).toBe(2);
    await vi.advanceTimersByTimeAsync(1);
    expect(fetchCalls()).toBe(3);
    connection.dispose();
  });

  it('delays a repeated auth_expired resync by five seconds', async () => {
    const { connection } = build();
    connection.start();
    await vi.waitFor(() => expect(FakeEventSource.instances).toHaveLength(1));
    vi.useFakeTimers();
    FakeEventSource.instances[0].emit('auth_expired', '');
    const second = FakeEventSource.instances.length;
    // First one resyncs immediately (closing the stream); a stream is only reopened once the snapshot resolves.
    await vi.advanceTimersByTimeAsync(0);
    vi.useRealTimers();
    await vi.waitFor(() => expect(FakeEventSource.instances.length).toBeGreaterThan(second - 1));
    const latest = FakeEventSource.instances[FakeEventSource.instances.length - 1];
    const before = vi.mocked(fetch).mock.calls.length;
    vi.useFakeTimers();
    latest.emit('auth_expired', '');
    await vi.advanceTimersByTimeAsync(4000);
    expect(vi.mocked(fetch).mock.calls.length).toBe(before);
    await vi.advanceTimersByTimeAsync(1100);
    expect(vi.mocked(fetch).mock.calls.length).toBe(before + 1);
    connection.dispose();
  });

  it('authEnding disposes, clears the cursor and resets document state', async () => {
    const { deps, connection } = build();
    connection.start();
    await vi.waitFor(() => expect(FakeEventSource.instances).toHaveLength(1));
    connection.authEnding();
    expect(deps.cursorRef.current).toBe('');
    expect(deps.resetDocumentState).toHaveBeenCalled();
    expect(FakeEventSource.instances[0].close).toHaveBeenCalled();
  });
});
