import { renderHook } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { ChatSessionRuntime } from './chat-session-refs';
import { useConversationSelection, type ConversationSelectionParams } from './use-conversation-selection';

function setup(over: Partial<ConversationSelectionParams> = {}) {
  const refs = new ChatSessionRuntime({ conversationId: null, generation: 0 });
  const live = { dispatch: vi.fn(), patch: vi.fn(), clearOutput: vi.fn() };
  const streamResponseRun = vi.fn<(responseId: string, conversationId: string, isCurrentView: () => boolean) => Promise<void>>(async () => undefined);
  const onSelectionReset = vi.fn();
  const params: ConversationSelectionParams = {
    refs, live, isPending: false, conversationId: 'c1', activeConversationId: 'c1', activeConversationGeneration: 1,
    activeResponseId: null, streamResponseRun, onSelectionReset, ...over,
  };
  return { refs, live, streamResponseRun, onSelectionReset, params };
}

describe('useConversationSelection', () => {
  it('resets the pending state when the selection changes without a promotion', () => {
    const { params, live, onSelectionReset, refs } = setup();
    renderHook(() => useConversationSelection(params));
    expect(onSelectionReset).toHaveBeenCalled();
    expect(live.clearOutput).toHaveBeenCalled();
    expect(live.patch).toHaveBeenCalledWith({ isPending: false, error: null, cancelled: false });
    expect(refs.mounted).toBe(true);
  });

  it('keeps isPending through the reset when the selection was promoted by the in-flight send', () => {
    const { params, live, refs } = setup({ conversationId: 'new1', activeConversationId: 'new1', activeConversationGeneration: 2 });
    refs.setPromotedSelection({ conversationId: 'new1', generation: 2, sendGeneration: refs.sendGeneration });
    renderHook(() => useConversationSelection(params));
    expect(live.patch).not.toHaveBeenCalledWith({ isPending: false, error: null, cancelled: false });
    expect(refs.promotedSelection).toBeNull();
  });

  it('does not honour a promotion that belongs to an orphaned send', () => {
    const { params, live, refs } = setup({ conversationId: 'new1', activeConversationId: 'new1', activeConversationGeneration: 2 });
    refs.setPromotedSelection({ conversationId: 'new1', generation: 2, sendGeneration: refs.sendGeneration + 5 });
    renderHook(() => useConversationSelection(params));
    expect(live.patch).toHaveBeenCalledWith({ isPending: false, error: null, cancelled: false });
  });

  it('reattaches to the owner-reported run and bumps the send generation', () => {
    const { params, streamResponseRun, refs, live } = setup({ activeResponseId: 'r1' });
    const before = refs.sendGeneration;
    renderHook(() => useConversationSelection(params));
    expect(streamResponseRun).toHaveBeenCalledWith('r1', 'c1', expect.any(Function));
    expect(refs.sendGeneration).toBe(before + 1);
    expect(live.patch).toHaveBeenCalledWith(expect.objectContaining({ activeResponseId: 'r1', isStreaming: true }));
    const isCurrent = streamResponseRun.mock.calls[0][2] as () => boolean;
    expect(isCurrent()).toBe(true);
    refs.bumpSendGeneration();
    expect(isCurrent()).toBe(false);
  });

  it('skips the reattach while a local send is pending', () => {
    const { params, streamResponseRun, refs } = setup({ activeResponseId: 'r1', isPending: true });
    const before = refs.sendGeneration;
    renderHook(() => useConversationSelection(params));
    expect(streamResponseRun).not.toHaveBeenCalled();
    expect(refs.sendGeneration).toBe(before);
  });

  it('skips the reattach when an identical live attachment already exists', () => {
    const { params, streamResponseRun, refs } = setup({ activeResponseId: 'r1' });
    refs.setAttachedStream({ responseId: 'r1', conversationId: 'c1', controller: new AbortController(), promise: Promise.resolve() });
    renderHook(() => useConversationSelection(params));
    expect(streamResponseRun).not.toHaveBeenCalled();
  });

  it('detaches the reader and orphans continuations on unmount', () => {
    const { params, refs } = setup();
    const ctrl = new AbortController();
    const { unmount } = renderHook(() => useConversationSelection(params));
    refs.setAbortController(ctrl);
    const before = refs.sendGeneration;
    unmount();
    expect(ctrl.signal.aborted).toBe(true);
    expect(refs.mounted).toBe(false);
    expect(refs.sendGeneration).toBe(before + 1);
  });
});
