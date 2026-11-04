import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { configureWorkspace } from '@/core/api';
import { RealtimeProvider, useRealtime } from '@/core/realtime-provider';

// Stable references: the provider's effect depends on these, so fresh objects per render would re-open the stream.
const display = vi.hoisted(() => ({ authGeneration: 1, isCurrentGeneration: () => true }));
vi.mock('@/core/query-provider', () => ({ useDisplayPreferences: () => display }));
vi.mock('next-intl', () => ({ useTranslations: () => (key: string) => key }));

const CURSOR = '11111111-1111-4111-8111-111111111111:5';

class FakeEventSource {
  static instances: FakeEventSource[] = [];
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  constructor(readonly url: string) { FakeEventSource.instances.push(this); }
  addEventListener() {}
  close = vi.fn();
}

beforeEach(() => {
  FakeEventSource.instances = [];
  vi.stubGlobal('EventSource', FakeEventSource);
  vi.stubGlobal('fetch', vi.fn(async (path: string) => {
    const body = path === '/api/v1/auth/session'
      ? { authenticated: true, csrfToken: 'csrf' }
      : path === '/api/v1/realtime/snapshot'
        ? { cursor: CURSOR, floor_sequence: '0' }
        : null;
    if (!body) throw new Error(`unexpected fetch ${path}`);
    return new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } });
  }));
  configureWorkspace({ selectedId: () => 'ws-1', generation: () => 0, signal: () => new AbortController().signal });
});

afterEach(() => {
  configureWorkspace(null);
  vi.unstubAllGlobals();
});

describe('RealtimeProvider', () => {
  it('opens the event stream with the selected workspace_id', async () => {
    render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <RealtimeProvider>
          <div>child</div>
        </RealtimeProvider>
      </QueryClientProvider>,
    );

    await waitFor(() => expect(FakeEventSource.instances).toHaveLength(1));
    expect(FakeEventSource.instances[0].url).toBe(
      `/api/v1/realtime/events?cursor=${encodeURIComponent(CURSOR)}&workspace_id=ws-1`,
    );
  });

  describe('open watchdog', () => {
    /** Renders the provider with a child exposing the realtime status. */
    const renderStatus = () => {
      const Status = () => <div data-testid="status">{useRealtime().status}</div>;
      render(
        <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
          <RealtimeProvider><Status /></RealtimeProvider>
        </QueryClientProvider>,
      );
    };
    const snapshotFetches = () => vi.mocked(fetch).mock.calls.filter(([path]) => path === '/api/v1/realtime/snapshot').length;

    afterEach(() => vi.useRealTimers());

    it('reports unavailable and retries when the stream never opens', async () => {
      vi.useFakeTimers({ shouldAdvanceTime: true });
      renderStatus();
      await vi.waitFor(() => expect(FakeEventSource.instances).toHaveLength(1));
      await act(async () => { await vi.advanceTimersByTimeAsync(20_000); });
      expect(screen.getByTestId('status').textContent).toBe('unavailable');
      expect(FakeEventSource.instances[0].close).toHaveBeenCalled();
      await act(async () => { await vi.advanceTimersByTimeAsync(1_000); });
      await vi.waitFor(() => expect(FakeEventSource.instances).toHaveLength(2));
      expect(snapshotFetches()).toBe(2);
    });

    it('stays connected without a second stream when onopen fires before the deadline', async () => {
      vi.useFakeTimers({ shouldAdvanceTime: true });
      renderStatus();
      await vi.waitFor(() => expect(FakeEventSource.instances).toHaveLength(1));
      act(() => FakeEventSource.instances[0].onopen?.());
      await act(async () => { await vi.advanceTimersByTimeAsync(25_000); });
      expect(screen.getByTestId('status').textContent).toBe('connected');
      expect(FakeEventSource.instances).toHaveLength(1);
    });
  });
});
