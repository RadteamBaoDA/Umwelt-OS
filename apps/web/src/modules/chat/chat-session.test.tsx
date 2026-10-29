import * as React from 'react';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { NextIntlClientProvider } from 'next-intl';
import { beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '@/core/api';
import { messages } from '@/core/messages';
import type { StreamEventsOptions } from '@/modules/chat/api';

const h = vi.hoisted(() => ({
  setDraft: vi.fn(),
  sendMessage: vi.fn(),
  streamResponseEvents: vi.fn(),
  getConversation: vi.fn(),
  cancelResponse: vi.fn(),
}));

vi.mock('@/modules/chat/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/modules/chat/api')>()),
  sendMessage: h.sendMessage,
  streamResponseEvents: h.streamResponseEvents,
  getConversation: h.getConversation,
  cancelResponse: h.cancelResponse,
}));
vi.mock('@/core/app-shell/workspace-shell', () => ({ useWorkspaceSession: () => ({ csrfToken: 'csrf' }) }));
vi.mock('@/core/query-provider', () => ({ useDisplayPreferences: () => ({ confirmedPreferences: { locale: 'en-us', timezone: 'UTC' } }) }));
vi.mock('@/core/app-shell/chat-controller', () => ({
  PendingMessageMutationConflictError: class extends Error {},
  useChatController: () => {
    const [draft, setDraftState] = React.useState('');
    return {
      activeConversationId: 'c1',
      activeConversationGeneration: 1,
      pendingSpecialistRuns: {},
      pendingMessageMutations: {},
      context: null,
      draft,
      setDraft: (value: string) => { h.setDraft(value); setDraftState(value); },
      setContext: () => undefined,
      setActiveConversationId: () => undefined,
      resetConversation: () => undefined,
      bindDayConversation: () => undefined,
      clearPendingSpecialistRun: () => undefined,
      setPendingSpecialistRun: () => undefined,
    };
  },
}));
vi.mock('@/modules/agents/conversation-agent-activity', () => ({ ConversationAgentActivity: () => null }));
vi.mock('@/modules/pick/pick-qr-modal', () => ({ PickQrButton: () => null }));
vi.mock('./provider-gate', () => ({ useChatProviderReady: () => true, ProviderGateAlert: () => null }));
vi.mock('./model-picker', () => ({ useChatModelPicker: () => undefined, ModelPicker: () => null }));
vi.mock('./generative-ui/components/tool-rendering', () => ({ ToolRendering: () => null }));

import { ChatSession } from './chat-session';

const PLACEHOLDER = 'Ask a question about your knowledge or documents…';
const conversation = (active: string | null = null) => ({
  id: 'c1', title: 'T', messages: [], active_response_id: active, context_kind: null, context_resource_id: null,
  pinned: false, archived: false, created_at: '2026-01-01T00:00:00Z', updated_at: '2026-01-01T00:00:00Z', metadata: {},
});

function renderSession(props: Partial<React.ComponentProps<typeof ChatSession>> = {}) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <NextIntlClientProvider locale="en-US" messages={messages['en-us']}>
        <ChatSession conversationId="c1" mode="drawer" {...props} />
      </NextIntlClientProvider>
    </QueryClientProvider>,
  );
}

async function sendText(text: string) {
  await userEvent.type(screen.getByLabelText(PLACEHOLDER), text);
  await userEvent.click(screen.getByTestId('chat-send'));
}

describe('ChatSession', () => {
  beforeAll(() => {
    Element.prototype.scrollTo = (() => undefined) as unknown as typeof Element.prototype.scrollTo;
    Element.prototype.scrollIntoView = (() => undefined) as unknown as typeof Element.prototype.scrollIntoView;
    window.matchMedia = ((query: string) => ({
      matches: false, media: query, addEventListener: () => undefined, removeEventListener: () => undefined,
      addListener: () => undefined, removeListener: () => undefined, onchange: null, dispatchEvent: () => false,
    })) as unknown as typeof window.matchMedia;
  });
  beforeEach(() => {
    vi.clearAllMocks();
    vi.spyOn(console, 'warn').mockImplementation(() => undefined);
    h.getConversation.mockResolvedValue(conversation());
    h.cancelResponse.mockResolvedValue({ response_id: 'r1', status: 'cancelled' });
  });

  it('restores the draft when the send is rejected and Retry resends the same text', async () => {
    h.sendMessage.mockRejectedValueOnce(new ApiError(429, 'busy'));
    renderSession();
    await sendText('hello');

    expect(await screen.findByRole('alert')).toBeTruthy();
    // Cleared optimistically, then restored because the server never accepted it.
    expect(h.setDraft.mock.calls.map((c) => c[0])).toContain('');
    expect(h.setDraft).toHaveBeenLastCalledWith('hello');
    expect((screen.getByLabelText(PLACEHOLDER) as HTMLTextAreaElement).value).toBe('hello');

    h.sendMessage.mockResolvedValueOnce({ response_id: 'r1', status: 'queued' });
    h.streamResponseEvents.mockImplementation(async (_id: string, o: StreamEventsOptions) => {
      o.onDone?.({ text: 'ok', citations: [], status: 'completed' });
    });
    await userEvent.click(screen.getByRole('button', { name: 'Retry' }));
    await waitFor(() => expect(h.sendMessage).toHaveBeenCalledTimes(2));
    expect(h.sendMessage.mock.calls[1][1]).toMatchObject({ content: 'hello' });
  });

  it('shows a neutral status (no alert, no Retry) after the user stops generation', async () => {
    h.sendMessage.mockResolvedValue({ response_id: 'r1', status: 'queued' });
    h.streamResponseEvents.mockImplementation((_id: string, o: StreamEventsOptions) => new Promise<void>((resolve) => {
      o.signal?.addEventListener('abort', () => resolve());
    }));
    renderSession();
    await sendText('hello');
    await userEvent.click(await screen.findByTestId('chat-stop'));

    expect(await screen.findByTestId('chat-cancelled')).toBeTruthy();
    expect(screen.queryByRole('alert')).toBeNull();
    expect(screen.queryByRole('button', { name: 'Retry' })).toBeNull();
  });

  it('recovers from a stream that ends without a terminal event and reattaches once', async () => {
    h.streamResponseEvents.mockResolvedValue(undefined); // resolves with no events at all
    h.getConversation.mockResolvedValue(conversation('r1')); // server still reports the run active
    renderSession(); // mount attaches to the active run, like a returning user

    await waitFor(() => expect(h.streamResponseEvents).toHaveBeenCalledTimes(2));
    // Bounded: a second lost stream is not retried forever, and the UI is no longer stuck "Generating".
    await waitFor(() => expect(screen.queryByText('Generating response…')).toBeNull());
    await new Promise((resolve) => setTimeout(resolve, 100));
    expect(h.streamResponseEvents).toHaveBeenCalledTimes(2);
  });

  it('resumes a dropped stream with the stored last event id before showing an error', async () => {
    h.sendMessage.mockResolvedValue({ response_id: 'r1', status: 'queued' });
    const seen: Array<string | undefined> = [];
    let calls = 0;
    h.streamResponseEvents.mockImplementation(async (_id: string, o: StreamEventsOptions) => {
      seen.push(o.lastEventId);
      calls += 1;
      if (calls < 3) {
        o.lastEventId = `evt-${calls}`; // what the SSE parser stores from each `id:` line
        throw new TypeError('network down');
      }
      o.onDone?.({ text: 'ok', citations: [], status: 'completed' });
    });
    renderSession();
    await sendText('hello');

    await waitFor(() => expect(h.streamResponseEvents).toHaveBeenCalledTimes(3), { timeout: 6000 });
    expect(seen).toEqual([undefined, 'evt-1', 'evt-2']);
    expect(screen.queryByRole('alert')).toBeNull();
  }, 10000);

  it('shows an error once automatic resumes are exhausted', async () => {
    h.sendMessage.mockResolvedValue({ response_id: 'r1', status: 'queued' });
    h.streamResponseEvents.mockRejectedValue(new TypeError('network down'));
    renderSession();
    await sendText('hello');

    expect(await screen.findByRole('alert', {}, { timeout: 9000 })).toBeTruthy();
    expect(h.streamResponseEvents).toHaveBeenCalledTimes(4); // first attempt + 3 resumes
  }, 15000);

  it('renders streaming reasoning open, then collapses it once the answer text starts', async () => {
    h.sendMessage.mockResolvedValue({ response_id: 'r1', status: 'queued' });
    let finish: () => void = () => undefined;
    let opts: StreamEventsOptions | undefined;
    h.streamResponseEvents.mockImplementation((_id: string, o: StreamEventsOptions) => new Promise<void>((resolve) => {
      opts = o;
      finish = () => { o.onDone?.({ text: 'a', citations: [], status: 'completed' }); resolve(); };
    }));
    renderSession();
    await sendText('hello');
    await waitFor(() => expect(opts).toBeDefined());

    React.act(() => { opts?.onReasoning?.('step one'); });
    const block = await screen.findByTestId('chat-reasoning');
    expect((block as HTMLDetailsElement).open).toBe(true);
    expect(block.textContent).toContain('Thinking…');

    React.act(() => { opts?.onDelta?.('Answer'); });
    await waitFor(() => expect((screen.getByTestId('chat-reasoning') as HTMLDetailsElement).open).toBe(false));
    expect(screen.getByTestId('chat-reasoning').textContent).toContain('Thought process');
    React.act(() => finish());
  });

  it('collapses Rename and Delete into a More menu and keeps them reachable in full mode', async () => {
    const rename = vi.fn();
    renderSession({ mode: 'full', onRenameConversation: rename, onDeleteConversation: vi.fn() });
    expect(screen.getByRole('button', { name: 'More actions' })).toBeTruthy();
    await userEvent.click(screen.getAllByRole('button', { name: 'Rename conversation' })[0]);
    expect(rename).toHaveBeenCalled();
  });
});
