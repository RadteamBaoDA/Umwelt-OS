import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { NextIntlClientProvider, useTranslations } from 'next-intl';
import { describe, expect, it, vi } from 'vitest';
import { messages } from '@/core/messages';
import { carryMaxTokens, ModelDefaults, ModelsPanel, pruneDefaults, type Mapping } from './models-section';
import type { ProviderRead } from './provider-editor';

const cloud: ProviderRead = { id: 'default', kind: 'openai', label: 'OpenAI', base_url: 'https://api.openai.com/v1', destination_id: 'remote:x', credential_configured: true, local: false, policy_denied: false, models: [{ id: 'gpt-4o', type: 'llm' }] };
const m = (model: string, provider_id = 'default'): Mapping => ({ model, version: null, destination: 'remote', provider_id });

function Panel({ providers, onChange = vi.fn(), onTestModel = vi.fn(), discovered = {}, browse }: { browse?: (p: ProviderRead) => never; providers: ProviderRead[]; onChange?: (p: ProviderRead[]) => void; onTestModel?: (p: ProviderRead, model: { id: string; type: string }) => void; discovered?: Record<string, string[]> }) {
  const t = useTranslations('aiSettings');
  return <ModelsPanel providers={providers} discovered={discovered} modelTests={{}} disabled={false} onChange={onChange} onTestModel={onTestModel as never} browseOpenRouter={browse} t={t as never} />;
}
const renderPanel = (props: Parameters<typeof Panel>[0]) => render(<NextIntlClientProvider locale="en-US" messages={messages['en-us']}><QueryClientProvider client={new QueryClient()}><Panel {...props} /></QueryClientProvider></NextIntlClientProvider>);
const routed: ProviderRead = { ...cloud, id: 'or', kind: 'openrouter', label: 'OpenRouter', models: [] };

describe('models panel', () => {
  it('shows the empty state until a model is added', () => {
    renderPanel({ providers: [{ ...cloud, models: [] }] });
    expect(screen.getByText('Discover models from a provider.')).toBeTruthy();
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

describe('openrouter models', () => {
  const flux = { id: 'black-forest-labs/flux-2', name: 'FLUX.2', created: null, context_length: null, input_modalities: ['text'], output_modalities: ['image'], pricing_prompt: null, pricing_completion: null, supported_parameters: [] };
  it('uses the browser for openrouter providers and adds an image model', async () => {
    const onChange = vi.fn();
    const browse = vi.fn().mockResolvedValue({ ok: true, error: null, models: [flux], total_count: 1, next_offset: null });
    renderPanel({ providers: [routed], onChange, browse: () => browse as never });
    await userEvent.click(screen.getByRole('tab', { name: 'Image' }));
    await userEvent.click(await screen.findByRole('button', { name: /FLUX\.2/ }));
    await userEvent.click(screen.getByRole('button', { name: 'Add' }));
    expect(onChange).toHaveBeenLastCalledWith([{ ...routed, models: [{ id: 'black-forest-labs/flux-2', type: 'image' }] }]);
  });
  it('offers output-only types only for OpenRouter providers', async () => {
    renderPanel({ providers: [cloud] });
    await userEvent.click(screen.getByRole('combobox', { name: 'Type' }));
    expect(screen.queryByRole('option', { name: 'Image' })).toBeNull();
  });
});

describe('model defaults', () => {
  it('never lists output-only types in role defaults', async () => {
    const odd: ProviderRead = { ...cloud, models: [{ id: 'img', type: 'image' }, { id: 'chat1', type: 'llm' }] };
    function Block() {
      const t = useTranslations('aiSettings');
      return <ModelDefaults providers={[odd]} defaults={{}} disabled={false} embeddingStatus="idle" onPick={vi.fn()} t={t as never} />;
    }
    render(<NextIntlClientProvider locale="en-US" messages={messages['en-us']}><Block /></NextIntlClientProvider>);
    await userEvent.click(screen.getByRole('combobox', { name: 'Background model' }));
    expect(screen.queryByRole('option', { name: /img/ })).toBeNull();
  });

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
  describe('max output tokens', () => {
    const custom: ProviderRead = { ...cloud, id: 'cu', kind: 'custom', label: 'Custom', models: [{ id: 'llama', type: 'llm' }] };
    const block = (providers: ProviderRead[], chat: Mapping | null, onMaxTokens = vi.fn()) => {
      function Block() {
        const t = useTranslations('aiSettings');
        return <ModelDefaults providers={providers} defaults={{ chat }} disabled={false} embeddingStatus="idle" onPick={vi.fn()} onMaxTokens={onMaxTokens} t={t as never} />;
      }
      render(<NextIntlClientProvider locale="en-US" messages={messages['en-us']}><Block /></NextIntlClientProvider>);
      return onMaxTokens;
    };

    it.each(['openai', 'anthropic', 'groq'] as const)('hides the field for hosted kind %s', (kind) => {
      block([{ ...cloud, kind }], m('gpt-4o'));
      expect(screen.queryByLabelText(/Max output tokens/)).toBeNull();
    });

    it('hides the field until a model is chosen', () => {
      block([custom], null);
      expect(screen.queryByLabelText(/Max output tokens/)).toBeNull();
    });

    it.each(['custom', 'openrouter', 'gemini', 'ollama', 'lmstudio'] as const)('shows an optional number input for %s and reports edits', async (kind) => {
      const onMaxTokens = block([{ ...custom, kind }], { ...m('llama', 'cu'), max_output_tokens: 2048 });
      const input = screen.getByLabelText('Chat model · Max output tokens') as HTMLInputElement;
      expect(input.type).toBe('number');
      expect(input.value).toBe('2048');
      fireEvent.change(input, { target: { value: '' } });
      expect(onMaxTokens).toHaveBeenLastCalledWith('chat', null);
      fireEvent.change(input, { target: { value: '512' } });
      expect(onMaxTokens).toHaveBeenLastCalledWith('chat', 512);
      fireEvent.change(input, { target: { value: '5000000' } });
      expect(onMaxTokens).toHaveBeenLastCalledWith('chat', 1000000);
    });

    it('carries the value to another non-hosted provider and clears it for a hosted one', () => {
      const previous: Mapping = { ...m('llama', 'cu'), max_output_tokens: 2048 };
      expect(carryMaxTokens(m('x'), previous, custom).max_output_tokens).toBe(2048);
      expect(carryMaxTokens(m('x'), previous, cloud).max_output_tokens).toBeUndefined();
      expect(carryMaxTokens(m('x'), null, custom)).toEqual(m('x'));
    });
  });
});
