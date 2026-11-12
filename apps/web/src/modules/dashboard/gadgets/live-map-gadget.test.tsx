import { act, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { NextIntlClientProvider } from 'next-intl';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { dashboardMessages } from '@/core/messages/dashboard';
import type { GadgetInstance } from '../api';

vi.mock('@/core/workspace-context', () => ({ useWorkspace: () => ({ selection: { id: 'workspace' } }) }));
vi.mock('@/core/query-provider', () => ({ useDisplayPreferences: () => ({ locale: 'en-us', timezone: 'UTC' }) }));
const request = vi.hoisted(() => vi.fn());
vi.mock('@/core/api', () => ({ retrySessionConflict: () => false, apiRequest: request, ApiError: class extends Error {} }));
import { LiveMapGadget } from './live-map-gadget';

describe('live map visibility', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    request.mockReset();
  });
  it('shows the proxied source links returned by the proxy', async () => {
    let observe: IntersectionObserverCallback | undefined;
    vi.stubGlobal(
      'IntersectionObserver',
      class {
        constructor(callback: IntersectionObserverCallback) {
          observe = callback;
        }
        observe() {}
        disconnect() {}
      },
    );
    vi.stubGlobal(
      'ResizeObserver',
      class {
        observe() {}
        disconnect() {}
      },
    );
    request.mockImplementation(async (url) =>
      url.includes('/config')
        ? {
            revision: 1,
            terms_catalog: [
              { id: 'lobsters', accepted: true },
              { id: 'cisa_kev', accepted: true },
            ],
          }
        : {
            fetched_at: '2026-11-04T00:00:00Z',
            truncated: false,
            items: [
              {
                id: '1',
                title: 'Event',
                source: 'Lobsters',
                observed_at: null,
                latitude: null,
                longitude: null,
                url: null,
                summary: null,
                sources: [{ id: 'agency', url: 'https://example.com/event' }],
              },
            ],
          },
    );
    const instance = { id: 'one', definition: { scope: { live_map_providers: ['lobsters'] }, filters: {} } } as GadgetInstance;
    render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <NextIntlClientProvider locale="en-US" messages={{ dashboard: dashboardMessages['en-us'] }}>
          <LiveMapGadget instance={instance} isEditMode={false} />
        </NextIntlClientProvider>
      </QueryClientProvider>,
    );
    await waitFor(() => expect(observe).toBeDefined());
    act(() => observe?.([{ isIntersecting: true } as IntersectionObserverEntry], {} as IntersectionObserver));
    const link = await screen.findByRole('link', { name: 'Provider source' });
    expect(link.getAttribute('href')).toBe('https://example.com/event');
  });
  it('hides previously fetched events when provider consent is withdrawn', async () => {
    let observe: IntersectionObserverCallback | undefined;
    vi.stubGlobal(
      'IntersectionObserver',
      class {
        constructor(callback: IntersectionObserverCallback) {
          observe = callback;
        }
        observe() {}
        disconnect() {}
      },
    );
    vi.stubGlobal(
      'ResizeObserver',
      class {
        observe() {}
        disconnect() {}
      },
    );
    const accepted = { revision: 1, terms_catalog: [{ id: 'cisa_kev', accepted: true }] };
    request.mockImplementation(async (url) =>
      url.includes('/config')
        ? accepted
        : {
            fetched_at: '2026-11-04T00:00:00Z',
            truncated: false,
            items: [
              { id: '1', title: 'Old event', source: 'USGS', observed_at: null, latitude: null, longitude: null, url: null, summary: null },
            ],
          },
    );
    const instance = { id: 'one', definition: { scope: { live_map_providers: ['cisa_kev'] }, filters: {} } } as GadgetInstance;
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <NextIntlClientProvider locale="en-US" messages={{ dashboard: dashboardMessages['en-us'] }}>
          <LiveMapGadget instance={instance} isEditMode={false} />
        </NextIntlClientProvider>
      </QueryClientProvider>,
    );
    await waitFor(() => expect(observe).toBeDefined());
    act(() => observe?.([{ isIntersecting: true } as IntersectionObserverEntry], {} as IntersectionObserver));
    expect(await screen.findByText('Old event')).toBeTruthy();
    act(() => {
      client.setQueryData(['live-map-config', 'workspace'], { revision: 2, terms_catalog: [{ id: 'cisa_kev', accepted: false }] });
    });
    await waitFor(() => expect(screen.queryByText('Old event')).toBeNull());
  });
  it('cancels an in-flight provider only after its last visible gadget leaves', async () => {
    const observers: IntersectionObserverCallback[] = [];
    vi.stubGlobal(
      'IntersectionObserver',
      class {
        constructor(callback: IntersectionObserverCallback) {
          observers.push(callback);
        }
        observe() {}
        disconnect() {}
      },
    );
    vi.stubGlobal(
      'ResizeObserver',
      class {
        observe() {}
        disconnect() {}
      },
    );
    let signal: AbortSignal | undefined;
    request.mockImplementation((url, options) =>
      url.includes('/config')
        ? Promise.resolve({
            revision: 1,
            terms_catalog: [
              { id: 'lobsters', accepted: true },
              { id: 'cisa_kev', accepted: true },
            ],
          })
        : new Promise((_resolve, reject) => {
            signal = options.signal;
            signal?.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')));
          }),
    );
    const instance = { id: 'one', definition: { scope: { live_map_providers: ['cisa_kev'] }, filters: {} } } as GadgetInstance;
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const renderMaps = (count: number) => (
      <QueryClientProvider client={client}>
        <NextIntlClientProvider locale="en-US" messages={{ dashboard: dashboardMessages['en-us'] }}>
          {Array.from({ length: count }, (_, i) => (
            <LiveMapGadget key={i} instance={instance} isEditMode={false} />
          ))}
        </NextIntlClientProvider>
      </QueryClientProvider>
    );
    const view = render(renderMaps(2));
    await waitFor(() => expect(observers).toHaveLength(2));
    act(() => {
      for (const callback of observers) callback([{ isIntersecting: true } as IntersectionObserverEntry], {} as IntersectionObserver);
    });
    await waitFor(() => expect(signal).toBeDefined());
    expect(request.mock.calls.filter(([url]) => url.includes('/events'))).toHaveLength(1);
    view.rerender(renderMaps(1));
    expect(signal?.aborted).toBe(false);
    act(() => observers[0]([{ isIntersecting: false } as IntersectionObserverEntry], {} as IntersectionObserver));
    await waitFor(() => expect(signal?.aborted).toBe(true));
  });
});
