import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, renderHook, waitFor } from '@testing-library/react';
import type { ReactNode } from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const replace = vi.fn();
const apiRequest = vi.fn();
const endAuthSession = vi.fn();

vi.mock('next/navigation', () => ({ useRouter: () => ({ replace }) }));
vi.mock('@/core/api', () => {
  class ApiError extends Error {
    constructor(readonly status: number, message: string) { super(message); }
  }
  return { ApiError, apiRequest: (...args: unknown[]) => apiRequest(...args), csrfHeaders: () => ({}), retrySessionConflict: () => false };
});
const display = vi.hoisted(() => ({
  authGeneration: 0, isCurrentGeneration: () => true, confirmPreferences: () => {}, endAuthSession: () => {},
}));
vi.mock('@/core/query-provider', () => ({ useDisplayPreferences: () => ({ ...display, endAuthSession: () => endAuthSession() }) }));

import { ApiError } from '@/core/api';
import { useShellSession } from '@/core/app-shell/use-shell-session';

const resetOverlays = vi.fn();
const SESSION = { authenticated: true, csrfToken: 't', isInstanceAdmin: false };

/** Mounts the hook under a fresh query client, like the auth-keyed shell remount. */
function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>;
  return { client, ...renderHook(() => useShellSession(resetOverlays), { wrapper }) };
}

describe('useShellSession', () => {
  beforeEach(() => { replace.mockClear(); apiRequest.mockReset(); endAuthSession.mockClear(); resetOverlays.mockClear(); });

  it('logout success ends the session once and routes to /login', async () => {
    apiRequest.mockImplementation(async (path: string) => (path === '/api/v1/auth/session' ? SESSION : undefined));
    const { result } = mount();
    await waitFor(() => expect(result.current.session.data).toBeTruthy());
    await act(async () => { await result.current.logout.mutateAsync(); });
    await waitFor(() => expect(replace).toHaveBeenCalledWith('/login'));
    expect(endAuthSession).toHaveBeenCalledTimes(1);
    expect(resetOverlays).toHaveBeenCalledTimes(1);
  });

  it('announces bbd:auth-ending before the query cache is cleared', async () => {
    apiRequest.mockImplementation(async (path: string) => (path === '/api/v1/auth/session' ? SESSION : undefined));
    const { result, client } = mount();
    await waitFor(() => expect(result.current.session.data).toBeTruthy());
    let cachedAtAnnounce: unknown = 'unset';
    const onEnding = () => { cachedAtAnnounce = client.getQueryData(['session']); };
    window.addEventListener('bbd:auth-ending', onEnding);
    await act(async () => { await result.current.logout.mutateAsync(); });
    window.removeEventListener('bbd:auth-ending', onEnding);
    expect(cachedAtAnnounce).toEqual(SESSION);
    expect(client.getQueryData(['session'])).toBeUndefined();
  });

  it('logout failure re-arms expiry reporting so a later 401 is ?reason=expired', async () => {
    apiRequest.mockImplementation(async (path: string) => {
      if (path === '/api/v1/auth/session') return SESSION;
      if (path === '/api/v1/auth/logout') throw new Error('network');
      return undefined;
    });
    const { result } = mount();
    await waitFor(() => expect(result.current.session.data).toBeTruthy());
    await act(async () => { await result.current.logout.mutateAsync().catch(() => undefined); });
    expect(replace).not.toHaveBeenCalled();
    act(() => { window.dispatchEvent(new Event('umwelt:unauthorized')); });
    expect(replace).toHaveBeenCalledWith('/login?reason=expired');
  });

  it('a 401 session read routes to /login?reason=expired', async () => {
    apiRequest.mockImplementation(async () => { throw new ApiError(401, 'expired'); });
    mount();
    await waitFor(() => expect(replace).toHaveBeenCalledWith('/login?reason=expired'));
  });

  it('endSession is single-shot: repeated unauthorized events navigate once', async () => {
    apiRequest.mockImplementation(async (path: string) => (path === '/api/v1/auth/session' ? SESSION : undefined));
    const { result } = mount();
    await waitFor(() => expect(result.current.session.data).toBeTruthy());
    act(() => {
      window.dispatchEvent(new Event('umwelt:unauthorized'));
      window.dispatchEvent(new Event('umwelt:unauthorized'));
    });
    expect(replace).toHaveBeenCalledTimes(1);
    expect(endAuthSession).toHaveBeenCalledTimes(1);
  });

  it('a post-logout 401 on the remounted shell stays on /login (module latch)', async () => {
    apiRequest.mockImplementation(async (path: string) => (path === '/api/v1/auth/session' ? SESSION : undefined));
    const first = mount();
    await waitFor(() => expect(first.result.current.session.data).toBeTruthy());
    await act(async () => { await first.result.current.logout.mutateAsync(); });
    first.unmount();
    replace.mockClear();
    apiRequest.mockImplementation(async () => { throw new ApiError(401, 'expired'); });
    mount();
    await waitFor(() => expect(replace).toHaveBeenCalledWith('/login'));
    expect(replace).not.toHaveBeenCalledWith('/login?reason=expired');
  });
});
