import { describe, expect, it } from 'vitest';
import { messages } from '@/core/messages';

const NEW_KEYS = ['reasoningLive', 'reasoningDone', 'loading', 'moreActions', 'jumpToLatest', 'historyLoadFailed', 'deleteFailed', 'sandboxedBadge'] as const;

describe('chat catalog parity for the chat-fixes keys', () => {
  it.each(NEW_KEYS)('%s exists and is non-empty in en-us and vi-vi, and is translated', (key) => {
    const en = (messages['en-us'].chat as unknown as Record<string, string>)[key];
    const vi = (messages['vi-vi'].chat as unknown as Record<string, string>)[key];
    expect(en).toBeTruthy();
    expect(vi).toBeTruthy();
    expect(vi).not.toBe(en);
  });
});
