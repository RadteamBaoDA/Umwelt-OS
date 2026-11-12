import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { NextIntlClientProvider } from 'next-intl';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { dashboardMessages } from '@/core/messages/dashboard';
import type { GadgetInstance } from '../api';

const workspace = vi.hoisted(() => ({ isOwner: true }));
vi.mock('@/core/workspace-context', () => ({ useWorkspace: () => ({ selection: { id: 'workspace' }, isOwner: workspace.isOwner }) }));
vi.mock('@/core/query-provider', () => ({ useDisplayPreferences: () => ({ locale: 'en-us', timezone: 'UTC' }) }));
const request = vi.hoisted(() => vi.fn());
vi.mock('@/core/api', () => ({ retrySessionConflict: () => false, apiRequest: request, ApiError: class extends Error {} }));
import { LiveMapGadget } from './live-map-gadget';

describe('live map visibility', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    request.mockReset();
    workspace.isOwner = true;
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
  it('keeps the map mounted and stops refetching when every gadget leaves the screen', async () => {
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
    act(() => observers[0]([{ isIntersecting: false } as IntersectionObserverEntry], {} as IntersectionObserver));
    // Content stays mounted (filters and selection survive); only fetching is paused.
    expect(screen.getAllByLabelText('Search events').length).toBeGreaterThan(0);
    expect(request.mock.calls.filter(([url]) => url.includes('/events'))).toHaveLength(1);
  });
  describe('empty state', () => {
    /** Renders the gadget as visible with the given config query behaviour. */
    const renderEmpty = async (config: () => Promise<unknown>) => {
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
      request.mockImplementation((url: string) =>
        url.includes('/config') ? config() : Promise.resolve({ items: [], fetched_at: '', truncated: false }),
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
    };
    it('says sources are loading while the config is pending', async () => {
      await renderEmpty(() => new Promise(() => undefined));
      expect(await screen.findByText('Loading live sources...')).toBeTruthy();
      expect(screen.queryByText('No events in this range.')).toBeNull();
    });
    it('shows the config error with a retry', async () => {
      await renderEmpty(() => Promise.reject(new Error('boom')));
      expect((await screen.findAllByText('Could not load provider settings.')).length).toBeGreaterThan(0);
      expect(screen.getByRole('button', { name: 'Retry' })).toBeTruthy();
    });
    it('points at gadget settings when no provider terms are accepted', async () => {
      await renderEmpty(async () => ({ revision: 1, terms_catalog: [{ id: 'lobsters', accepted: false }] }));
      expect(await screen.findAllByText('Review and accept provider terms in gadget settings.')).toBeTruthy();
      expect(screen.queryByText('No events in this range.')).toBeNull();
    });
    it('keeps the empty-range text when a provider is accepted but nothing matches', async () => {
      await renderEmpty(async () => ({ revision: 1, terms_catalog: [{ id: 'lobsters', accepted: true }] }));
      expect(await screen.findByText('No events in this range.')).toBeTruthy();
    });
  });
});

describe('live map owner gate, event cap and selection', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    request.mockReset();
    workspace.isOwner = true;
  });
  /** Renders a visible gadget for one proxied provider whose proxy returns `count` located events. */
  const renderVisible = async (count: number) => {
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
    request.mockImplementation(async (url: string) =>
      url.includes('/config')
        ? { revision: 1, terms_catalog: [{ id: 'lobsters', accepted: true }] }
        : {
            fetched_at: '2026-11-04T00:00:00Z',
            truncated: false,
            items: Array.from({ length: count }, (_, i) => ({
              id: String(i),
              title: `Event ${i}`,
              source: 'Lobsters',
              observed_at: null,
              latitude: null,
              longitude: null,
              url: null,
              summary: null,
            })),
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
  };
  it('renders only the first 50 rows until expanded', async () => {
    await renderVisible(120);
    await screen.findByText('Event 0');
    expect(screen.getAllByRole('listitem')).toHaveLength(50);
    fireEvent.click(screen.getByRole('button', { name: 'Show 70 more' }));
    expect(screen.getAllByRole('listitem')).toHaveLength(120);
  });
  it('does not request owner-only proxied data for non-owners and says so', async () => {
    workspace.isOwner = false;
    await renderVisible(1);
    expect(await screen.findByText(/Only the workspace owner can load this data\./)).toBeTruthy();
    expect(request.mock.calls.filter(([url]) => url.includes('/events'))).toHaveLength(0);
  });
  it('closes the selected event popup', async () => {
    await renderVisible(2);
    fireEvent.click(await screen.findByRole('button', { name: 'Event 1' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Close event details' }));
    expect(screen.queryByRole('button', { name: 'Close event details' })).toBeNull();
  });
});
