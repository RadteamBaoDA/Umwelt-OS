import { renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { ChatMessage } from './api';
import { useFollowupRefresh } from './use-followup-refresh';

const NOW = new Date('2026-11-10T10:00:00Z');

/** Assistant message created `ageMs` before NOW. */
function answer(over: Partial<ChatMessage> = {}, ageMs = 1000): ChatMessage {
  return {
    id: 'a1', conversation_id: 'c1', role: 'assistant', content: 'x', client_request_id: null, model_identity: null, citations: [],
    response_id: 'r', revision_of_message_id: null, created_at: new Date(NOW.getTime() - ageMs).toISOString(), ...over,
  };
}

describe('useFollowupRefresh', () => {
  beforeEach(() => { vi.useFakeTimers(); vi.setSystemTime(NOW); });
  afterEach(() => vi.useRealTimers());

  it('refreshes at ~4s and ~10s while the fresh answer has no suggestions', () => {
    const refresh = vi.fn();
    renderHook(() => useFollowupRefresh({ conversationId: 'c1', messages: [answer()], busy: false, refresh }));
    vi.advanceTimersByTime(3999);
    expect(refresh).not.toHaveBeenCalled();
    vi.advanceTimersByTime(1);
    expect(refresh).toHaveBeenCalledTimes(1);
    vi.advanceTimersByTime(6000);
    expect(refresh).toHaveBeenCalledTimes(2);
  });

  it('stops once suggestions arrive', () => {
    const refresh = vi.fn();
    const { rerender } = renderHook(
      (p: { messages: ChatMessage[] }) => useFollowupRefresh({ conversationId: 'c1', messages: p.messages, busy: false, refresh }),
      { initialProps: { messages: [answer()] } },
    );
    vi.advanceTimersByTime(4000);
    expect(refresh).toHaveBeenCalledTimes(1);
    rerender({ messages: [answer({ suggestions: ['a', 'b'] })] });
    vi.advanceTimersByTime(20000);
    expect(refresh).toHaveBeenCalledTimes(1);
  });

  it('cancels pending timers on conversation switch and unmount', () => {
    const refresh = vi.fn();
    const { rerender, unmount } = renderHook(
      (p: { id: string; messages: ChatMessage[] }) => useFollowupRefresh({ conversationId: p.id, messages: p.messages, busy: false, refresh }),
      { initialProps: { id: 'c1', messages: [answer()] } },
    );
    vi.advanceTimersByTime(2000);
    rerender({ id: 'c2', messages: [] });
    vi.advanceTimersByTime(3000); // c1's 4s timer would have fired here
    expect(refresh).not.toHaveBeenCalled();
    rerender({ id: 'c2', messages: [answer({ id: 'a9' })] });
    unmount();
    vi.advanceTimersByTime(20000);
    expect(refresh).not.toHaveBeenCalled();
  });

  it('does nothing while busy, for old answers or user-last threads', () => {
    const refresh = vi.fn();
    renderHook(() => useFollowupRefresh({ conversationId: 'c1', messages: [answer()], busy: true, refresh }));
    renderHook(() => useFollowupRefresh({ conversationId: 'c1', messages: [answer({}, 10 * 60 * 1000)], busy: false, refresh }));
    renderHook(() => useFollowupRefresh({ conversationId: 'c1', messages: [answer({ role: 'user' })], busy: false, refresh }));
    vi.advanceTimersByTime(20000);
    expect(refresh).not.toHaveBeenCalled();
  });
});
