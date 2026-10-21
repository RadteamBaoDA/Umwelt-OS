import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { NextIntlClientProvider } from 'next-intl';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { messages } from '@/core/messages';
import { AISettingsWorkspace, autoFillDefaults } from './ai-settings';

vi.mock('@/core/app-shell/workspace-shell', () => ({ useWorkspaceSession: () => ({ csrfToken: 'csrf' }) }));
vi.mock('@/modules/agents/agent-settings', () => ({ AgentSettingsWorkspace: () => null }));
vi.mock('@/modules/automations/rule-list', () => ({ AutomationSettings: () => null }));

const provider = { id: 'ol', kind: 'ollama', label: 'Ollama', base_url: 'http://localhost:11434/v1', destination_id: 'local:x', credential_configured: false, local: true, policy_denied: false, models: [{ id: 'llama3', type: 'llm' }, { id: 'nomic', type: 'embedding' }] };
const mapping = { model: '', version: null, destination: 'unknown' };
const read = { configuration_revision: 1, providers: [provider], web_search_provider: 'none', web_search_endpoint: null, web_search_credential_configured: false, defaults: { embedding: { ...mapping, model: 'nomic', provider_id: 'ol' } }, capabilities: [], privacy: { allow_remote_reasoning: false, allow_remote_embeddings: false, allow_remote_web_search: false, allow_widget_remote_media: false, reasoning_destinations: [], embedding_destinations: [], web_search_destinations: [] }, request_timeout_seconds: 20 };
const json = (body: unknown) => Promise.resolve(new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } }));

describe('AI settings models', () => {
  const fetchMock = vi.fn();
  beforeEach(() => { fetchMock.mockReset(); vi.stubGlobal('fetch', fetchMock); sessionStorage.clear(); });

  it('shows one Models area with defaults, no legacy model names, and probes dimensions when an embedding model is picked', async () => {
    fetchMock.mockImplementation((url: string) => json(url.includes('draft-test') ? { role: 'embedding', capability: 'embeddings', result: 'supported', dimensions: 768 } : { ...read, defaults: {} }));
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<NextIntlClientProvider locale="en-US" messages={messages['en-us']}><QueryClientProvider client={client}><AISettingsWorkspace /></QueryClientProvider></NextIntlClientProvider>);
    await screen.findByRole('table', { name: 'Models' });
    expect(screen.queryByText(/reasoning-|Advanced: alias/)).toBeNull();
    await userEvent.click(screen.getByRole('combobox', { name: 'Embedding model' }));
    await userEvent.click(screen.getByRole('option', { name: 'Ollama · nomic' }));
    await waitFor(() => expect(fetchMock.mock.calls.some(([url]) => String(url).includes('/models/draft-test'))).toBe(true));
    await waitFor(() => expect((screen.getByRole('button', { name: 'Save AI settings' }) as HTMLButtonElement).disabled).toBe(false));
  });

  it('tests a saved chat model through the role endpoint (streaming then tools) so chat evidence is stored', async () => {
    fetchMock.mockImplementation((url: string) => json(String(url).includes('/models/chat/test') ? { role: 'chat', capability: 'streaming', result: 'supported' } : read));
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<NextIntlClientProvider locale="en-US" messages={messages['en-us']}><QueryClientProvider client={client}><AISettingsWorkspace /></QueryClientProvider></NextIntlClientProvider>);
    await screen.findByRole('table', { name: 'Models' });
    await userEvent.click(screen.getByRole('button', { name: 'Test model llama3' }));
    await waitFor(() => expect(fetchMock.mock.calls.filter(([url]) => String(url).includes('/models/chat/test')).length).toBe(2));
    const bodies = fetchMock.mock.calls.filter(([url]) => String(url).includes('/models/chat/test')).map(([, init]) => JSON.parse(String(init.body)));
    expect(bodies).toEqual([{ capability: 'streaming', provider_id: 'ol', model: 'llama3' }, { capability: 'tools', provider_id: 'ol', model: 'llama3' }]);
    expect(fetchMock.mock.calls.some(([url]) => String(url).includes('draft-test'))).toBe(false);
  });

  it('auto-fills only empty chat/brief defaults (and vision when flagged) for a new Chat model', () => {
    const base = { ...provider, kind: 'ollama' as const };
    const prev = [{ ...base, models: [] }];
    const next = [{ ...base, models: [{ id: 'gpt', type: 'llm' as const, vision: true }, { id: 'emb', type: 'embedding' as const }] }];
    const filled = autoFillDefaults(prev, next, { brief: { ...mapping, model: 'keep', destination: 'unknown' as const } });
    expect(filled.chat?.model).toBe('gpt');
    expect(filled.chat?.provider_id).toBe('ol');
    expect(filled.brief?.model).toBe('keep');
    expect(filled.vision?.model).toBe('gpt');
    expect(filled.embedding).toBeUndefined();
    expect(filled.background).toBeUndefined();
    expect(autoFillDefaults(next, next, {})).toEqual({});
  });
});
