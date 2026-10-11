import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { NextIntlClientProvider } from 'next-intl';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { messages } from '@/core/messages';
import { N8nSettingsWorkspace } from './n8n-settings';

vi.mock('@/core/app-shell/workspace-shell', () => ({ useWorkspaceSession: () => ({ csrfToken: 'csrf' }) }));

const read = { configuration_revision: 2, service_url: null, api_key_configured: false, webhook_token_configured: true, service_url_source: 'environment', api_key_source: 'none', webhook_token_source: 'database' };
const json = (body: unknown) => Promise.resolve(new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } }));

describe('n8n settings', () => {
  const fetchMock = vi.fn();
  beforeEach(() => { fetchMock.mockReset(); vi.stubGlobal('fetch', fetchMock); });

  it('shows source badges and saves url plus a replaced api key', async () => {
    fetchMock.mockImplementation((_url: string, init?: RequestInit) => json(init?.method === 'PUT' ? read : read));
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
    expect(JSON.parse(put[1].body)).toEqual({ expected_revision: 2, service_url: 'https://n8n.example', api_key_action: 'replaced', api_key: 'k1', webhook_token_action: 'unchanged', webhook_token: null });
  });
});
