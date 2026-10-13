import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { NextIntlClientProvider } from 'next-intl';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { messages } from '@/core/messages';
import { AISettingsWorkspace } from './ai-settings';

vi.mock('@/core/app-shell/workspace-shell', () => ({ useWorkspaceSession: () => ({ csrfToken: 'csrf' }) }));
vi.mock('@/modules/agents/agent-settings', () => ({ AgentSettingsWorkspace: () => null }));
vi.mock('@/modules/automations/rule-list', () => ({ AutomationSettings: () => null }));

const provider = { id: 'ol', kind: 'ollama', label: 'Ollama', base_url: 'http://localhost:11434/v1', destination_id: 'local:x', credential_configured: false, local: true, policy_denied: false, models: [{ id: 'llama3', type: 'llm' }, { id: 'nomic', type: 'embedding' }] };
const mapping = { model: '', version: null, destination: 'unknown' };
const read = { configuration_revision: 1, providers: [provider], web_search_provider: 'none', web_search_endpoint: null, web_search_credential_configured: false, chat_alias: 'reasoning-large', brief_alias: 'reasoning-small', aliases: { embedding: { ...mapping, model: 'nomic', provider_id: 'ol' } }, capabilities: [], privacy: { allow_remote_reasoning: false, allow_remote_embeddings: false, allow_remote_web_search: false, reasoning_destinations: [], embedding_destinations: [], web_search_destinations: [] }, request_timeout_seconds: 20 };
const json = (body: unknown) => Promise.resolve(new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } }));

describe('AI settings models', () => {
  const fetchMock = vi.fn();
  beforeEach(() => { fetchMock.mockReset(); vi.stubGlobal('fetch', fetchMock); sessionStorage.clear(); });

  it('hides unused slots, lists only type-matching discovered models and fills dimensions from a probe', async () => {
    fetchMock.mockImplementation((url: string) => json(url.includes('draft-test') ? { alias: 'embedding', capability: 'embeddings', result: 'supported', dimensions: 768 } : read));
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<NextIntlClientProvider locale="en-US" messages={messages['en-us']}><QueryClientProvider client={client}><AISettingsWorkspace /></QueryClientProvider></NextIntlClientProvider>);
    await userEvent.click(await screen.findByText('Model details (advanced)'));
    expect(screen.queryByRole('heading', { name: 'vision' })).toBeNull();
    expect(screen.queryByRole('heading', { name: 'local-private' })).toBeNull();
    expect(screen.getByRole('heading', { name: 'embedding' })).toBeTruthy();
    await userEvent.click(screen.getAllByRole('combobox', { name: 'Model ID' })[3]);
    expect(screen.getByRole('option', { name: 'nomic' })).toBeTruthy();
    expect(screen.queryByRole('option', { name: 'llama3' })).toBeNull();
    await userEvent.keyboard('{Escape}');
    await userEvent.click(screen.getAllByRole('button', { name: 'Probe draft' })[3]);
    await waitFor(() => expect(screen.getByText(/768/)).toBeTruthy());
  });
});
