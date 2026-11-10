import { act, renderHook } from '@testing-library/react';
import type { ReactNode } from 'react';
import { beforeEach, describe, expect, it } from 'vitest';
import { ChatControllerProvider, useChatController } from './chat-controller';

const wrapper = ({ children }: { children: ReactNode }) => <ChatControllerProvider>{children}</ChatControllerProvider>;

describe('ChatController draft persistence', () => {
  beforeEach(() => window.sessionStorage.clear());

  it('mirrors the draft per conversation and restores it after a reload', () => {
    const first = renderHook(() => useChatController(), { wrapper });
    act(() => first.result.current.setActiveConversationId('c1'));
    act(() => first.result.current.setDraft('unsent text'));
    expect(window.sessionStorage.getItem('umwelt.chat.draft.c1')).toBe('unsent text');
    first.unmount();

    // A new provider is a page reload: the URL sync re-selects c1, which restores its draft.
    const second = renderHook(() => useChatController(), { wrapper });
    expect(second.result.current.draft).toBe('');
    act(() => second.result.current.selectConversation('c1'));
    expect(second.result.current.draft).toBe('unsent text');
    act(() => second.result.current.selectConversation('c2'));
    expect(second.result.current.draft).toBe('');
  });

  it('clears the stored draft when the draft is emptied after a send and on a new chat', () => {
    const { result } = renderHook(() => useChatController(), { wrapper });
    act(() => result.current.setActiveConversationId('c1'));
    act(() => result.current.setDraft('hello'));
    act(() => result.current.setDraft(''));
    expect(window.sessionStorage.getItem('umwelt.chat.draft.c1')).toBeNull();
    act(() => result.current.setDraft('again'));
    act(() => result.current.resetConversation());
    expect(result.current.draft).toBe('');
    // The previous conversation keeps its own draft.
    expect(window.sessionStorage.getItem('umwelt.chat.draft.c1')).toBe('again');
  });
});
