import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { NextIntlClientProvider } from 'next-intl';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { messages } from '@/core/messages';
import { WebSearchSettingsWorkspace } from './web-search-settings';

vi.mock('@/core/app-shell/workspace-shell', () => ({ useWorkspaceSession: () => ({ csrfToken: 'csrf' }) }));

const read = { configuration_revision: 3, providers: [], allow_remote_web_search: false };
const json = (body: unknown) => Promise.resolve(new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } }));

describe('web search settings', () => {
  const fetchMock = vi.fn();
  beforeEach(() => { fetchMock.mockReset(); vi.stubGlobal('fetch', fetchMock); });

  it('saves a keyless SearXNG provider with the expected revision', async () => {
    fetchMock.mockImplementation((_url: string, init?: RequestInit) => json(init?.method === 'PUT' ? { ...read, providers: [{ provider: 'searxng', endpoint: 'https://s.example/', credential_configured: false, destination_id: 's' }] } : read));
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<NextIntlClientProvider locale="en-US" messages={messages['en-us']}><QueryClientProvider client={client}><WebSearchSettingsWorkspace /></QueryClientProvider></NextIntlClientProvider>);
    await userEvent.click(await screen.findByRole('button', { name: 'Add provider' }));
    await userEvent.click(screen.getByRole('combobox', { name: 'Provider 1' }));
    await userEvent.click(await screen.findByRole('option', { name: 'SearXNG' }));
    expect(screen.queryByLabelText('API key')).toBeNull();
    await userEvent.type(screen.getByLabelText('Endpoint URL'), 'https://s.example/');
    await userEvent.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(fetchMock.mock.calls.some(([, init]) => init?.method === 'PUT')).toBe(true));
    const put = fetchMock.mock.calls.find(([, init]) => init?.method === 'PUT')!;
    expect(JSON.parse(put[1].body)).toEqual({ expected_revision: 3, providers: [{ provider: 'searxng', endpoint: 'https://s.example/', credential_action: 'unchanged', api_key: null }], allow_remote_web_search: false });
  });

  it('keeps new keys with their providers when reordered', async () => {
    fetchMock.mockImplementation(() => json(read));
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<NextIntlClientProvider locale="en-US" messages={messages['en-us']}><QueryClientProvider client={client}><WebSearchSettingsWorkspace /></QueryClientProvider></NextIntlClientProvider>);
    await userEvent.click(await screen.findByRole('button', { name: 'Add provider' }));
    await userEvent.type(screen.getByLabelText('Endpoint URL'), 'https://first.example/');
    await userEvent.type(screen.getByLabelText('API key'), 'first-key');
    await userEvent.click(screen.getByRole('button', { name: 'Add provider' }));
    await userEvent.click(screen.getByRole('combobox', { name: 'Provider 2' }));
    await userEvent.click(await screen.findByRole('option', { name: 'OmniRoute' }));
    await userEvent.type(screen.getAllByLabelText('Endpoint URL')[1], 'https://second.example/v1');
    await userEvent.type(screen.getAllByLabelText('API key')[1], 'second-key');
    await userEvent.click(screen.getByRole('button', { name: 'Move provider 2 up' }));
    await userEvent.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(fetchMock.mock.calls.some(([, init]) => init?.method === 'PUT')).toBe(true));
    const put = fetchMock.mock.calls.find(([, init]) => init?.method === 'PUT')!;
    expect(JSON.parse(put[1].body).providers).toEqual([
      { provider: 'omniroute', endpoint: 'https://second.example/v1', credential_action: 'replaced', api_key: 'second-key' },
      { provider: 'tavily', endpoint: 'https://first.example/', credential_action: 'replaced', api_key: 'first-key' },
    ]);
  });

  it('uses the stored key for a test and discards a pending result after reorder', async () => {
    let finishTest!: (value: Response) => void;
    const configured = { ...read, providers: [
      { provider: 'brave', endpoint: 'https://first.example/', credential_configured: true, destination_id: 'first' },
      { provider: 'searxng', endpoint: 'https://second.example/', credential_configured: false, destination_id: 'second' },
    ] };
    fetchMock.mockImplementation((url: string) => url.endsWith('/test')
      ? new Promise<Response>((resolve) => { finishTest = resolve; }) : json(configured));
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<NextIntlClientProvider locale="en-US" messages={messages['en-us']}><QueryClientProvider client={client}><WebSearchSettingsWorkspace /></QueryClientProvider></NextIntlClientProvider>);
    await userEvent.click((await screen.findAllByRole('button', { name: 'Test connection' }))[0]);
    const request = fetchMock.mock.calls.find(([url]) => url.endsWith('/test'))!;
    expect(JSON.parse(request[1].body).api_key).toBe('');
    await userEvent.click(screen.getByRole('button', { name: 'Move provider 2 up' }));
    await act(async () => { finishTest(new Response(JSON.stringify({ ok: true, error: null, detail: 5 }), { status: 200, headers: { 'Content-Type': 'application/json' } })); });
    expect(screen.queryByText('Connection works (5 results).')).toBeNull();
  });

  it('saves with the revision at the start of editing after a background refresh', async () => {
    fetchMock.mockImplementation(() => json(read));
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<NextIntlClientProvider locale="en-US" messages={messages['en-us']}><QueryClientProvider client={client}><WebSearchSettingsWorkspace /></QueryClientProvider></NextIntlClientProvider>);
    await userEvent.click(await screen.findByRole('button', { name: 'Add provider' }));
    await userEvent.click(screen.getByRole('combobox', { name: 'Provider 1' }));
    await userEvent.click(await screen.findByRole('option', { name: 'SearXNG' }));
    await userEvent.type(screen.getByLabelText('Endpoint URL'), 'https://s.example/');
    act(() => client.setQueryData(['web-search-settings'], { ...read, configuration_revision: 4 }));
    await userEvent.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(fetchMock.mock.calls.some(([, init]) => init?.method === 'PUT')).toBe(true));
    const put = fetchMock.mock.calls.find(([, init]) => init?.method === 'PUT')!;
    expect(JSON.parse(put[1].body).expected_revision).toBe(3);
  });
});
