import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { NextIntlClientProvider } from 'next-intl';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { messages } from '@/core/messages';
import { VizRouterCard } from './viz-router-card';

vi.mock('@/core/app-shell/workspace-shell', () => ({ useWorkspaceSession: () => ({ csrfToken: 'csrf' }) }));

const read = { configuration_revision: 1, kind: 'internal', provider_id: null, model: '', mode: 'json_schema', api: 'chat', typesafe_key_configured: false, openrouter_key_configured: false, cloudflare_key_configured: false, account_id: null, gateway_url: null, allow_remote_visualization_routing: false, destination_id: null };
const ai = { providers: [{ id: 'default', label: 'Gateway', local: false, models: [{ id: 'gpt-x', type: 'llm' }, { id: 'emb', type: 'embedding' }, { id: 'dec-1', type: 'decision' }] }] };
const json = (body: unknown) => Promise.resolve(new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } }));
const mount = (locale: 'en-us' | 'vi-vi' = 'en-us', onError?: (error: unknown) => void) => render(<NextIntlClientProvider locale={locale === 'en-us' ? 'en-US' : 'vi-VN'} messages={messages[locale]} onError={onError}><QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><VizRouterCard /></QueryClientProvider></NextIntlClientProvider>);

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
    await userEvent.click(await screen.findByRole('option', { name: 'LLM classifier (my providers)' }));
    await userEvent.click(await screen.findByRole('combobox', { name: 'Model' }));
    expect(screen.queryByRole('option', { name: 'emb' })).toBeNull();
    expect(screen.getAllByRole('option').map((o) => o.textContent)).toEqual(['dec-1', 'gpt-x']);
    await userEvent.click(screen.getByRole('option', { name: 'gpt-x' }));
    await userEvent.click(screen.getByRole('combobox', { name: 'Constraint mode' }));
    await userEvent.click(await screen.findByRole('option', { name: 'Forced tool call' }));
    await userEvent.click(screen.getByRole('checkbox'));
    await userEvent.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(fetchMock.mock.calls.some(([, init]) => init?.method === 'PUT')).toBe(true));
    const put = fetchMock.mock.calls.find(([, init]) => init?.method === 'PUT')!;
    expect(JSON.parse(put[1].body)).toEqual({ expected_revision: 1, kind: 'llm', provider_id: 'default', model: 'gpt-x', mode: 'forced_tool', api: 'chat', typesafe_key_action: 'unchanged', typesafe_key: null, api_key_action: 'unchanged', api_key: null, account_id: null, gateway_url: null, allow_remote_visualization_routing: true });
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

  it.each([
    ['en-us', 401, 'Please sign in again to continue.'],
    ['en-us', 403, 'This action is not allowed for this account.'],
    ['en-us', 409, 'Settings changed elsewhere. Reload and try again.'],
    ['en-us', 500, 'The service is unavailable. Try again shortly.'],
    ['en-us', 400, 'The request failed.'],
    ['vi-vi', 401, 'Hãy đăng nhập lại để tiếp tục.'],
    ['vi-vi', 403, 'Tài khoản này không được phép thực hiện thao tác này.'],
    ['vi-vi', 409, 'Cài đặt đã thay đổi ở nơi khác. Hãy tải lại và thử lại.'],
    ['vi-vi', 500, 'Dịch vụ không khả dụng. Hãy thử lại sau.'],
    ['vi-vi', 400, 'Yêu cầu thất bại.'],
  ] as const)('localizes %s HTTP %i test failures without missing messages', async (locale, status, expected) => {
    const typesafe = { ...read, kind: 'typesafe', typesafe_key_configured: true, allow_remote_visualization_routing: true };
    fetchMock.mockImplementation((url: string) => String(url).endsWith('/viz-router/test')
      ? Promise.resolve(new Response(JSON.stringify({ detail: 'Request failed' }), { status, headers: { 'Content-Type': 'application/json' } }))
      : json(String(url).includes('viz-router') ? typesafe : ai));
    const onError = vi.fn();
    mount(locale, onError);
    await userEvent.click(await screen.findByRole('button', { name: messages[locale].vizRouter.test }));
    expect(await screen.findByRole('alert')).toHaveTextContent(expected);
    expect(onError).not.toHaveBeenCalled();
  });

  it('saves OpenRouter with its own write-only key and validates required fields', async () => {
    const saved = { ...read, configuration_revision: 2, kind: 'openrouter', model: 'openrouter/auto', openrouter_key_configured: true, allow_remote_visualization_routing: true };
    fetchMock.mockImplementation((url: string, init?: RequestInit) => json(String(url).includes('viz-router') ? (init?.method === 'PUT' ? saved : read) : ai));
    mount();
    await userEvent.click(await screen.findByRole('combobox', { name: 'Router' }));
    await userEvent.click(await screen.findByRole('option', { name: 'OpenRouter' }));
    const save = screen.getByRole('button', { name: 'Save' }) as HTMLButtonElement;
    expect(save.disabled).toBe(true);
    await userEvent.type(screen.getByLabelText('Model'), 'openrouter/auto');
    expect(save.disabled).toBe(true); // key still missing
    await userEvent.type(screen.getByLabelText('OpenRouter API key'), 'or-secret');
    await userEvent.click(screen.getByRole('checkbox'));
    expect(save.disabled).toBe(false);
    await userEvent.click(save);
    await waitFor(() => expect(fetchMock.mock.calls.some(([, init]) => init?.method === 'PUT')).toBe(true));
    const body = JSON.parse(fetchMock.mock.calls.find(([, init]) => init?.method === 'PUT')![1].body);
    expect(body).toMatchObject({ kind: 'openrouter', model: 'openrouter/auto', mode: 'json_schema', api: 'decisions', api_key_action: 'replaced', api_key: 'or-secret', typesafe_key: null, account_id: null, allow_remote_visualization_routing: true });
  });

  it('loads Decision models into the OpenRouter model list and hides the constraint mode', async () => {
    fetchMock.mockImplementation((url: string) => {
      if (String(url).endsWith('/viz-router/decision-models')) return json({ ok: true, models: [{ id: 'vendor/decider-1', input_modalities: ['text'] }], error: null });
      return json(String(url).includes('viz-router') ? read : ai);
    });
    mount();
    await userEvent.click(await screen.findByRole('combobox', { name: 'Router' }));
    await userEvent.click(await screen.findByRole('option', { name: 'OpenRouter' }));
    expect(screen.queryByRole('combobox', { name: 'Constraint mode' })).toBeNull();
    const load = screen.getByRole('button', { name: 'Load decision models' }) as HTMLButtonElement;
    expect(load.disabled).toBe(true); // no key yet
    await userEvent.type(screen.getByLabelText('OpenRouter API key'), 'or-secret');
    await userEvent.click(load);
    expect(await screen.findByText(/1 decision models loaded/)).toBeTruthy();
    const call = fetchMock.mock.calls.find(([url]) => String(url).endsWith('/decision-models'))!;
    expect(JSON.parse(call[1].body)).toEqual({ api_key: 'or-secret' });
    expect(document.querySelector('datalist option[value="vendor/decider-1"]')).toBeTruthy();
    expect(document.querySelector('datalist option[value="cloudflare/clef-flash"]')).toBeTruthy();
    await userEvent.click(screen.getByRole('combobox', { name: 'API' }));
    await userEvent.click(await screen.findByRole('option', { name: 'Chat completions' }));
    expect(screen.getByRole('combobox', { name: 'Constraint mode' })).toBeTruthy();
  });

  it('requires a valid Cloudflare account id or an https gateway URL', async () => {
    fetchMock.mockImplementation((url: string) => json(String(url).includes('viz-router') ? { ...read, cloudflare_key_configured: true } : ai));
    mount();
    await userEvent.click(await screen.findByRole('combobox', { name: 'Router' }));
    await userEvent.click(await screen.findByRole('option', { name: 'Cloudflare Workers AI' }));
    const save = screen.getByRole('button', { name: 'Save' }) as HTMLButtonElement;
    await userEvent.type(screen.getByLabelText('Model'), '@cf/meta/llama-3.1-8b-instruct');
    await userEvent.type(screen.getByLabelText('Cloudflare API token'), 'cf-token');
    await userEvent.type(screen.getByLabelText(/Cloudflare account ID/), 'not-hex');
    expect(save.disabled).toBe(true);
    await userEvent.clear(screen.getByLabelText(/Cloudflare account ID/));
    await userEvent.type(screen.getByLabelText(/Cloudflare account ID/), '0123456789abcdef0123456789abcdef');
    expect(save.disabled).toBe(false);
  });
});
