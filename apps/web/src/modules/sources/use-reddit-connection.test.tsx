import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, renderHook, waitFor } from '@testing-library/react';
import type { ReactNode } from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { EditorContext } from './connector-editor-context';
import { useRedditConnection } from './use-reddit-connection';

const mocks = vi.hoisted(() => ({
  getRedditOAuthStatus: vi.fn(),
  startRedditOAuth: vi.fn(),
  disconnectRedditOAuth: vi.fn(),
  startOAuthConnection: vi.fn(),
  revokeOAuthConnection: vi.fn(),
}));
vi.mock('./api', () => ({
  getRedditOAuthStatus: mocks.getRedditOAuthStatus,
  startRedditOAuth: mocks.startRedditOAuth,
  disconnectRedditOAuth: mocks.disconnectRedditOAuth,
}));
vi.mock('./connector-oauth', () => ({
  startOAuthConnection: mocks.startOAuthConnection,
  revokeOAuthConnection: mocks.revokeOAuthConnection,
}));

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}
const ctx = (draft: Record<string, unknown>) => ({ draft, csrfToken: 'csrf-1' }) as unknown as EditorContext;

describe('useRedditConnection', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.getRedditOAuthStatus.mockResolvedValue({ connected: true, token_revision: 4 });
  });

  it('polls OAuth status only for a saved reddit source', async () => {
    const reddit = renderHook(() => useRedditConnection(ctx({ provider: 'reddit', sourceId: 's1' })), { wrapper });
    await waitFor(() => expect(reddit.result.current.redditStatusQuery.data).toEqual({ connected: true, token_revision: 4 }));
    expect(mocks.getRedditOAuthStatus).toHaveBeenCalledWith('s1', expect.any(AbortSignal));
    mocks.getRedditOAuthStatus.mockClear();
    const unsaved = renderHook(() => useRedditConnection(ctx({ provider: 'reddit', sourceId: '' })), { wrapper });
    const other = renderHook(() => useRedditConnection(ctx({ provider: 'rss', sourceId: 's1' })), { wrapper });
    expect(unsaved.result.current.redditStatusQuery.fetchStatus).toBe('idle');
    expect(other.result.current.redditStatusQuery.fetchStatus).toBe('idle');
    expect(mocks.getRedditOAuthStatus).not.toHaveBeenCalled();
  });

  it('connectReddit delegates to the shared OAuth flow with a fenced start call', async () => {
    const context = ctx({ provider: 'reddit', sourceId: 's1' });
    const { result } = renderHook(() => useRedditConnection(context), { wrapper });
    const saved = { id: 's1', revision: 2 };
    await act(() => result.current.connectReddit(saved));
    const [passedCtx, options] = mocks.startOAuthConnection.mock.calls[0];
    expect(passedCtx).toBe(context);
    expect(options).toMatchObject({ action: 'reddit-connect', failureKey: 'redditConnectionFailed', saved });
    const controller = new AbortController();
    options.start({ id: 's1', generation: 3, revision: 2 }, { controller });
    expect(mocks.startRedditOAuth).toHaveBeenCalledWith('s1', 3, 2, 'csrf-1', controller.signal);
  });

  it('connectReddit is a no-op for other providers', async () => {
    const { result } = renderHook(() => useRedditConnection(ctx({ provider: 'rss', sourceId: 's1' })), { wrapper });
    await act(() => result.current.connectReddit());
    expect(mocks.startOAuthConnection).not.toHaveBeenCalled();
  });

  it('disconnectReddit passes the current status, refetch and a revoke call carrying the token revision', async () => {
    const { result } = renderHook(() => useRedditConnection(ctx({ provider: 'reddit', sourceId: 's1' })), { wrapper });
    await waitFor(() => expect(result.current.redditStatusQuery.isSuccess).toBe(true));
    await act(() => result.current.disconnectReddit());
    const options = mocks.revokeOAuthConnection.mock.calls[0][1];
    expect(options).toMatchObject({
      action: 'reddit-disconnect',
      failureKey: 'redditDisconnectFailed',
      status: { connected: true, token_revision: 4 },
    });
    const controller = new AbortController();
    options.revoke({ generation: 3, revision: 2 }, 4, { controller });
    expect(mocks.disconnectRedditOAuth).toHaveBeenCalledWith('s1', 3, 2, 4, 'csrf-1', controller.signal);
  });
});
