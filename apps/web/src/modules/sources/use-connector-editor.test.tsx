import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, renderHook, waitFor } from '@testing-library/react';
import { NextIntlClientProvider } from 'next-intl';
import type { ReactNode } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '@/core/api';
import { sourcesMessages } from '@/core/messages/sources';
import type { ConnectorCatalogEntry, ConnectorConfiguration, Source } from './api';

vi.mock('@/core/app-shell/workspace-shell', () => ({ useWorkspaceSession: () => ({ csrfToken: 't' }) }));
vi.mock('@/core/query-provider', () => {
  const display = { locale: 'en-us', timezone: 'UTC', authGeneration: 1, isCurrentGeneration: () => true };
  return { useDisplayPreferences: () => display };
});
vi.mock('@/core/guarded-navigation', () => ({ useGuardedNavigation: () => ({ registerLeaveGuard: () => () => undefined }) }));
vi.mock('@/core/workspace-context', () => ({ useWorkspace: () => ({ isOwner: true }) }));
const redirect = vi.hoisted(() => ({ assignAuthorizationUrl: vi.fn() }));
vi.mock('@/core/safe-url', () => redirect);

const api = vi.hoisted(() => ({
  getConnectorCatalog: vi.fn(),
  listBuiltinSources: vi.fn(),
  createConnectorSource: vi.fn(),
  getMonotonicConnectorConfiguration: vi.fn(),
  getConnectorActivation: vi.fn(),
  getProviderTerms: vi.fn(),
  saveConnectorConfiguration: vi.fn(),
  activateConnector: vi.fn(),
  getGitHubStatus: vi.fn(),
  getGoogleOAuthStatus: vi.fn(),
  getRedditOAuthStatus: vi.fn(),
  disconnectRedditOAuth: vi.fn(),
  acknowledgeGitHubReconnect: vi.fn(),
  startGitHubOAuth: vi.fn(),
  saveNativeRestCredential: vi.fn(),
}));
vi.mock('./api', async (original) => ({ ...(await original<typeof import('./api')>()), ...api }));

import { useConnectorEditor, type ConnectorEditorProps } from './use-connector-editor';

const entry = (provider_id: string, extra: Record<string, unknown> = {}): ConnectorCatalogEntry =>
  ({
    provider_id,
    label: provider_id,
    source_type: 'api',
    availability: 'available',
    code_available: true,
    auth_methods: ['none'],
    key_fields: [],
    scope_fields: [],
    example_config: {},
    ...extra,
  }) as unknown as ConnectorCatalogEntry;
const catalog = [
  entry('rest'),
  entry('coingecko', { kind: 'preset', availability: 'requires_credentials', key_fields: ['api_key'] }),
  entry('telegram', { auth_methods: ['telegram_bot_token'] }),
  entry('google_mail'),
  entry('reddit'),
  entry('github'),
];
const baseConfig = { timeout_seconds: 30, timezone: 'UTC', schedule_interval_minutes: 30, history_mode: 'returned_snapshot' };
const serverConfig = (provider: string | null, overrides: Record<string, unknown> = {}): ConnectorConfiguration =>
  ({
    source_id: 's1',
    source_type: 'api',
    source_generation: 1,
    provider,
    configuration: baseConfig,
    expected_revision: 1,
    auth_method: 'none',
    auth_header_name: null,
    desired_enabled: false,
    activation_state: 'saved_not_active',
    activation_error_code: null,
    provider_credential_configured: false,
    provider_credential_state: null,
    ...overrides,
  }) as unknown as ConnectorConfiguration;
const existing = (provider: string | null): Source =>
  ({
    id: 's1',
    name: 'Saved',
    type: 'api',
    provider,
    status: 'active',
    generation: 1,
    graph_enabled: false,
    rag_index_enabled: false,
  }) as unknown as Source;

const onChanged = vi.fn();
const onClose = vi.fn();
/** Mounts the editor hook the way the component does, inside query and message providers. */
function mount(source?: Source) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>
      <NextIntlClientProvider locale="en-US" messages={{ sources: sourcesMessages['en-us'] }}>
        {children}
      </NextIntlClientProvider>
    </QueryClientProvider>
  );
  const props: ConnectorEditorProps = { source, onClose, onChanged, registerTransitionGuard: () => undefined };
  return renderHook(() => useConnectorEditor(props), { wrapper });
}

beforeEach(() => {
  Object.values(api).forEach((fn) => fn.mockReset());
  onChanged.mockReset();
  onClose.mockReset();
  redirect.assignAuthorizationUrl.mockReset();
  api.getConnectorCatalog.mockResolvedValue(catalog);
  api.listBuiltinSources.mockResolvedValue([]);
  api.getConnectorActivation.mockResolvedValue({ state: 'saved_not_active', error_code: null });
  api.getProviderTerms.mockResolvedValue({ eligible: true });
  api.getGitHubStatus.mockResolvedValue(undefined);
  api.getGoogleOAuthStatus.mockResolvedValue({ state: 'ready', token_revision: 4, error_code: null });
  api.getRedditOAuthStatus.mockResolvedValue({ state: 'ready', token_revision: 2, error_code: null });
});
afterEach(cleanup);

describe('useConnectorEditor draft', () => {
  it('selectProvider resets provider-specific fields and marks the draft dirty', async () => {
    const { result } = mount();
    await waitFor(() => expect(result.current.catalog.length).toBeGreaterThan(0));
    act(() => result.current.setSecret('stale'));
    act(() => result.current.selectProvider('telegram', catalog[2]));
    expect(result.current.provider).toBe('telegram');
    expect(result.current.secret).toBe('');
    expect(result.current.telegramSecretAction).toBe('replace');
    expect(result.current.dirty).toBe(true);
    expect(result.current.draftVersion.current).toBe(1);
  });

  it('changeConfiguration edits one field, bumps the draft version and drops the validation', async () => {
    const { result } = mount();
    act(() => result.current.setValidation({ at: 'now', generation: 1, revision: 0 }));
    act(() => result.current.changeConfiguration('timezone', 'Europe/Paris'));
    expect(result.current.configuration.timezone).toBe('Europe/Paris');
    expect(result.current.validation).toBeNull();
    expect(result.current.dirtyRef.current).toBe(true);
    expect(result.current.draftVersion.current).toBe(1);
  });
});

describe('useConnectorEditor save and enable', () => {
  /** Mounts an unsaved REST source named `Mine`. */
  async function unsavedRest() {
    api.createConnectorSource.mockResolvedValue({ id: 's1', generation: 1 });
    api.saveConnectorConfiguration.mockResolvedValue({ state: 'saved_not_active', error_code: null, desired_revision: 1 });
    api.getMonotonicConnectorConfiguration.mockResolvedValue(serverConfig(null));
    api.activateConnector.mockResolvedValue({ state: 'active', error_code: null });
    const hook = mount();
    await waitFor(() => expect(hook.result.current.currentEntry?.provider_id).toBe('rest'));
    act(() => hook.result.current.setName('Mine'));
    return hook;
  }

  it('creates the source, saves, then activates and reports the accepted activation in order', async () => {
    const { result } = await unsavedRest();
    let outcome: Awaited<ReturnType<typeof result.current.saveAndEnable>> = null;
    await act(async () => {
      outcome = await result.current.saveAndEnable();
    });
    expect(outcome).toEqual({ id: 's1', state: 'active', errorCode: null });
    const order = [api.createConnectorSource, api.saveConnectorConfiguration, api.activateConnector].map(
      (fn) => fn.mock.invocationCallOrder[0],
    );
    expect(order).toEqual([...order].sort((a, b) => a - b));
    expect(api.activateConnector.mock.calls[0].slice(0, 4)).toEqual(['s1', 1, 'keep', undefined]);
    expect(result.current.revision).toBe(1);
    expect(result.current.dirty).toBe(false);
    expect(onChanged).toHaveBeenCalled();
    expect(result.current.busyAction).toBe('');
  });

  it('does not activate a key-required preset without a stored or typed key', async () => {
    api.createConnectorSource.mockResolvedValue({ id: 's1', generation: 1 });
    api.saveConnectorConfiguration.mockResolvedValue({ state: 'saved_not_active', error_code: null, desired_revision: 1 });
    api.getMonotonicConnectorConfiguration.mockResolvedValue(serverConfig('coingecko'));
    const { result } = mount();
    await waitFor(() => expect(result.current.catalog.length).toBeGreaterThan(0));
    act(() => result.current.selectProvider('coingecko', catalog[1]));
    act(() => result.current.setName('Coins'));
    await act(async () => {
      await result.current.saveAndEnable();
    });
    expect(result.current.error).toBe('providerSecretRequired');
    expect(api.activateConnector).not.toHaveBeenCalled();
  });

  it('reports a local-only refusal through the verify failure code and keeps the draft saved', async () => {
    const { result } = await unsavedRest();
    api.activateConnector.mockRejectedValue(new ApiError(409, 'local only', { code: 'local_only_source' }));
    await act(async () => {
      await result.current.runVerify();
    });
    expect(result.current.verify).toEqual({ phase: 'failed', code: 'local_only_source' });
    expect(result.current.error).toBe('');
    expect(result.current.notice).toBe(sourcesMessages['en-us'].savedInactive);
  });

  it('serializes actions: a second request while one runs is refused', async () => {
    const { result } = await unsavedRest();
    api.saveConnectorConfiguration.mockImplementation(() => new Promise(() => undefined));
    act(() => {
      void result.current.saveConfiguration();
    });
    await waitFor(() => expect(result.current.busyAction).toBe('save'));
    let second: unknown = 'pending';
    await act(async () => {
      second = await result.current.saveConfiguration();
    });
    expect(second).toBeNull();
    expect(api.saveConnectorConfiguration).toHaveBeenCalledTimes(1);
  });
});

describe('useConnectorEditor provider connections', () => {
  it('stops Google selection at the provider maximum without editing the draft', async () => {
    const ids = Array.from({ length: 10 }, (_, index) => `label-${index}`);
    api.getMonotonicConnectorConfiguration.mockResolvedValue(
      serverConfig('google_mail', { configuration: { ...baseConfig, google_mail_label_ids: ids } }),
    );
    const { result } = mount(existing('google_mail'));
    await waitFor(() => expect(result.current.configuration.google_mail_label_ids).toEqual(ids));
    act(() => result.current.toggleGoogleResource('extra', true));
    expect(result.current.googleDiscoveryError).toBe('limit');
    expect(result.current.configuration.google_mail_label_ids).toEqual(ids);
    act(() => result.current.toggleGoogleResource('label-0', false));
    expect(result.current.googleDiscoveryError).toBeNull();
    expect(result.current.configuration.google_mail_label_ids).toHaveLength(9);
  });

  it('disconnects Reddit against the saved revision and the current grant revision', async () => {
    api.getMonotonicConnectorConfiguration.mockResolvedValue(serverConfig('reddit'));
    api.disconnectRedditOAuth.mockResolvedValue(undefined);
    const { result } = mount(existing('reddit'));
    await waitFor(() => expect(result.current.redditStatusQuery.data?.state).toBe('ready'));
    await waitFor(() => expect(result.current.revision).toBe(1));
    await act(async () => {
      await result.current.disconnectReddit();
    });
    expect(api.disconnectRedditOAuth).toHaveBeenCalledWith('s1', 1, 1, 2, 't', expect.any(AbortSignal));
  });

  it('keeps Reddit connected when there is unsaved draft input', async () => {
    api.getMonotonicConnectorConfiguration.mockResolvedValue(serverConfig('reddit'));
    const { result } = mount(existing('reddit'));
    await waitFor(() => expect(result.current.revision).toBe(1));
    act(() => result.current.changeConfiguration('timezone', 'Asia/Tokyo'));
    await act(async () => {
      await result.current.disconnectReddit();
    });
    expect(api.disconnectRedditOAuth).not.toHaveBeenCalled();
  });

  const recoveryStatus = {
    state: 'reconciliation_required',
    recovery_available: true,
    operation_id: 'op-1',
    operation_kind: 'authorization',
    operation_source_id: 'origin',
    operation_source_generation: 3,
    operation_configuration_revision: 5,
    coordinator_state: 'reconciliation_required',
    coordinator_error_code: null,
    sync: { scope_sha256: null },
  };

  it('captures the recovery review then fences the confirmation on the reviewed operation', async () => {
    api.getMonotonicConnectorConfiguration.mockResolvedValue(serverConfig('github'));
    api.getGitHubStatus.mockResolvedValue(recoveryStatus);
    api.acknowledgeGitHubReconnect.mockResolvedValue(undefined);
    api.startGitHubOAuth.mockResolvedValue({ authorization_url: 'https://github.example/authorize' });
    const { result } = mount(existing('github'));
    await waitFor(() => expect(result.current.githubStatusQuery.data?.operation_id).toBe('op-1'));
    await waitFor(() => expect(result.current.revision).toBe(1));
    act(() => result.current.reviewGitHubRecovery());
    expect(result.current.githubRecoveryOpen).toBe(true);
    expect(result.current.githubRecoveryReview).toMatchObject({
      operationId: 'op-1',
      originSourceId: 'origin',
      sourceId: 's1',
      generation: 1,
      revision: 1,
    });
    await act(async () => {
      await result.current.confirmGitHubRecovery();
    });
    expect(api.acknowledgeGitHubReconnect).toHaveBeenCalledWith('s1', 'op-1', 't', expect.any(AbortSignal));
    expect(redirect.assignAuthorizationUrl).toHaveBeenCalledWith('https://github.example/authorize');
    expect(result.current.githubRecoveryOpen).toBe(false);
  });

  it('refuses a recovery confirmation when the operation changed after review', async () => {
    api.getMonotonicConnectorConfiguration.mockResolvedValue(serverConfig('github'));
    api.getGitHubStatus.mockResolvedValue(recoveryStatus);
    const { result } = mount(existing('github'));
    await waitFor(() => expect(result.current.githubStatusQuery.data?.operation_id).toBe('op-1'));
    await waitFor(() => expect(result.current.revision).toBe(1));
    act(() => result.current.reviewGitHubRecovery());
    api.getGitHubStatus.mockResolvedValue({ ...recoveryStatus, operation_id: 'op-2' });
    await act(async () => {
      await result.current.githubStatusQuery.refetch();
    });
    await waitFor(() => expect(result.current.githubStatusQuery.data?.operation_id).toBe('op-2'));
    await act(async () => {
      await result.current.confirmGitHubRecovery();
    });
    expect(api.acknowledgeGitHubReconnect).not.toHaveBeenCalled();
    expect(result.current.error).toBe('conflict');
    expect(result.current.githubRecoveryOpen).toBe(false);
    expect(result.current.githubRecoveryReview).toBeNull();
  });
});
