import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { configureWorkspace } from '@/core/api';
import { RealtimeProvider } from '@/core/realtime-provider';

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
  close() {}
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
});
