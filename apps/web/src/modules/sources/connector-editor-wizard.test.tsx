import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { NextIntlClientProvider } from 'next-intl';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { sourcesMessages } from '@/core/messages/sources';
import type { ConnectorCatalogEntry, ConnectorConfiguration, Source } from './api';
import { ApiError } from '@/core/api';
import type { DataShape } from '@/core/data-shapes';
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
const shaped = (item: ConnectorCatalogEntry, data_shapes: string[]) => ({ ...item, data_shapes }) as ConnectorCatalogEntry;
const googleNews = { ...entry('google_news', 'Google News RSS', 'rss'), kind: 'preset', scope_fields: ['news_query', 'news_site', 'news_locale'],
  example_config: { ...{ timeout_seconds: 30, timezone: 'UTC', schedule_interval_minutes: 30, history_mode: 'returned_snapshot' }, news_query: 'Vietnam economy', news_site: 'any', news_locale: 'en-US' } } as unknown as ConnectorCatalogEntry;
const catalog = [entry('rest', 'REST API', 'api'), shaped(entry('rss', 'RSS / Atom', 'rss'), ['news']), shaped(entry('binance', 'Binance', 'api'), ['measurement']), entry('coingecko', 'CoinGecko', 'api'), entry('telegram', 'Telegram', 'api'), googleNews];
const baseConfig = { timeout_seconds: 30, timezone: 'UTC', schedule_interval_minutes: 30, history_mode: 'returned_snapshot' };
const serverConfig = (provider: string | null): ConnectorConfiguration => ({
  source_id: 's1', source_type: 'api', source_generation: 1, provider, configuration: baseConfig, expected_revision: 0, auth_method: 'none',
  auth_header_name: null, desired_enabled: false, activation_state: 'saved_not_active', activation_error_code: null,
  provider_credential_configured: false, provider_credential_state: null,
} as unknown as ConnectorConfiguration);

let actions: { current: EditorActions | null };
let snapshot: EditorSnapshot | null;

/** Mounts the embedded editor and returns a helper that switches the wizard step like the shell does. */
function mount(initial: EditorStep = 'provider', shapeFilter?: DataShape[], source?: Source) {
  actions = { current: null };
  snapshot = null;
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const ui = (step: EditorStep) => <QueryClientProvider client={client}>
    <NextIntlClientProvider locale="en-US" messages={{ sources: sourcesMessages['en-us'] }}>
      <ConnectorEditor source={source} shapeFilter={shapeFilter} onClose={() => undefined} onChanged={() => undefined} registerTransitionGuard={() => undefined} step={step} actionsRef={actions} onSnapshot={(s) => { snapshot = s; }} />
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

  it('searches providers and shows the no-key group first', async () => {
    mount();
    expect((await screen.findAllByRole('group'))[0].getAttribute('aria-label')).toBe('No key needed');
    const search = screen.getByLabelText('Search providers');
    await userEvent.type(search, 'coingeck');
    expect(screen.getByRole('button', { name: /^CoinGecko/ })).toBeTruthy();
    expect(screen.queryByRole('button', { name: /^REST API/ })).toBeNull();
    await userEvent.clear(search);
    await userEvent.type(search, 'qqqq');
    expect(screen.getByText('No providers match your search.')).toBeTruthy();
  });

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

  it('prefills the catalog example scope and blocks Choose when a required field is cleared', async () => {
    const h = mount();
    await pick(h, 'Google News', 'google_news');
    h.goto('choose');
    const query = document.getElementById('source-scope-news_query') as HTMLInputElement;
    expect(query.value).toBe('Vietnam economy');
    await waitFor(() => expect(snapshot?.chooseOk).toBe(true));
    await userEvent.clear(query);
    await waitFor(() => expect(snapshot?.chooseOk).toBe(false));
    await userEvent.type(query, 'oil price');
    await waitFor(() => expect(snapshot?.chooseOk).toBe(true));
  });

  it('reopens an unsaved source with defaults and names each rejected field by its label', async () => {
    // A source created at step 1 but never saved reloads with an empty configuration at revision 0.
    api.getMonotonicConnectorConfiguration.mockResolvedValue({ ...serverConfig('google_news'), source_type: 'rss', configuration: {} });
    api.validateDraftConnector.mockRejectedValue(new ApiError(422, 'Draft connector configuration is invalid: Missing required fields: news_site, history_mode'));
    mount('choose', undefined, { id: 's1', name: 'News', type: 'rss', provider: 'google_news', status: 'active' } as unknown as Source);
    await userEvent.click(await screen.findByRole('button', { name: 'Validate draft' }));
    await waitFor(() => expect(api.validateDraftConnector).toHaveBeenCalled());
    const sent = api.validateDraftConnector.mock.calls[0][1].configuration;
    expect(sent).toMatchObject({ news_query: 'Vietnam economy', news_site: 'any', news_locale: 'en-US', history_mode: 'returned_snapshot' });
    const issues = await screen.findByTestId('scope-issues');
    expect(issues.textContent).toContain('Publisher site: required');
    expect(screen.getByText(/Fix these fields/)).toBeTruthy();
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

  it('shape filter narrows the provider grid and shows the empty state when nothing matches', async () => {
    mount('provider', ['measurement']);
    expect(await screen.findByRole('button', { name: /^Binance/ })).toBeTruthy();
    expect(screen.queryByRole('button', { name: /^RSS/ })).toBeNull();
    cleanup();
    mount('provider', ['video']);
    expect(await screen.findByText(sourcesMessages['en-us'].shapeFilterNone)).toBeTruthy();
  });
});
