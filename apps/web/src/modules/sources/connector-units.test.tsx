import { act, renderHook } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import type { ConnectorCatalogEntry, ConnectorConfig, ConnectorConfiguration } from './api';
import {
  CONNECT_SECTIONS,
  connectFamily,
  SCOPE_SECTIONS,
  scopeFamily,
  type ConnectFamily,
  type ScopeFamily,
} from './connector-section-registry';
import { isGoogleProvider } from './connector-oauth';
import { authMethodOf, initialProviderOf, markDraftChanged, providerOfConfiguration, useConnectorDraft } from './use-connector-draft';
import { useConnectorRequests, type AuthGenerations } from './use-connector-requests';
import { connectGate, scopeGate } from './use-connector-wizard';

const entry = (extra: Record<string, unknown>) =>
  ({
    provider_id: 'x',
    kind: 'connector',
    source_type: 'api',
    code_available: true,
    auth_methods: ['none'],
    key_fields: [],
    scope_fields: [],
    ...extra,
  }) as unknown as ConnectorCatalogEntry;

describe('provider resolution helpers', () => {
  it('derives the starting provider from a source', () => {
    expect(initialProviderOf(undefined)).toBe('rest');
    expect(initialProviderOf({ provider: null, type: 'rss' } as never)).toBe('rss');
    expect(initialProviderOf({ provider: null, type: 'mcp' } as never)).toBe('mcp');
  });

  it('maps a server configuration to a provider, treating MCP only when allowed', () => {
    const value = (patch: object) => ({ provider: null, source_type: 'api', ...patch }) as unknown as ConnectorConfiguration;
    expect(providerOfConfiguration(value({ source_type: 'web' }), false)).toBe('web');
    expect(providerOfConfiguration(value({ source_type: 'mcp' }), false)).toBe('rest');
    expect(providerOfConfiguration(value({ source_type: 'mcp' }), true)).toBe('mcp');
  });

  it('maps auth methods onto the three editor modes', () => {
    expect(authMethodOf('http_header')).toBe('http_header');
    expect(authMethodOf('telegram_bot_token')).toBe('telegram_bot_token');
    expect(authMethodOf('oauth2')).toBe('none');
    expect(authMethodOf(undefined)).toBe('none');
  });
});

describe('section registry', () => {
  it('has a non-empty connect list for every family and a section for every scope family', () => {
    const families: ConnectFamily[] = ['google', 'reddit', 'telegram', 'rest', 'default'];
    for (const family of families) expect(CONNECT_SECTIONS[family].length).toBeGreaterThan(0);
    const scopes: ScopeFamily[] = ['google', 'health', 'home_assistant', 'fixed', 'native', 'rss', 'url'];
    for (const family of scopes) expect(typeof SCOPE_SECTIONS[family]).toBe('function');
  });

  it('keeps the original connect order (provider section between the capability sections)', () => {
    expect(CONNECT_SECTIONS.telegram.map((section) => section.name)).toEqual(['BearerTokenField', 'TelegramCredential', 'ApiKeyField']);
    expect(CONNECT_SECTIONS.rest.map((section) => section.name)).toEqual(['BearerTokenField', 'ApiKeyField', 'RestAuthFields']);
  });

  it('resolves families in the former chain order', () => {
    expect(connectFamily('google_drive')).toBe('google');
    expect(connectFamily('notion')).toBe('default');
    expect(scopeFamily('google_mail', undefined)).toBe('google');
    expect(scopeFamily('health', entry({ kind: 'connector' }))).toBe('health');
    expect(scopeFamily('google_news', entry({ kind: 'preset', scope_fields: [] }))).toBe('fixed');
    expect(scopeFamily('google_news', entry({ kind: 'preset', scope_fields: ['news_query'] }))).toBe('native');
    expect(scopeFamily('rss', entry({ kind: 'connector', provider_id: 'rss' }))).toBe('rss');
    expect(scopeFamily('web', undefined)).toBe('url');
    expect(isGoogleProvider('google_calendar')).toBe(true);
    expect(isGoogleProvider('reddit')).toBe(false);
  });
});

describe('wizard gates', () => {
  const base = {
    provider: 'rest',
    entry: undefined,
    redditReady: false,
    googleReady: false,
    credentialConfigured: false,
    secret: '',
    telegramSecretAction: 'keep' as const,
    authMethod: 'none',
    backendChoice: null,
  };
  it('gates credentialed providers on a key and grants on readiness', () => {
    expect(connectGate({ ...base })).toBe(true);
    expect(connectGate({ ...base, authMethod: 'http_header' })).toBe(false);
    expect(connectGate({ ...base, authMethod: 'http_header', secret: 'k' })).toBe(true);
    expect(connectGate({ ...base, provider: 'reddit', redditReady: true })).toBe(true);
    expect(connectGate({ ...base, provider: 'google_mail', googleReady: true })).toBe(false);
    expect(connectGate({ ...base, provider: 'google_mail', googleReady: true, credentialConfigured: true })).toBe(true);
    expect(connectGate({ ...base, provider: 'telegram', telegramSecretAction: 'replace', secret: 't' })).toBe(true);
    expect(connectGate({ ...base, provider: 'telegram' })).toBe(false);
    expect(connectGate({ ...base, provider: 'mastodon' })).toBe(true);
    expect(connectGate({ ...base, provider: 'alpha_vantage' })).toBe(false);
  });

  it('requires scope fields and exactly one side of the either-or scopes', () => {
    const config = (patch: object) => ({ ...patch }) as unknown as ConnectorConfig;
    expect(scopeGate('youtube', config({}))).toBe(false);
    expect(scopeGate('youtube', config({ youtube_channel_id: 'UC1' }))).toBe(true);
    expect(scopeGate('x', config({ x_account: 'a' }))).toBe(true);
    expect(scopeGate('x', config({ x_account: 'a', x_query: 'q' }))).toBe(false);
    expect(scopeGate('x', config({}))).toBe(false);
  });
});

describe('useConnectorDraft', () => {
  it('starts from defaults and tracks the dirty flag in its leave-guard ref', () => {
    const { result } = renderHook(() => useConnectorDraft(undefined));
    expect(result.current.provider).toBe('rest');
    expect(result.current.dirtyRef.current).toBe(false);
    act(() => markDraftChanged(result.current));
    expect(result.current.dirty).toBe(true);
    expect(result.current.dirtyRef.current).toBe(true);
    expect(result.current.draftVersion.current).toBe(1);
  });

  it('counts a typed secret as unsaved work for the leave guard even when the fields are clean', () => {
    const { result } = renderHook(() => useConnectorDraft(undefined));
    act(() => result.current.setSecret('s3cret'));
    expect(result.current.dirty).toBe(false);
    expect(result.current.dirtyRef.current).toBe(true);
  });

  it('exposes stable setters for field handlers', () => {
    const { result, rerender } = renderHook(() => useConnectorDraft(undefined));
    const first = result.current.setDirtyRef;
    rerender();
    expect(result.current.setDirtyRef).toBe(first);
    act(() => result.current.bumpDraftVersion());
    expect(result.current.draftVersion.current).toBe(1);
  });
});

describe('useConnectorRequests', () => {
  const display: AuthGenerations = { authGeneration: 1, isCurrentGeneration: (generation) => generation === 1 };
  const draftLike = { sourceId: 's1', draftVersion: { current: 4 } } as never;

  it('runs one request at a time and releases the slot when the owner finishes', () => {
    const { result } = renderHook(() => useConnectorRequests(draftLike, display));
    let token = null as ReturnType<typeof result.current.beginRequest>;
    act(() => {
      token = result.current.beginRequest('save');
    });
    expect(token).toMatchObject({ sourceId: 's1', draftVersion: 4, authGeneration: 1 });
    let second = 'x' as unknown;
    act(() => {
      second = result.current.beginRequest('other');
    });
    expect(second).toBeNull();
    expect(result.current.busyAction).toBe('save');
    act(() => result.current.endRequest(token!));
    expect(result.current.busyAction).toBe('');
  });

  it('drops in-flight requests on leave and rejects stale completions', () => {
    const { result } = renderHook(() => useConnectorRequests(draftLike, display));
    let token = null as ReturnType<typeof result.current.beginRequest>;
    act(() => {
      token = result.current.beginRequest('save');
    });
    expect(result.current.requestIsCurrent(token!)).toBe(true);
    act(() => result.current.dropInFlight());
    expect(token!.controller.signal.aborted).toBe(true);
    expect(result.current.requestIsCurrent(token!)).toBe(false);
    expect(result.current.busyAction).toBe('');
    let next = null as ReturnType<typeof result.current.beginRequest>;
    act(() => {
      next = result.current.beginRequest('save');
    });
    expect(next).not.toBeNull();
  });

  it('rejects a request from a previous authentication generation', () => {
    let current = 1;
    const switching: AuthGenerations = { authGeneration: 1, isCurrentGeneration: (generation) => generation === current };
    const { result } = renderHook(() => useConnectorRequests(draftLike, switching));
    let token = null as ReturnType<typeof result.current.beginRequest>;
    act(() => {
      token = result.current.beginRequest('save');
    });
    current = 2;
    expect(result.current.requestIsCurrent(token!)).toBe(false);
  });

  it('aborts both controllers on unmount cleanup', () => {
    const { result } = renderHook(() => useConnectorRequests(draftLike, display));
    const verify = new AbortController();
    result.current.verifyController.current = verify;
    act(() => {
      result.current.beginRequest('save');
    });
    const request = result.current.requestController.current!;
    act(() => result.current.abortOnUnmount());
    expect(verify.signal.aborted).toBe(true);
    expect(request.signal.aborted).toBe(true);
  });
});
