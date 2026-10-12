import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { NextIntlClientProvider } from 'next-intl';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { messages } from '@/core/messages';
import { N8nSettingsWorkspace } from './n8n-settings';

vi.mock('@/core/app-shell/workspace-shell', () => ({ useWorkspaceSession: () => ({ csrfToken: 'csrf' }) }));
vi.mock('@/core/query-provider', () => ({ useDisplayPreferences: () => ({ locale: 'en-us', timezone: 'UTC' }) }));

const read = { enabled: true, callback_base_url: 'https://os.example/api', configuration_revision: 2, service_url: null, api_key_configured: false, webhook_token_configured: true, service_url_source: 'environment', api_key_source: 'none', webhook_token_source: 'database' };
const row = { source_id: 's1', source_name: 'Feed A', source_type: 'rss', provider: null, workflow_id: 'w1', state: 'active', error_code: null, transition_phase: null, last_success_at: '2026-01-02T03:04:00Z', last_error_at: null, last_error_code: 'timeout', collected_at: null, next_due_at: null };
const json = (body: unknown) => Promise.resolve(new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } }));

/** Mounts the workspace with a fresh query client. */
const mount = () => render(<NextIntlClientProvider locale="en-US" messages={messages['en-us']}><QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><N8nSettingsWorkspace /></QueryClientProvider></NextIntlClientProvider>);

describe('n8n settings', () => {
  const fetchMock = vi.fn();
  /** Routes mocked fetches: settings, workflows list (configurable) and everything else echoes settings. */
  const route = (workflows: unknown = [row], settings: unknown = read) => fetchMock.mockImplementation((url: string) => String(url).includes('/connectors/n8n/workflows') ? (workflows instanceof Response ? Promise.resolve(workflows) : json(workflows)) : json(settings));
  beforeEach(() => { fetchMock.mockReset(); vi.stubGlobal('fetch', fetchMock); });

  it('toggling enabled dirties the form, warns and PUTs enabled', async () => {
    route([], { ...read, enabled: false });
    mount();
    const toggle = await screen.findByRole('switch', { name: 'Enable n8n integration' });
    expect(screen.getByText(/ingest, activation and wakes are blocked/)).toBeTruthy();
    expect((screen.getByRole('button', { name: 'Save' }) as HTMLButtonElement).disabled).toBe(true);
    await userEvent.click(toggle);
    expect(screen.queryByText(/ingest, activation and wakes are blocked/)).toBeNull();
    await userEvent.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(fetchMock.mock.calls.some(([, init]) => init?.method === 'PUT')).toBe(true));
    expect(JSON.parse(fetchMock.mock.calls.find(([, init]) => init?.method === 'PUT')![1].body).enabled).toBe(true);
  });

  it('copies the callback base url', async () => {
    route([]);
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true });
    mount();
    await userEvent.click(await screen.findByRole('button', { name: 'Copy' }));
    expect(writeText).toHaveBeenCalledWith('https://os.example/api');
    expect(await screen.findByText('Copied')).toBeTruthy();
  });

  it('renders workflow rows and runs an action with the CSRF header', async () => {
    route([row, { ...row, source_id: 's2', source_name: 'Feed B', last_success_at: null, last_error_code: null }]);
    mount();
    expect(await screen.findByText('Feed A')).toBeTruthy();
    expect(screen.getByText('Feed B')).toBeTruthy();
    expect(screen.getByText('Never')).toBeTruthy();
    expect(screen.getByText('timeout')).toBeTruthy();
    expect(screen.getAllByText('Active').length).toBeGreaterThan(0);
    expect((screen.getAllByRole('link', { name: 'Open source' })[0] as HTMLAnchorElement).getAttribute('href')).toBe('/settings/sources?source=s1');
    await userEvent.click(screen.getAllByRole('button', { name: 'Collect now' })[0]);
    await waitFor(() => expect(fetchMock.mock.calls.some(([url]) => String(url).endsWith('/connectors/sources/s1/collect'))).toBe(true));
    const call = fetchMock.mock.calls.find(([url]) => String(url).endsWith('/connectors/sources/s1/collect'))!;
    expect(new Headers(call[1].headers).get('X-CSRF-Token')).toBe('csrf');
  });

  it('disables row actions while n8n is disabled', async () => {
    route([row], { ...read, enabled: false });
    mount();
    expect((await screen.findByRole('button', { name: 'Collect now' }) as HTMLButtonElement).disabled).toBe(true);
  });

  it('shows empty and error states for workflows', async () => {
    route([]);
    const first = mount();
    expect(await screen.findByText('No n8n workflows yet.')).toBeTruthy();
    first.unmount();
    route(new Response('{}', { status: 500, headers: { 'Content-Type': 'application/json' } }));
    mount();
    expect(await screen.findByRole('alert')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Retry' })).toBeTruthy();
  });

  it('shows source badges and saves url plus a replaced api key', async () => {
    route([]);
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<NextIntlClientProvider locale="en-US" messages={messages['en-us']}><QueryClientProvider client={client}><N8nSettingsWorkspace /></QueryClientProvider></NextIntlClientProvider>);
    expect(await screen.findByText('From environment')).toBeTruthy();
    expect(screen.getByText('Saved here')).toBeTruthy();
    expect(screen.getByText(/re-provisioning/)).toBeTruthy();
    await userEvent.type(screen.getByLabelText('Service URL'), 'https://n8n.example');
    await userEvent.type(screen.getByLabelText('API key'), 'k1');
    await userEvent.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(fetchMock.mock.calls.some(([, init]) => init?.method === 'PUT')).toBe(true));
    const put = fetchMock.mock.calls.find(([, init]) => init?.method === 'PUT')!;
    expect(JSON.parse(put[1].body)).toEqual({ expected_revision: 2, service_url: 'https://n8n.example', api_key_action: 'replaced', api_key: 'k1', webhook_token_action: 'unchanged', webhook_token: null, enabled: true });
  });
});
