import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { NextIntlClientProvider, useTranslations } from 'next-intl';
import { describe, expect, it, vi } from 'vitest';
import { messages } from '@/core/messages';
import { mappingProviderId, guessModelType, maxProviders, newProvider, parseDraftSnapshot, ProviderEditor, type ProviderRead } from './provider-editor';

const cloud: ProviderRead = { id: 'default', kind: 'openai', label: 'OpenAI', base_url: 'https://api.openai.com/v1', destination_id: 'remote:x', credential_configured: true, local: false, policy_denied: false, models: [{ id: 'gpt-4o', type: 'llm' }] };
const local: ProviderRead = { ...cloud, id: 'ollama-1', kind: 'ollama', label: 'Ollama', base_url: 'http://localhost:11434/v1', local: true, policy_denied: true, credential_configured: false };

function Harness({ providers, onChange = vi.fn(), onDiscover = vi.fn(), discovered = {} }: { providers: ProviderRead[]; onChange?: (p: ProviderRead[]) => void; onDiscover?: (p: ProviderRead) => void; discovered?: Record<string, string[]> }) {
  const t = useTranslations('aiSettings');
  return <ProviderEditor providers={providers} savedIds={providers.map((p) => p.id)} secrets={{}} disabled={false} discoveringId={null} discoverError={null} discovered={discovered} onChange={onChange} onSecret={vi.fn()} onDiscover={onDiscover} t={t as never} />;
}
const renderEditor = (props: Parameters<typeof Harness>[0]) => render(<NextIntlClientProvider locale="en-US" messages={messages['en-us']}><Harness {...props} /></NextIntlClientProvider>);

describe('provider editor', () => {
  it('shows local and policy-denied badges and discovers models', async () => {
    const onDiscover = vi.fn();
    renderEditor({ providers: [cloud, local], onDiscover });
    expect(screen.getByText('Local')).toBeTruthy();
    expect(screen.getByText('Blocked by deployment network policy')).toBeTruthy();
    await userEvent.click(screen.getAllByRole('button', { name: 'Discover models' })[1]);
    expect(onDiscover).toHaveBeenCalledWith(local);
  });

  it('adds and deletes providers, disabling add at the cap', async () => {
    const onChange = vi.fn();
    renderEditor({ providers: [cloud], onChange });
    await userEvent.click(screen.getByRole('button', { name: /Add provider/ }));
    expect(onChange.mock.calls[0][0]).toHaveLength(2);
    await userEvent.click(screen.getByRole('button', { name: 'Remove provider OpenAI' }));
    expect(onChange).toHaveBeenLastCalledWith([]);
  });

  it('keeps provider cards to connection fields only', () => {
    renderEditor({ providers: [cloud], discovered: { default: ['gpt-4o', 'x'] } });
    expect(screen.getByText('2 models discovered · pick one to add (draft, not saved)')).toBeTruthy();
    expect(screen.queryByRole('table')).toBeNull();
    expect(screen.queryByRole('button', { name: 'Add' })).toBeNull();
  });

  it('disables add at the maximum', () => {
    const many = Array.from({ length: maxProviders }, (_, i) => ({ ...cloud, id: `p${i}` }));
    renderEditor({ providers: many });
    expect((screen.getByRole('button', { name: /Add provider/ }) as HTMLButtonElement).disabled).toBe(true);
  });
});

describe('provider helpers', () => {
  it('generates unique ids and resolves the mapping provider', () => {
    expect(newProvider('ollama', [local]).id).toBe('ollama-2');
    expect(mappingProviderId(null, [local, cloud])).toBe('default');
    expect(mappingProviderId(null, [local])).toBe('ollama-1');
    expect(mappingProviderId('x', [local])).toBe('x');
  });

  it('migrates drafts without providers and drops other revisions', () => {
    const old = JSON.stringify({ revision: 3, draft: { configuration_revision: 3 }, searchAction: 'unchanged' });
    expect(parseDraftSnapshot(old, 3)?.draft?.providers).toEqual([]);
    expect(parseDraftSnapshot(old, 4)).toBeNull();
    expect(parseDraftSnapshot('not json', 3)).toBeNull();
    expect(parseDraftSnapshot(null, 3)).toBeNull();
  });
});

describe('model types', () => {
  it('guesses types from ids', () => {
    const cases: [string, string][] = [['text-embedding-3-small', 'embedding'], ['bge-reranker-v2', 'reranker'], ['whisper-1', 'asr'], ['tts-1', 'tts'], ['llava:7b', 'vision'], ['qwen2-vl', 'vision'], ['gpt-4o', 'llm']];
    for (const [id, type] of cases) expect(guessModelType(id)).toBe(type);
  });
});
