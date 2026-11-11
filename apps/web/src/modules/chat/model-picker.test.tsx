import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, render, renderHook, screen, waitFor } from '@testing-library/react';
import { NextIntlClientProvider } from 'next-intl';
import type { ReactNode } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { messages } from '@/core/messages';
import { ChatComposer } from './chat-composer';
import { ModelPicker, useChatModelPicker } from './model-picker';

const settings = {
  providers: [
    {
      id: 'p1',
      label: 'Prov',
      models: [
        { id: 'gpt-a', type: 'llm' },
        { id: 'emb', type: 'embedding' },
      ],
    },
  ],
};

afterEach(() => {
  vi.unstubAllGlobals();
  window.localStorage.clear();
});

/** Wraps renders in a fresh query client. */
function wrapper({ children }: { children: ReactNode }) {
  return <QueryClientProvider client={new QueryClient()}>{children}</QueryClientProvider>;
}

describe('useChatModelPicker', () => {
  it('lists only llm models, defaults to none, and remembers a pick', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => new Response(JSON.stringify(settings), { status: 200, headers: { 'Content-Type': 'application/json' } })),
    );
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

describe('ModelPicker narrow trigger', () => {
  const option = { providerId: 'p1', model: 'gpt-a', label: 'Prov · gpt-a' };
  /** Renders the picker and returns its trigger. */
  const trigger = (value: typeof option | null, defaultModel: string | null) => {
    render(
      <NextIntlClientProvider locale="en-US" messages={messages['en-us']}>
        <ModelPicker picker={{ options: [option], value, defaultModel, onChange: vi.fn() }} />
      </NextIntlClientProvider>,
    );
    return screen.getByRole('combobox', { name: 'Model for this chat' });
  };

  it('wraps the "Default ·" prefix in an sm-only span and keeps the full text as title', () => {
    const el = trigger(null, 'gpt-d');
    expect(el.textContent).toBe('Default · gpt-d');
    expect(el.getAttribute('title')).toBe('Default · gpt-d');
    expect(el.querySelector('span.hidden')?.textContent).toBe('Default · ');
  });

  it('hides the provider prefix of a chosen model below sm', () => {
    const el = trigger(option, null);
    expect(el.getAttribute('title')).toBe('Prov · gpt-a');
    expect(el.querySelector('span.hidden')?.textContent).toBe('Prov · ');
  });
});
