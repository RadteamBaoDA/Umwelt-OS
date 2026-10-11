import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { NextIntlClientProvider } from 'next-intl';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { messages } from '@/core/messages';
import { WebSearchSettingsWorkspace } from './web-search-settings';

vi.mock('@/core/app-shell/workspace-shell', () => ({ useWorkspaceSession: () => ({ csrfToken: 'csrf' }) }));

const read = { configuration_revision: 3, provider: 'none', endpoint: null, credential_configured: false, allow_remote_web_search: false, destination_id: null };
const json = (body: unknown) => Promise.resolve(new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } }));

describe('web search settings', () => {
  const fetchMock = vi.fn();
  beforeEach(() => { fetchMock.mockReset(); vi.stubGlobal('fetch', fetchMock); });

  it('saves a keyless SearXNG provider with the expected revision', async () => {
    fetchMock.mockImplementation((_url: string, init?: RequestInit) => json(init?.method === 'PUT' ? { ...read, provider: 'searxng', endpoint: 'https://s.example/' } : read));
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<NextIntlClientProvider locale="en-US" messages={messages['en-us']}><QueryClientProvider client={client}><WebSearchSettingsWorkspace /></QueryClientProvider></NextIntlClientProvider>);
    await userEvent.click(await screen.findByRole('combobox', { name: 'Provider' }));
    await userEvent.click(await screen.findByRole('option', { name: 'SearXNG' }));
    expect(screen.queryByLabelText('API key')).toBeNull();
    await userEvent.type(screen.getByLabelText('Endpoint URL'), 'https://s.example/');
    await userEvent.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(fetchMock.mock.calls.some(([, init]) => init?.method === 'PUT')).toBe(true));
    const put = fetchMock.mock.calls.find(([, init]) => init?.method === 'PUT')!;
    expect(JSON.parse(put[1].body)).toEqual({ expected_revision: 3, provider: 'searxng', endpoint: 'https://s.example/', credential_action: 'unchanged', api_key: null, allow_remote_web_search: false });
  });
});
