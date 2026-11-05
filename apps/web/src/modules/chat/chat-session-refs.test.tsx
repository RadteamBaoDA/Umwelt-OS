import { renderHook } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { ChatSessionRuntime, useChatSessionRefs } from './chat-session-refs';

describe('useChatSessionRefs', () => {
  it('creates one runtime from the initial selection and keeps it across renders', () => {
    const { result, rerender } = renderHook(({ selection }) => useChatSessionRefs(selection), {
      initialProps: { selection: { conversationId: 'c1', generation: 4 } },
    });
    const first = result.current;
    expect(first).toBeInstanceOf(ChatSessionRuntime);
    expect(first.selection).toEqual({ conversationId: 'c1', generation: 4 });
    rerender({ selection: { conversationId: 'c2', generation: 5 } });
    expect(result.current).toBe(first);
    expect(result.current.selection.conversationId).toBe('c1');
  });
});

describe('ChatSessionRuntime', () => {
  it('detachStream aborts the active controller once and clears it', () => {
    const runtime = new ChatSessionRuntime({ conversationId: null, generation: 0 });
    const controller = new AbortController();
    runtime.setAbortController(controller);
    runtime.detachStream();
    expect(controller.signal.aborted).toBe(true);
    expect(runtime.abortController).toBeNull();
    expect(() => runtime.detachStream()).not.toThrow();
  });

  it('bumpSendGeneration increments monotonically and returns the new value', () => {
    const runtime = new ChatSessionRuntime({ conversationId: null, generation: 0 });
    expect([runtime.bumpSendGeneration(), runtime.bumpSendGeneration()]).toEqual([1, 2]);
    expect(runtime.sendGeneration).toBe(2);
  });

  it('stores the mirrored mutable state through its setters', () => {
    const runtime = new ChatSessionRuntime({ conversationId: null, generation: 0 });
    const run = vi.fn();
    runtime.setLastSent('hello');
    runtime.setMounted(true);
    runtime.setPendingMirror(true);
    runtime.setStreamRun(run);
    runtime.setPromotedSelection({ conversationId: 'c', generation: 1, sendGeneration: 2 });
    runtime.setAttachedStream(null);
    runtime.setSelection({ conversationId: 'c', generation: 1 });
    expect(runtime).toMatchObject({
      lastSent: 'hello',
      mounted: true,
      isPending: true,
      streamRun: run,
      selection: { conversationId: 'c', generation: 1 },
    });
    expect(runtime.promotedSelection).toEqual({ conversationId: 'c', generation: 1, sendGeneration: 2 });
  });
});
