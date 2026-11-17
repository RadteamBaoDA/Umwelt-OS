import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { renderHook, waitFor } from '@testing-library/react';
import type { ReactNode } from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { useChatWebSearch } from './use-chat-web-search';

const apiRequest = vi.hoisted(() => vi.fn());
vi.mock('@/core/api', () => ({ apiRequest }));

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

const settings = (overrides: Record<string, unknown> = {}) => ({
  web_search_provider: 'brave',
  web_search_credential_configured: true,
  privacy: { allow_remote_web_search: true },
  ...overrides,
});

describe('useChatWebSearch', () => {
  beforeEach(() => apiRequest.mockReset());

  it('does not load settings in drawer mode', () => {
    const { result } = renderHook(() => useChatWebSearch('drawer', false), { wrapper });
    expect(apiRequest).not.toHaveBeenCalled();
    expect(result.current).toEqual({ available: false, usable: false, provider: undefined });
  });

  it('is available and usable with a configured provider and remote search allowed', async () => {
    apiRequest.mockResolvedValue(settings());
    const { result } = renderHook(() => useChatWebSearch('full', false), { wrapper });
    await waitFor(() => expect(result.current.available).toBe(true));
    expect(result.current).toEqual({ available: true, usable: true, provider: 'brave' });
    expect(apiRequest).toHaveBeenCalledWith('/api/v1/settings/ai');
  });

  it('is unavailable without credentials, except for the keyless searxng provider', async () => {
    apiRequest.mockResolvedValue(settings({ web_search_credential_configured: false }));
    const missing = renderHook(() => useChatWebSearch('full', false), { wrapper });
    await waitFor(() => expect(missing.result.current.provider).toBe('brave'));
    expect(missing.result.current.available).toBe(false);

    apiRequest.mockResolvedValue(settings({ web_search_provider: 'searxng', web_search_credential_configured: false }));
    const searx = renderHook(() => useChatWebSearch('full', false), { wrapper });
    await waitFor(() => expect(searx.result.current.available).toBe(true));
  });

  it('is unavailable when the provider is none or the privacy setting forbids remote search', async () => {
    apiRequest.mockResolvedValue(settings({ web_search_provider: 'none' }));
    const none = renderHook(() => useChatWebSearch('full', false), { wrapper });
    await waitFor(() => expect(none.result.current.provider).toBe('none'));
    expect(none.result.current.available).toBe(false);

    apiRequest.mockResolvedValue(settings({ privacy: { allow_remote_web_search: false } }));
    const private_ = renderHook(() => useChatWebSearch('full', false), { wrapper });
    await waitFor(() => expect(private_.result.current.provider).toBe('brave'));
    expect(private_.result.current.available).toBe(false);
  });

  it('keeps availability but not usability while the selected profile blocks web search', async () => {
    apiRequest.mockResolvedValue(settings());
    const { result } = renderHook(() => useChatWebSearch('full', true), { wrapper });
    await waitFor(() => expect(result.current.available).toBe(true));
    expect(result.current.usable).toBe(false);
  });
});
