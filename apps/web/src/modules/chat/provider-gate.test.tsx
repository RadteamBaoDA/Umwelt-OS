import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, renderHook, screen, waitFor } from '@testing-library/react';
import { NextIntlClientProvider } from 'next-intl';
import type { ReactNode } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { messages } from '@/core/messages';
import { isChatProviderReady, MODELS_SETTINGS_HREF, ProviderGateAlert, useChatProviderReady } from './provider-gate';

const base = {
  chat_alias: 'reasoning-large',
  providers: [{ id: 'default', credential_configured: true, local: false, policy_denied: false, destination_id: 'remote:x' }],
  aliases: { 'reasoning-large': { model: 'gpt', provider_id: null } },
  privacy: { allow_remote_reasoning: true, reasoning_destinations: ['remote:x'] },
};
const local = { ...base, providers: [{ id: 'ol', credential_configured: false, local: true, policy_denied: false, destination_id: null }], privacy: undefined };

afterEach(() => vi.unstubAllGlobals());

/** Wraps hook renders in a fresh query client. */
function wrapper(client: QueryClient) {
  /** Provides the shared client. */
  function Wrapper({ children }: { children: ReactNode }) { return <QueryClientProvider client={client}>{children}</QueryClientProvider>; }
  return Wrapper;
}

/** Stubs fetch with a JSON settings body or a failure. */
function stubFetch(body: unknown | Error) {
  const fn = vi.fn(async () => {
    if (body instanceof Error) throw body;
    return new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } });
  });
  vi.stubGlobal('fetch', fn);
  return fn;
}

describe('isChatProviderReady', () => {
  it('is ready for a credentialed, consented remote provider and for a bare local provider', () => {
    expect(isChatProviderReady(base)).toBe(true);
    expect(isChatProviderReady(local)).toBe(true);
  });
  it('is not ready without mapping, credential, consent or when policy denies', () => {
    expect(isChatProviderReady({ ...base, aliases: {} })).toBe(false);
    expect(isChatProviderReady({ ...base, providers: [{ ...base.providers[0], credential_configured: false }] })).toBe(false);
    expect(isChatProviderReady({ ...base, privacy: { allow_remote_reasoning: false } })).toBe(false);
    expect(isChatProviderReady({ ...base, providers: [{ ...base.providers[0], policy_denied: true }] })).toBe(false);
    expect(isChatProviderReady({ ...base, providers: [] })).toBe(false);
  });
  it('does not block on the destination allow-list: the server falls back to permitted_destinations', () => {
    // Credential and consent are present but reasoning_destinations is empty: the server may still accept the
    // destination via permitted_destinations, so the gate must not stop the send.
    expect(isChatProviderReady({ ...base, privacy: { allow_remote_reasoning: true, reasoning_destinations: [] } })).toBe(true);
  });
});

describe('useChatProviderReady', () => {
  it('reports not ready when settings show no usable model', async () => {
    stubFetch({ ...base, aliases: {} });
    const { result } = renderHook(() => useChatProviderReady(), { wrapper: wrapper(new QueryClient()) });
    await waitFor(() => expect(result.current).toBe(false));
  });
  it('reports ready when configured', async () => {
    const f = stubFetch(base);
    const { result } = renderHook(() => useChatProviderReady(), { wrapper: wrapper(new QueryClient()) });
    await waitFor(() => expect(f).toHaveBeenCalled());
    expect(result.current).toBe(true);
  });
  it('fails open on a settings error', async () => {
    const f = stubFetch(new Error('down'));
    const { result } = renderHook(() => useChatProviderReady(), { wrapper: wrapper(new QueryClient()) });
    await waitFor(() => expect(f).toHaveBeenCalled());
    expect(result.current).toBe(true);
  });
  it('caches within the stale window across mounts', async () => {
    const f = stubFetch(base);
    const client = new QueryClient();
    const first = renderHook(() => useChatProviderReady(), { wrapper: wrapper(client) });
    await waitFor(() => expect(f).toHaveBeenCalledTimes(1));
    first.unmount();
    renderHook(() => useChatProviderReady(), { wrapper: wrapper(client) });
    expect(f).toHaveBeenCalledTimes(1);
  });
  it('does not read settings when disabled', () => {
    const f = stubFetch(base);
    const { result } = renderHook(() => useChatProviderReady(false), { wrapper: wrapper(new QueryClient()) });
    expect(result.current).toBe(true);
    expect(f).not.toHaveBeenCalled();
  });
});

describe('ProviderGateAlert', () => {
  it.each([['en-us', 'Open Settings'], ['vi-vi', 'Mở Cài đặt']] as const)('links to Settings in %s', (locale, label) => {
    render(<NextIntlClientProvider locale={locale} messages={messages[locale]}><ProviderGateAlert onDismiss={() => undefined} /></NextIntlClientProvider>);
    expect(screen.getByRole('alert')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: label })).toHaveAttribute('href', MODELS_SETTINGS_HREF);
  });
});
