import { describe, expect, it } from 'vitest';
import { messages } from '@/core/messages';

const NEW_KEYS = [
  'reasoningLive',
  'reasoningDone',
  'loading',
  'moreActions',
  'jumpToLatest',
  'historyLoadFailed',
  'deleteFailed',
  'sandboxedBadge',
  'newChatShortcut',
  'stopGeneratingShortcut',
] as const;

describe('chat catalog parity for the chat-fixes keys', () => {
  it.each(NEW_KEYS)('%s exists and is non-empty in en-us and vi-vi, and is translated', (key) => {
    const en = (messages['en-us'].chat as unknown as Record<string, string>)[key];
    const vi = (messages['vi-vi'].chat as unknown as Record<string, string>)[key];
    expect(en).toBeTruthy();
    expect(vi).toBeTruthy();
    expect(vi).not.toBe(en);
  });
});

describe('chat review copy (D4, D5, D12)', () => {
  const en = messages['en-us'].chat as unknown as Record<string, string>;
  const vi = messages['vi-vi'].chat as unknown as Record<string, string>;
  it('names the evidence rail "Sources" and pluralizes the source count', () => {
    expect(en.railLabel).toBe('Sources');
    expect(vi.railLabel).toBe('Nguồn');
    expect(en.sourcesCount).toContain('plural');
    expect(vi.historyArchivedEmpty).toBe('Chưa có cuộc trò chuyện nào được lưu trữ');
  });
});
