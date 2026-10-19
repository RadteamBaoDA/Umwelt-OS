import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { NextIntlClientProvider } from 'next-intl';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { messages } from '@/core/messages';
import { VizRouterCard } from './viz-router-card';

vi.mock('@/core/app-shell/workspace-shell', () => ({ useWorkspaceSession: () => ({ csrfToken: 'csrf' }) }));

const read = { configuration_revision: 1, kind: 'internal', provider_id: null, model: '', mode: 'json_schema', typesafe_key_configured: false, allow_remote_visualization_routing: false, destination_id: null };
const ai = { providers: [{ id: 'default', label: 'Gateway', local: false, models: [{ id: 'gpt-x', type: 'llm' }, { id: 'emb', type: 'embedding' }] }] };
const json = (body: unknown) => Promise.resolve(new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } }));
const mount = () => render(<NextIntlClientProvider locale="en-US" messages={messages['en-us']}><QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><VizRouterCard /></QueryClientProvider></NextIntlClientProvider>);

describe('visualization router card', () => {
  const fetchMock = vi.fn();
  beforeEach(() => { fetchMock.mockReset(); vi.stubGlobal('fetch', fetchMock); });

  it('defaults to the internal router with no external controls', async () => {
    fetchMock.mockImplementation((url: string) => json(String(url).includes('viz-router') ? read : ai));
    mount();
    expect(await screen.findByRole('combobox', { name: 'Router' })).toBeTruthy();
    expect(screen.queryByRole('checkbox')).toBeNull();
    expect((screen.getByRole('button', { name: 'Save' }) as HTMLButtonElement).disabled).toBe(true);
  });

  it('saves the LLM classifier with consent and mode, then tests routing', async () => {
    const saved = { ...read, configuration_revision: 2, kind: 'llm', provider_id: 'default', model: 'gpt-x', mode: 'forced_tool', allow_remote_visualization_routing: true };
    fetchMock.mockImplementation((url: string, init?: RequestInit) => {
      if (String(url).endsWith('/viz-router/test')) return json({ ok: true, category: 'table', mode: 'forced_tool', error: null });
      if (String(url).includes('viz-router')) return json(init?.method === 'PUT' ? saved : read);
      return json(ai);
    });
    mount();
    await userEvent.click(await screen.findByRole('combobox', { name: 'Router' }));
    await userEvent.click(await screen.findByRole('option', { name: 'LLM classifier' }));
    await userEvent.click(await screen.findByRole('combobox', { name: 'Model' }));
    expect(screen.queryByRole('option', { name: 'emb' })).toBeNull();
    await userEvent.click(screen.getByRole('option', { name: 'gpt-x' }));
    await userEvent.click(screen.getByRole('combobox', { name: 'Constraint mode' }));
    await userEvent.click(await screen.findByRole('option', { name: 'Forced tool call' }));
    await userEvent.click(screen.getByRole('checkbox'));
    await userEvent.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(fetchMock.mock.calls.some(([, init]) => init?.method === 'PUT')).toBe(true));
    const put = fetchMock.mock.calls.find(([, init]) => init?.method === 'PUT')!;
    expect(JSON.parse(put[1].body)).toEqual({ expected_revision: 1, kind: 'llm', provider_id: 'default', model: 'gpt-x', mode: 'forced_tool', typesafe_key_action: 'unchanged', typesafe_key: null, allow_remote_visualization_routing: true });
    await userEvent.click(await screen.findByRole('button', { name: 'Test routing' }));
    expect(await screen.findByText(/Routing works with Forced tool call: answered "table"/)).toBeTruthy();
  });

  it('shows a localized error code from Test routing and never renders the key', async () => {
    const typesafe = { ...read, kind: 'typesafe', typesafe_key_configured: true, allow_remote_visualization_routing: true };
    fetchMock.mockImplementation((url: string) => String(url).endsWith('/test') ? json({ ok: false, category: null, mode: null, error: 'auth_error' }) : json(String(url).includes('viz-router') ? typesafe : ai));
    mount();
    await userEvent.click(await screen.findByRole('button', { name: 'Test routing' }));
    expect(await screen.findByText('The provider rejected the credentials.')).toBeTruthy();
    expect((screen.getByLabelText('TypeSafe API key') as HTMLInputElement).value).toBe('');
  });
});
