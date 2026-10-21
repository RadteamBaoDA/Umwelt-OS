import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, render, renderHook, screen, waitFor } from '@testing-library/react';
import { NextIntlClientProvider } from 'next-intl';
import type { ReactNode } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { messages } from '@/core/messages';
import { ChatComposer } from './chat-composer';
import { ModelPicker, useChatModelPicker } from './model-picker';

const settings = {
  providers: [{ id: 'p1', label: 'Prov', models: [{ id: 'gpt-a', type: 'llm' }, { id: 'emb', type: 'embedding' }] }],
};

afterEach(() => { vi.unstubAllGlobals(); window.localStorage.clear(); });

/** Wraps renders in a fresh query client. */
function wrapper({ children }: { children: ReactNode }) {
  return <QueryClientProvider client={new QueryClient()}>{children}</QueryClientProvider>;
}

describe('useChatModelPicker', () => {
  it('lists only llm models, defaults to none, and remembers a pick', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify(settings), { status: 200, headers: { 'Content-Type': 'application/json' } })));
    const { result } = renderHook(() => useChatModelPicker(), { wrapper });
    await waitFor(() => expect(result.current).toBeDefined());
    expect(result.current!.options.map((o) => o.label)).toEqual(['Prov · gpt-a']);
    expect(result.current!.value).toBeNull();
    act(() => result.current!.onChange(result.current!.options[0]));
    expect(result.current!.value).toMatchObject({ providerId: 'p1', model: 'gpt-a' });
    expect(window.localStorage.getItem('umwelt.chat.model')).toContain('gpt-a');
  });
});

describe('composer model picker', () => {
  it('renders in the drawer composer when a picker is supplied', () => {
    const picker = { options: [{ providerId: 'p1', model: 'gpt-a', label: 'Prov · gpt-a' }], value: null, onChange: vi.fn() };
    render(
      <NextIntlClientProvider locale="en-US" messages={messages['en-us']}>
        <ChatComposer value="" onChange={() => undefined} onSend={() => undefined} isStreaming={false} mode="drawer" modelPicker={picker} />
        <ModelPicker picker={picker} />
      </NextIntlClientProvider>,
    );
    expect(screen.getAllByRole('combobox', { name: 'Model for this chat' })).toHaveLength(2);
  });
});
