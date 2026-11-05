import { act, renderHook } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { useChatComposerState } from './use-chat-composer-state';

describe('useChatComposerState', () => {
  it('starts with web search on, db query off and the assistant profile selected', () => {
    const { result } = renderHook(() => useChatComposerState());
    expect(result.current).toMatchObject({
      webSearchOn: true,
      dbQueryOn: false,
      contextPickerOpen: false,
      providerGateOpen: false,
      selectedProfileId: 'assistant',
      editingMessageId: null,
    });
    expect(result.current.fileInputRef.current).toBeNull();
  });

  it('updates each field independently', () => {
    const { result } = renderHook(() => useChatComposerState());
    act(() => {
      result.current.setContextPickerOpen(true);
      result.current.setProviderGateOpen(true);
      result.current.setSelectedProfileId('researcher');
      result.current.setEditingMessageId('m-1');
    });
    expect(result.current).toMatchObject({
      contextPickerOpen: true,
      providerGateOpen: true,
      selectedProfileId: 'researcher',
      editingMessageId: 'm-1',
    });
    expect(result.current.webSearchOn).toBe(true);
  });

  it('resetToggles restores only the two tool toggles and keeps a stable identity', () => {
    const { result } = renderHook(() => useChatComposerState());
    const reset = result.current.resetToggles;
    act(() => {
      result.current.setWebSearchOn(false);
      result.current.setDbQueryOn(true);
      result.current.setSelectedProfileId('researcher');
    });
    expect(result.current).toMatchObject({ webSearchOn: false, dbQueryOn: true });
    act(() => result.current.resetToggles());
    expect(result.current).toMatchObject({ webSearchOn: true, dbQueryOn: false, selectedProfileId: 'researcher' });
    expect(result.current.resetToggles).toBe(reset);
  });
});
