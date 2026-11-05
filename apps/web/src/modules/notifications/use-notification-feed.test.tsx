import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, renderHook, waitFor } from '@testing-library/react';
import type { ReactNode } from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { useNotificationFeed } from './notification-bell';

const mocks = vi.hoisted(() => ({ apiRequest: vi.fn(), listNotifications: vi.fn(), setNotificationRead: vi.fn() }));
const known = new Set(['kind_source_failed']);
const t = Object.assign((key: string, values?: Record<string, unknown>) => `${key}:${JSON.stringify(values ?? {})}`, {
  has: (key: string) => known.has(key),
});
vi.mock('next-intl', () => ({ useTranslations: () => t }));
vi.mock('@/core/api', () => ({ apiRequest: mocks.apiRequest }));
vi.mock('./api', async (original) => ({
  ...(await original<typeof import('./api')>()),
  listNotifications: mocks.listNotifications,
  setNotificationRead: mocks.setNotificationRead,
}));

const item = (overrides: Record<string, unknown> = {}) =>
  ({
    id: 'n1',
    kind: 'source.failed',
    title: 'Legacy',
    body: null,
    link: null,
    params: { name: 'Feed' },
    read_at: null,
    created_at: '2026-01-01T00:00:00Z',
    ...overrides,
  }) as never;

function setup() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const invalidate = vi.spyOn(client, 'invalidateQueries');
  const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>;
  return { invalidate, ...renderHook(() => useNotificationFeed(), { wrapper }) };
}

describe('useNotificationFeed', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.apiRequest.mockResolvedValue({ authenticated: true, csrfToken: 'csrf-9' });
    mocks.listNotifications.mockResolvedValue({ items: [item()], unread_count: 3 });
    mocks.setNotificationRead.mockResolvedValue({});
  });

  it('exposes the unread count from the loaded page, zero before it loads', async () => {
    const { result } = setup();
    expect(result.current.unread).toBe(0);
    await waitFor(() => expect(result.current.unread).toBe(3));
  });

  it('titleOf localizes known kinds with params and falls back to the stored title or kind', async () => {
    const { result } = setup();
    await waitFor(() => expect(result.current.page.isSuccess).toBe(true));
    expect(result.current.titleOf(item())).toBe('kind_source_failed:{"name":"Feed"}');
    expect(result.current.titleOf(item({ kind: 'legacy.kind', title: 'Stored title' }))).toBe('Stored title');
    expect(result.current.titleOf(item({ kind: 'legacy.kind', title: null }))).toBe('legacy.kind');
  });

  it('markRead sends the session csrf token and refreshes the list afterwards', async () => {
    const { result, invalidate } = setup();
    await waitFor(() => expect(result.current.session.isSuccess).toBe(true));
    await act(async () => {
      await result.current.markRead.mutateAsync('n1');
    });
    expect(mocks.setNotificationRead).toHaveBeenCalledWith('n1', true, 'csrf-9');
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['notifications'] });
  });

  it('refreshes the list even when marking read fails', async () => {
    mocks.setNotificationRead.mockRejectedValue(new Error('boom'));
    const { result, invalidate } = setup();
    await waitFor(() => expect(result.current.session.isSuccess).toBe(true));
    await act(async () => {
      await result.current.markRead.mutateAsync('n1').catch(() => undefined);
    });
    expect(invalidate).toHaveBeenCalled();
  });
});
