import * as React from 'react';
import { renderHook, act, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { StreamEventsOptions } from './api';

const h = vi.hoisted(() => ({ streamResponseEvents: vi.fn(), getConversation: vi.fn(), apiRequest: vi.fn() }));

vi.mock('next-intl', () => ({ useTranslations: () => (key: string) => key }));
vi.mock('./api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('./api')>()),
  streamResponseEvents: h.streamResponseEvents,
  getConversation: h.getConversation,
}));
vi.mock('@/core/api', async (importOriginal) => ({ ...(await importOriginal<typeof import('@/core/api')>()), apiRequest: h.apiRequest }));

import { ChatSessionRuntime } from './chat-session-refs';
import { useChatStream } from './use-chat-stream';
import { chatKeys } from './api';

function setup() {
  const refs = new ChatSessionRuntime({ conversationId: 'c1', generation: 1 });
  refs.setMounted(true);
  const live = { dispatch: vi.fn(), patch: vi.fn(), clearOutput: vi.fn() };
  const client = new QueryClient();
  const wrapper = ({ children }: { children: React.ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>;
  const hook = renderHook(() => useChatStream(refs, live, vi.fn()), { wrapper });
  return { refs, live, client, run: hook.result.current };
}

describe('useChatStream terminal statuses', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    h.getConversation.mockResolvedValue({ active_response_id: null });
  });

  it('marks a pending input request as cancelled when the run is cancelled', async () => {
    h.streamResponseEvents.mockImplementation(async (_id: string, o: StreamEventsOptions) => { o.onStatus?.('cancelled', {}); });
    const { run, live } = setup();
    await act(async () => { await run('r1', 'c1', () => true); });
    expect(live.dispatch).toHaveBeenCalledWith({ type: 'cancelPendingInput' });
    expect(live.patch).toHaveBeenCalledWith({ isStreaming: false, isPending: false, activeResponseId: null });
    expect(live.patch).toHaveBeenCalledWith({ cancelled: true });
  });

  it('shows the failure detail for a failed run and a generic message without one', async () => {
    h.streamResponseEvents.mockImplementation(async (_id: string, o: StreamEventsOptions) => { o.onStatus?.('failed', { error: ' boom ' }); });
    const first = setup();
    await act(async () => { await first.run('r1', 'c1', () => true); });
    expect(first.live.patch).toHaveBeenCalledWith({ error: 'responseFailedDetail' });
    expect(first.live.dispatch).toHaveBeenCalledWith({ type: 'cancelPendingInput' });

    h.streamResponseEvents.mockImplementation(async (_id: string, o: StreamEventsOptions) => { o.onStatus?.('failed', undefined); });
    const second = setup();
    await act(async () => { await second.run('r2', 'c1', () => true); });
    expect(second.live.patch).toHaveBeenCalledWith({ error: 'responseFailed' });
  });

  it('ignores cancelled/failed UI updates once the view is no longer current', async () => {
    h.streamResponseEvents.mockImplementation(async (_id: string, o: StreamEventsOptions) => { o.onStatus?.('cancelled', {}); });
    const { run, live } = setup();
    await act(async () => { await run('r1', 'c1', () => false); });
    expect(live.dispatch).not.toHaveBeenCalled();
    expect(live.patch).not.toHaveBeenCalled();
  });

  it('auth_expired with a 200 session probe reattaches without resending the prompt', async () => {
    h.apiRequest.mockResolvedValue({});
    h.streamResponseEvents.mockImplementationOnce(async (_id: string, o: StreamEventsOptions) => { o.onStatus?.('auth_expired', {}); });
    const { run, live, refs } = setup();
    const streamRun = vi.fn(async () => undefined);
    refs.setStreamRun(streamRun);
    await act(async () => { await run('r1', 'c1', () => true); });

    expect(live.patch).toHaveBeenCalledWith(expect.objectContaining({ error: 'sessionExpired', isStreaming: false }));
    expect(h.apiRequest).toHaveBeenCalledWith('/api/v1/auth/session');
    await waitFor(() => expect(streamRun).toHaveBeenCalledWith('r1', 'c1', expect.any(Function)));
    expect(live.dispatch).toHaveBeenCalledWith({ type: 'clearPartial' });
    expect(live.patch).toHaveBeenCalledWith({ error: null, cancelled: false, activeResponseId: 'r1', isStreaming: true });
  });

  it('auth_expired with a failing probe keeps the session-expired error and does not reattach', async () => {
    h.apiRequest.mockRejectedValue(new Error('401'));
    h.streamResponseEvents.mockImplementationOnce(async (_id: string, o: StreamEventsOptions) => { o.onStatus?.('auth_expired', {}); });
    const { run, refs } = setup();
    const streamRun = vi.fn(async () => undefined);
    refs.setStreamRun(streamRun);
    await act(async () => { await run('r1', 'c1', () => true); });
    await new Promise((r) => setTimeout(r, 20));
    expect(streamRun).not.toHaveBeenCalled();
  });

  it('settles the UI at message.done while the open stream still applies live title and suggestions', async () => {
    let release: () => void = () => undefined;
    const open = new Promise<void>((resolve) => { release = resolve; });
    h.streamResponseEvents.mockImplementation(async (_id: string, o: StreamEventsOptions) => {
      o.onDone?.({ text: 'a', citations: [], model: 'm', status: 'completed' } as never);
      await open; // connection stays open for the post-answer follow-up step
      o.onConversationTitle?.('Trip to Hue');
      o.onSuggestions?.({ message_id: 'm1', suggestions: ['a?', 'b?'] });
    });
    const { run, live, client } = setup();
    client.setQueryData(chatKeys.conversation('c1'), { id: 'c1', title: 'x', messages: [{ id: 'm1', role: 'assistant' }] });
    h.getConversation.mockResolvedValue({ id: 'c1', title: 'x', active_response_id: null, messages: [{ id: 'm1', role: 'assistant' }] });
    let finished = false;
    const done = act(async () => { await run('r1', 'c1', () => true); finished = true; });
    await waitFor(() => expect(live.patch).toHaveBeenCalledWith(expect.objectContaining({ isStreaming: false, activeResponseId: null })));
    expect(finished).toBe(false); // still connected, yet the live state is already settled
    release();
    await done;
    const detail = client.getQueryData<{ title: string; messages: { suggestions?: string[] }[] }>(chatKeys.conversation('c1'));
    expect(detail?.title).toBe('Trip to Hue');
    expect(detail?.messages[0].suggestions).toEqual(['a?', 'b?']);
  });
});
