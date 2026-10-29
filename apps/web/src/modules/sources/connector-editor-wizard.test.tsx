import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { NextIntlClientProvider } from 'next-intl';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { sourcesMessages } from '@/core/messages/sources';
import type { ConnectorCatalogEntry, ConnectorConfiguration } from './api';
import type { EditorActions, EditorSnapshot, EditorStep } from './connector-editor';

vi.mock('@/core/app-shell/workspace-shell', () => ({ useWorkspaceSession: () => ({ csrfToken: 't' }) }));
vi.mock('@/core/query-provider', () => ({ useDisplayPreferences: () => ({ locale: 'en-us', timezone: 'UTC', authGeneration: 1, isCurrentGeneration: () => true }) }));
vi.mock('@/core/guarded-navigation', () => ({ useGuardedNavigation: () => ({ registerLeaveGuard: () => () => undefined }) }));
vi.mock('@/core/workspace-context', () => ({ useWorkspace: () => ({ isOwner: true }) }));

const api = vi.hoisted(() => ({
  getConnectorCatalog: vi.fn(), listBuiltinSources: vi.fn(), createConnectorSource: vi.fn(), getMonotonicConnectorConfiguration: vi.fn(),
  getConnectorActivation: vi.fn(), getProviderTerms: vi.fn(), validateDraftConnector: vi.fn(),
}));
vi.mock('./api', async (original) => ({ ...(await original<typeof import('./api')>()), ...api }));

import { ConnectorEditor } from './connector-editor';

const entry = (provider_id: string, label: string, source_type: 'rss' | 'api' | 'web'): ConnectorCatalogEntry => ({
  provider_id, label, source_type, availability: provider_id === 'coingecko' || provider_id === 'telegram' ? 'requires_credentials' : 'available',
  code_available: true, auth_methods: provider_id === 'telegram' ? ['telegram_bot_token'] : ['none'],
  key_fields: provider_id === 'coingecko' ? ['api_key'] : [], scope_fields: [], example_config: {},
} as unknown as ConnectorCatalogEntry);
const catalog = [entry('rest', 'REST API', 'api'), entry('rss', 'RSS / Atom', 'rss'), entry('binance', 'Binance', 'api'), entry('coingecko', 'CoinGecko', 'api'), entry('telegram', 'Telegram', 'api')];
const baseConfig = { timeout_seconds: 30, timezone: 'UTC', schedule_interval_minutes: 30, history_mode: 'returned_snapshot' };
const serverConfig = (provider: string | null): ConnectorConfiguration => ({
  source_id: 's1', source_type: 'api', source_generation: 1, provider, configuration: baseConfig, expected_revision: 0, auth_method: 'none',
  auth_header_name: null, desired_enabled: false, activation_state: 'saved_not_active', activation_error_code: null,
  provider_credential_configured: false, provider_credential_state: null,
} as unknown as ConnectorConfiguration);

let actions: { current: EditorActions | null };
let snapshot: EditorSnapshot | null;

/** Mounts the embedded editor and returns a helper that switches the wizard step like the shell does. */
function mount(initial: EditorStep = 'provider') {
  actions = { current: null };
  snapshot = null;
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const ui = (step: EditorStep) => <QueryClientProvider client={client}>
    <NextIntlClientProvider locale="en-US" messages={{ sources: sourcesMessages['en-us'] }}>
      <ConnectorEditor onClose={() => undefined} onChanged={() => undefined} registerTransitionGuard={() => undefined} step={step} actionsRef={actions} onSnapshot={(s) => { snapshot = s; }} />
    </NextIntlClientProvider>
  </QueryClientProvider>;
  const view = render(ui(initial));
  return { goto: (step: EditorStep) => view.rerender(ui(step)) };
}

/** Selects a provider card, names the source and creates it so the connect/choose sections mount. */
async function pick(h: ReturnType<typeof mount>, label: string, provider: string) {
  api.getMonotonicConnectorConfiguration.mockResolvedValue(serverConfig(provider === 'rest' || provider === 'rss' ? null : provider));
  await userEvent.click(await screen.findByRole('button', { name: new RegExp(`^${label}`) }));
  await userEvent.clear(screen.getByLabelText('Source name'));
  await userEvent.type(screen.getByLabelText('Source name'), 'x');
  await act(async () => { await actions.current!.createSource(); });
  h.goto('connect');
}

describe('ConnectorEditor wizard gating', () => {
  beforeEach(() => {
    Object.values(api).forEach((fn) => fn.mockReset());
    api.getConnectorCatalog.mockResolvedValue(catalog);
    api.listBuiltinSources.mockResolvedValue([]);
    api.createConnectorSource.mockResolvedValue({ id: 's1', generation: 1 });
    api.getConnectorActivation.mockResolvedValue({ state: 'saved_not_active', error_code: null });
    api.getProviderTerms.mockResolvedValue({ eligible: true });
    api.validateDraftConnector.mockResolvedValue({ source_id: 's1', source_generation: 1, expected_revision: 0, validated_at: '2026-01-01T00:00:00Z', validation_status: 'valid', checks: [] });
  });
  afterEach(cleanup);

  it('puts Validate in the Choose step only', async () => {
    const h = mount();
    await pick(h, 'RSS / Atom', 'rss');
    expect(screen.queryByRole('button', { name: 'Validate draft' })).toBeNull();
    h.goto('choose');
    await userEvent.click(await screen.findByRole('button', { name: 'Validate draft' }));
    await waitFor(() => expect(api.validateDraftConnector).toHaveBeenCalledTimes(1));
    expect(api.validateDraftConnector.mock.calls[0][1].auth_method).toBe('none');
  });

  it.each([['REST API', 'rest'], ['RSS / Atom', 'rss'], ['Binance prices', 'binance']])('does not gate Connect on validation for %s', async (label, id) => {
    const h = mount();
    await pick(h, label, id);
    await waitFor(() => expect(snapshot?.connectOk).toBe(true));
  });

  it('requires a key for a keyed provider', async () => {
    const h = mount();
    await pick(h, 'CoinGecko', 'coingecko');
    await waitFor(() => expect(snapshot?.connectOk).toBe(false));
    await userEvent.type(document.getElementById('source-provider-key')!, 'dummy-key');
    await waitFor(() => expect(snapshot?.connectOk).toBe(true));
  });

  it('requires a bot token for Telegram', async () => {
    const h = mount();
    await pick(h, 'Telegram', 'telegram');
    await waitFor(() => expect(snapshot?.connectOk).toBe(false));
    await userEvent.type(document.getElementById('source-telegram-token')!, '123:abc');
    await waitFor(() => expect(snapshot?.connectOk).toBe(true));
  });

  it('sends no header auth or secret when a native provider is validated', async () => {
    const h = mount();
    await pick(h, 'Binance prices', 'binance');
    h.goto('choose');
    await userEvent.click(await screen.findByRole('button', { name: 'Validate draft' }));
    await waitFor(() => expect(api.validateDraftConnector).toHaveBeenCalled());
    const body = api.validateDraftConnector.mock.calls[0][1];
    expect(body.auth_method).toBe('none');
    expect(body.auth_header_name).toBeUndefined();
    expect(body.secret).toBeUndefined();
  });
});
