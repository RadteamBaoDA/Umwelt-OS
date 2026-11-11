import * as React from 'react';
import { renderHook, act } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '@/core/api';

const h = vi.hoisted(() => ({
  createConversation: vi.fn(),
  deleteConversation: vi.fn(),
  sendMessage: vi.fn(),
  mutateMessage: vi.fn(),
  startAgentRun: vi.fn(),
}));

vi.mock('next-intl', () => ({ useTranslations: () => (key: string) => key }));
vi.mock('./api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('./api')>()),
  createConversation: h.createConversation,
  deleteConversation: h.deleteConversation,
  sendMessage: h.sendMessage,
  mutateMessage: h.mutateMessage,
  hashMessageContent: async () => 'hash-1',
}));
vi.mock('@/modules/agents/public-api', () => ({ startAgentRun: h.startAgentRun }));
vi.mock('@/core/app-shell/chat-controller', () => ({
  PendingMessageMutationConflictError: class PendingMessageMutationConflictError extends Error {},
}));

import { PendingMessageMutationConflictError } from '@/core/app-shell/chat-controller';
import { ChatSessionRuntime } from './chat-session-refs';
import { useChatSend, type ChatSendDeps } from './use-chat-send';

type Deps = Omit<ChatSendDeps, 'queryClient' | 't' | 'tAgents'>;

/** Builds a full dependency snapshot with spies; `over` overrides individual members. */
function makeDeps(over: Partial<Deps> = {}, ctrlOver: Record<string, unknown> = {}) {
  const refs = new ChatSessionRuntime({ conversationId: null, generation: 0 });
  refs.setMounted(true);
  const chatCtrl = {
    context: null,
    pendingSpecialistRuns: {},
    setDraft: vi.fn(),
    bindDayConversation: vi.fn(),
    setActiveConversationId: vi.fn(),
    setPendingSpecialistRun: vi.fn(),
    clearPendingSpecialistRun: vi.fn(),
    getOrCreatePendingMessageMutation: vi.fn((_id: string, input: Record<string, unknown>) => ({ ...input, clientRequestId: 'req-1' })),
    clearPendingMessageMutation: vi.fn(),
    ...ctrlOver,
  };
  const live = { dispatch: vi.fn(), patch: vi.fn(), clearOutput: vi.fn() };
  const deps = {
    refs,
    live,
    chatCtrl: chatCtrl as unknown as Deps['chatCtrl'],
    csrfToken: 'csrf',
    conversationId: null,
    mode: 'full',
    onConversationCreated: undefined,
    messages: [{ id: 'm1', role: 'user', content: 'old prompt' }] as unknown as Deps['messages'],
    isBusy: false,
    providerReady: true,
    openProviderGate: vi.fn(),
    modelChoice: null,
    webSearchRequested: false,
    dbQueryEffective: false,
    resetToggles: vi.fn(),
    effectiveEditingMessageId: null,
    clearEditing: vi.fn(),
    selectedProfileId: 'assistant',
    profiles: undefined,
    pendingProfileRun: null,
    attachments: { items: [], clear: vi.fn() } as unknown as Deps['attachments'],
    setActiveAgentRunId: vi.fn(),
    streamResponseRun: vi.fn(async () => undefined),
    ...over,
  } satisfies Deps;
  return { deps, refs, chatCtrl, live };
}

function render(deps: Deps, client = new QueryClient()) {
  const wrapper = ({ children }: { children: React.ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>;
  return renderHook(() => useChatSend(deps), { wrapper });
}

describe('useChatSend', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    h.sendMessage.mockResolvedValue({ response_id: 'r1', status: 'queued' });
    h.mutateMessage.mockResolvedValue({ response_id: 'r2', status: 'queued' });
    h.deleteConversation.mockResolvedValue(undefined);
  });

  it('opens the provider gate and keeps the text as the draft when no model is usable', async () => {
    const { deps, chatCtrl } = makeDeps({ providerReady: false });
    const { result } = render(deps);
    let ok = true;
    await act(async () => {
      ok = await result.current.handleSend('hello');
    });
    expect(ok).toBe(false);
    expect(chatCtrl.setDraft).toHaveBeenCalledWith('hello');
    expect(deps.openProviderGate).toHaveBeenCalled();
    expect(h.sendMessage).not.toHaveBeenCalled();
  });

  it('first send creates the conversation, promotes it as the selection and streams the response', async () => {
    h.createConversation.mockResolvedValue({ id: 'new1' });
    const onCreated = vi.fn();
    const { deps, refs, live } = makeDeps({ onConversationCreated: onCreated });
    const { result } = render(deps);
    let ok = false;
    await act(async () => {
      ok = await result.current.handleSend('hello world');
    });

    expect(ok).toBe(true);
    expect(h.createConversation).toHaveBeenCalledWith(expect.objectContaining({ title: 'hello world' }), 'csrf');
    expect(onCreated).toHaveBeenCalledWith('new1');
    expect(refs.promotedSelection).toEqual({ conversationId: 'new1', generation: 1, sendGeneration: refs.sendGeneration });
    expect(refs.selection).toEqual({ conversationId: 'new1', generation: 1 });
    expect(h.sendMessage.mock.calls[0][0]).toBe('new1');
    expect(h.sendMessage.mock.calls[0][1]).toMatchObject({ content: 'hello world' });
    expect(deps.resetToggles).toHaveBeenCalled();
    expect(live.patch).toHaveBeenCalledWith(expect.objectContaining({ activeResponseId: 'r1', isStreaming: true, isPending: false }));
    expect(deps.streamResponseRun).toHaveBeenCalledWith('r1', 'new1', expect.any(Function));
  });

  it('falls back to the controller when no onConversationCreated callback is supplied', async () => {
    h.createConversation.mockResolvedValue({ id: 'new2' });
    const { deps, chatCtrl } = makeDeps();
    const { result } = render(deps);
    await act(async () => {
      await result.current.handleSend('x');
    });
    expect(chatCtrl.setActiveConversationId).toHaveBeenCalledWith('new2');
  });

  it('rejects an unresolved specialist prompt, restoring it and showing agentRetryPending', async () => {
    const pending = { prompt: 'other', profileId: 'p', profileRevision: 1, conversationId: 'c1', clientRequestId: 'q' };
    const { deps, chatCtrl, live } = makeDeps({ pendingProfileRun: pending, conversationId: 'c1' });
    const { result } = render(deps);
    let ok = true;
    await act(async () => {
      ok = await result.current.handleSend('different text');
    });
    expect(ok).toBe(false);
    expect(chatCtrl.setDraft).toHaveBeenCalledWith('other');
    expect(live.patch).toHaveBeenLastCalledWith({ isPending: false, error: 'agentRetryPending' });
    expect(h.sendMessage).not.toHaveBeenCalled();
    expect(h.startAgentRun).not.toHaveBeenCalled();
  });

  it('rejects an unavailable specialist with agentUnavailable and restores the draft', async () => {
    const { deps, chatCtrl, live } = makeDeps({ conversationId: 'c1', selectedProfileId: 'spec', profiles: [] });
    const { result } = render(deps);
    await act(async () => {
      await result.current.handleSend('hi');
    });
    expect(chatCtrl.setDraft).toHaveBeenLastCalledWith('hi');
    expect(live.patch).toHaveBeenLastCalledWith({ error: 'agentUnavailable' });
  });

  it('starts a specialist run with an immutable envelope instead of sending a message', async () => {
    h.startAgentRun.mockResolvedValue({ id: 'run1' });
    const profile = { id: 'spec', revision: 3, enabled: true, capability: 'available' };
    const { deps, chatCtrl } = makeDeps({
      conversationId: 'c1',
      selectedProfileId: 'spec',
      profiles: [profile] as unknown as Deps['profiles'],
    });
    const { result } = render(deps);
    let ok = false;
    await act(async () => {
      ok = await result.current.handleSend('go');
    });
    expect(ok).toBe(true);
    expect(chatCtrl.setPendingSpecialistRun).toHaveBeenCalledWith(
      expect.objectContaining({ prompt: 'go', profileId: 'spec', profileRevision: 3, conversationId: 'c1' }),
    );
    expect(h.startAgentRun).toHaveBeenCalledWith(
      'spec',
      expect.objectContaining({ prompt: 'go', expected_profile_revision: 3, conversation_id: 'c1' }),
      'csrf',
    );
    expect(chatCtrl.clearPendingSpecialistRun).toHaveBeenCalledWith('c1');
    expect(deps.setActiveAgentRunId).toHaveBeenCalledWith('run1');
    expect(h.sendMessage).not.toHaveBeenCalled();
  });

  it('throws selection_too_large for more than 32 documents, restores the draft and never calls the server', async () => {
    const items = Array.from({ length: 33 }, (_, i) => ({ sourceId: 's', documentId: `d${i}`, documentVersionId: 'v', chunkId: 'c' }));
    const { deps, chatCtrl, live } = makeDeps({ conversationId: 'c1' }, { context: { kind: 'selection', items } });
    const { result } = render(deps);
    let ok = true;
    await act(async () => {
      ok = await result.current.handleSend('hello');
    });
    expect(ok).toBe(false);
    expect(h.sendMessage).not.toHaveBeenCalled();
    expect(chatCtrl.setDraft).toHaveBeenLastCalledWith('hello');
    expect(live.patch).toHaveBeenLastCalledWith({ error: 'contextSelectionTooLarge' });
  });

  it('restores the draft and localizes a rejected send (not accepted)', async () => {
    h.sendMessage.mockRejectedValue(new ApiError(409, 'conflict'));
    const { deps, chatCtrl, live } = makeDeps({ conversationId: 'c1' });
    const { result } = render(deps);
    let ok = true;
    await act(async () => {
      ok = await result.current.handleSend('hello');
    });
    expect(ok).toBe(false);
    expect(chatCtrl.setDraft).toHaveBeenLastCalledWith('hello');
    expect(live.patch).toHaveBeenLastCalledWith({ error: 'errorConflict' });
  });

  it('shows the user message at once (keyed by client_request_id) and removes it when the server rejects', async () => {
    let reject: (e: unknown) => void = () => undefined;
    h.sendMessage.mockReturnValue(
      new Promise((_, r) => {
        reject = r;
      }),
    );
    const client = new QueryClient();
    const { deps } = makeDeps({ conversationId: 'c1' });
    const { result } = render(deps, client);
    let pending: Promise<boolean> = Promise.resolve(false);
    await act(async () => {
      pending = result.current.handleSend('hello');
    });
    const shown = client.getQueryData<{ content: string; client_request_id: string }[]>(['chat', 'optimistic', 'c1']);
    expect(shown).toHaveLength(1);
    expect(shown?.[0]).toMatchObject({ content: 'hello', client_request_id: h.sendMessage.mock.calls[0][1].client_request_id });
    await act(async () => {
      reject(new ApiError(409, 'conflict'));
      await pending;
    });
    expect(client.getQueryData(['chat', 'optimistic', 'c1'])).toEqual([]);
  });

  it('keeps the optimistic message when the server accepted the send', async () => {
    const client = new QueryClient();
    const { deps } = makeDeps({ conversationId: 'c1' });
    const { result } = render(deps, client);
    await act(async () => {
      await result.current.handleSend('hello');
    });
    expect(client.getQueryData<unknown[]>(['chat', 'optimistic', 'c1'])).toHaveLength(1);
  });

  it('deletes the conversation the first send created when the server did not accept the message', async () => {
    h.createConversation.mockResolvedValue({ id: 'new1' });
    h.sendMessage.mockRejectedValue(new ApiError(422, 'bad'));
    const { deps, chatCtrl, refs } = makeDeps({ onConversationCreated: vi.fn() });
    const { result } = render(deps);
    await act(async () => {
      await result.current.handleSend('hello');
    });
    expect(h.deleteConversation).toHaveBeenCalledWith('new1', 'csrf');
    expect(chatCtrl.setActiveConversationId).toHaveBeenCalledWith(null);
    expect(refs.promotedSelection).toMatchObject({ conversationId: null });
  });

  it.each([
    ['a network drop', new TypeError('Failed to fetch')],
    ['a 502 from the proxy', new ApiError(502, 'bad gateway')],
    ['a 408 timeout', new ApiError(408, 'timeout')],
  ])('keeps the created conversation and restores the draft on %s (the server may have committed it)', async (_n, err) => {
    h.createConversation.mockResolvedValue({ id: 'new1' });
    h.sendMessage.mockRejectedValue(err);
    const { deps, chatCtrl } = makeDeps({ onConversationCreated: vi.fn() });
    const { result } = render(deps);
    await act(async () => {
      await result.current.handleSend('hello');
    });
    expect(h.deleteConversation).not.toHaveBeenCalled();
    expect(chatCtrl.setDraft).toHaveBeenLastCalledWith('hello');
  });

  it('does not delete a conversation that already existed', async () => {
    h.sendMessage.mockRejectedValue(new ApiError(422, 'bad'));
    const { deps } = makeDeps({ conversationId: 'c1' });
    const { result } = render(deps);
    await act(async () => {
      await result.current.handleSend('hello');
    });
    expect(h.deleteConversation).not.toHaveBeenCalled();
  });

  it('edit and regenerate send base_content_hash and the envelope client_request_id', async () => {
    const { deps, chatCtrl } = makeDeps({ conversationId: 'c1' });
    const { result } = render(deps);
    await act(async () => {
      await result.current.handleMessageMutation('edit', 'm1', 'new text');
    });
    expect(h.mutateMessage).toHaveBeenCalledWith(
      'c1',
      'm1',
      { action: 'edit', base_content_hash: 'hash-1', client_request_id: 'req-1', content: 'new text' },
      'csrf',
    );
    expect(chatCtrl.clearPendingMessageMutation).toHaveBeenCalledWith('c1', 'req-1');
    expect(deps.streamResponseRun).toHaveBeenCalledWith('r2', 'c1', expect.any(Function));

    await act(async () => {
      await result.current.handleMessageMutation('regenerate', 'm1');
    });
    expect(h.mutateMessage.mock.calls[1][2]).toEqual({ action: 'regenerate', base_content_hash: 'hash-1', client_request_id: 'req-1' });
  });

  it('maps a pending-mutation conflict to retryOriginalRevisionFirst', async () => {
    const { deps, live } = makeDeps(
      { conversationId: 'c1' },
      {
        getOrCreatePendingMessageMutation: vi.fn((): never => {
          throw new PendingMessageMutationConflictError();
        }),
      },
    );
    const { result } = render(deps);
    await act(async () => {
      await result.current.handleMessageMutation('edit', 'm1', 'x');
    });
    expect(h.mutateMessage).not.toHaveBeenCalled();
    expect(live.patch).toHaveBeenLastCalledWith({ isPending: false, error: 'retryOriginalRevisionFirst' });
  });

  it('ignores a mutation while busy or for an unknown target', async () => {
    const busy = makeDeps({ conversationId: 'c1', isBusy: true });
    const busyHook = render(busy.deps);
    await act(async () => {
      await busyHook.result.current.handleMessageMutation('edit', 'm1', 'x');
    });
    const unknown = makeDeps({ conversationId: 'c1' });
    const unknownHook = render(unknown.deps);
    await act(async () => {
      await unknownHook.result.current.handleMessageMutation('edit', 'nope', 'x');
    });
    expect(h.mutateMessage).not.toHaveBeenCalled();
  });

  it('routes handleSend through an edit mutation while editing in full mode', async () => {
    const { deps } = makeDeps({ conversationId: 'c1', effectiveEditingMessageId: 'm1' });
    const { result } = render(deps);
    let ok = true;
    await act(async () => {
      ok = await result.current.handleSend('edited');
    });
    expect(ok).toBe(false);
    expect(h.mutateMessage).toHaveBeenCalledTimes(1);
    expect(h.sendMessage).not.toHaveBeenCalled();
  });

  it('restores the stashed pre-edit draft after a successful edit (R11)', async () => {
    const { deps, chatCtrl } = makeDeps({ conversationId: 'c1', effectiveEditingMessageId: 'm1' });
    (deps.clearEditing as ReturnType<typeof vi.fn>).mockReturnValue('half-typed thought');
    const { result } = render(deps);
    await act(async () => {
      await result.current.handleSend('edited');
    });
    expect(chatCtrl.setDraft).toHaveBeenLastCalledWith('half-typed thought');
  });

  it('leaves the composer draft alone for a retry that is not an edit session (R3)', async () => {
    const { deps, chatCtrl } = makeDeps({ conversationId: 'c1' });
    const { result } = render(deps);
    await act(async () => {
      await result.current.handleMessageMutation('edit', 'm1', 'same text');
    });
    expect(h.mutateMessage).toHaveBeenCalledTimes(1);
    expect(h.mutateMessage.mock.calls[0][2]).toMatchObject({ action: 'edit', content: 'same text' });
    expect(chatCtrl.setDraft).not.toHaveBeenCalled();
  });
});
