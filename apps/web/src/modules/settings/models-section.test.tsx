import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { NextIntlClientProvider, useTranslations } from 'next-intl';
import { describe, expect, it, vi } from 'vitest';
import { messages } from '@/core/messages';
import { ModelDefaults, ModelsPanel, pruneDefaults, type Mapping } from './models-section';
import type { ProviderRead } from './provider-editor';

const cloud: ProviderRead = { id: 'default', kind: 'openai', label: 'OpenAI', base_url: 'https://api.openai.com/v1', destination_id: 'remote:x', credential_configured: true, local: false, policy_denied: false, models: [{ id: 'gpt-4o', type: 'llm' }] };
const m = (model: string, provider_id = 'default'): Mapping => ({ model, version: null, destination: 'remote', provider_id });

function Panel({ providers, onChange = vi.fn(), onTestModel = vi.fn(), discovered = {} }: { providers: ProviderRead[]; onChange?: (p: ProviderRead[]) => void; onTestModel?: (p: ProviderRead, model: { id: string; type: string }) => void; discovered?: Record<string, string[]> }) {
  const t = useTranslations('aiSettings');
  return <ModelsPanel providers={providers} discovered={discovered} modelTests={{}} disabled={false} onChange={onChange} onTestModel={onTestModel as never} t={t as never} />;
}
const renderPanel = (props: Parameters<typeof Panel>[0]) => render(<NextIntlClientProvider locale="en-US" messages={messages['en-us']}><Panel {...props} /></NextIntlClientProvider>);

describe('models panel', () => {
  it('shows the empty state until a model is added', () => {
    renderPanel({ providers: [{ ...cloud, models: [] }] });
    expect(screen.getByText('Run Discover on a provider, then add the models you want.')).toBeTruthy();
  });

  it('adds a discovered model with an auto-guessed type', async () => {
    const onChange = vi.fn();
    renderPanel({ providers: [cloud], onChange, discovered: { default: ['gpt-4o', 'text-embedding-3', 'whisper-1'] } });
    await userEvent.click(screen.getByRole('combobox', { name: 'Model' }));
    await userEvent.type(screen.getByRole('textbox', { name: 'Search models' }), 'embed');
    expect(screen.queryByRole('option', { name: 'gpt-4o' })).toBeNull();
    await userEvent.click(screen.getByRole('option', { name: 'text-embedding-3' }));
    await userEvent.click(screen.getByRole('button', { name: 'Add' }));
    expect(onChange).toHaveBeenLastCalledWith([{ ...cloud, models: [{ id: 'gpt-4o', type: 'llm' }, { id: 'text-embedding-3', type: 'embedding' }] }]);
  });

  it('filters duplicate discovered IDs as the search changes', async () => {
    renderPanel({ providers: [cloud], discovered: { default: ['gemini/transcribe', 'gemini/transcribe', 'anthropic/claude'] } });
    await userEvent.click(screen.getByRole('combobox', { name: 'Model' }));
    await userEvent.type(screen.getByRole('textbox', { name: 'Search models' }), 'anthro');
    expect(screen.getAllByRole('option').map((option) => option.textContent)).toEqual(['anthropic/claude']);
    await userEvent.clear(screen.getByRole('textbox', { name: 'Search models' }));
    expect(screen.getAllByRole('option').map((option) => option.textContent)).toEqual(['gemini/transcribe', 'anthropic/claude']);
  });

  it('lists models across providers, tests and removes one', async () => {
    const onChange = vi.fn();
    const onTestModel = vi.fn();
    const other: ProviderRead = { ...cloud, id: 'ol', label: 'Ollama', models: [{ id: 'llava', type: 'llm', vision: true }] };
    renderPanel({ providers: [cloud, other], onChange, onTestModel });
    expect(screen.getByText('llava')).toBeTruthy();
    await userEvent.click(screen.getByRole('button', { name: 'Test model gpt-4o' }));
    expect(onTestModel).toHaveBeenCalledWith(cloud, { id: 'gpt-4o', type: 'llm' });
    await userEvent.click(screen.getByRole('button', { name: 'Remove model gpt-4o' }));
    expect(onChange).toHaveBeenLastCalledWith([{ ...cloud, models: [] }, other]);
  });
});

describe('model defaults', () => {
  it('prunes defaults whose model was removed', () => {
    const next = [{ ...cloud, models: [] }];
    const out = pruneDefaults([cloud], next, { chat: m('gpt-4o'), embedding: m('keep') });
    expect(out.chat).toBeNull();
    expect(out.embedding?.model).toBe('keep');
  });

  it('lists only models of the right type per role', async () => {
    const all: ProviderRead = { ...cloud, models: [{ id: 'chat1', type: 'llm' }, { id: 'emb1', type: 'embedding' }, { id: 'rr1', type: 'reranker' }, { id: 'vis1', type: 'llm', vision: true }, { id: 'bg1', type: 'background' }] };
    const onPick = vi.fn();
    function Block() {
      const t = useTranslations('aiSettings');
      return <ModelDefaults providers={[all]} defaults={{}} disabled={false} embeddingStatus="idle" onPick={onPick} t={t as never} />;
    }
    render(<NextIntlClientProvider locale="en-US" messages={messages['en-us']}><Block /></NextIntlClientProvider>);
    expect(screen.queryByText(/reasoning-|fast/)).toBeNull();
    await userEvent.click(screen.getByRole('combobox', { name: 'Background model' }));
    expect(screen.getAllByRole('option').map((o) => o.textContent)).toEqual(['Same as brief model', 'OpenAI · chat1', 'OpenAI · vis1', 'OpenAI · bg1']);
    await userEvent.click(screen.getByRole('option', { name: 'OpenAI · bg1' }));
    expect(onPick).toHaveBeenCalledWith('background', all, 'bg1');
    await userEvent.click(screen.getByRole('combobox', { name: 'Embedding model' }));
    expect(screen.getAllByRole('option').map((o) => o.textContent)).toEqual(['OpenAI · emb1']);
    await userEvent.click(screen.getByRole('option', { name: 'OpenAI · emb1' }));
    expect(onPick).toHaveBeenCalledWith('embedding', all, 'emb1');
  });

  it('sets the chat reasoning effort and is disabled without a chat model', async () => {
    const onEffort = vi.fn();
    function Block({ chat }: { chat: Mapping | null }) {
      const t = useTranslations('aiSettings');
      return <ModelDefaults providers={[cloud]} defaults={{ chat }} disabled={false} embeddingStatus="idle" onPick={vi.fn()} onEffort={onEffort} t={t as never} />;
    }
    const wrap = (chat: Mapping | null) => <NextIntlClientProvider locale="en-US" messages={messages['en-us']}><Block chat={chat} /></NextIntlClientProvider>;
    const { rerender } = render(wrap(null));
    expect(screen.getByRole('combobox', { name: 'Reasoning effort' }).hasAttribute('disabled')).toBe(true);
    rerender(wrap(m('gpt-4o')));
    await userEvent.click(screen.getByRole('combobox', { name: 'Reasoning effort' }));
    await userEvent.click(screen.getByRole('option', { name: 'High' }));
    expect(onEffort).toHaveBeenCalledWith('high');
  });
});
