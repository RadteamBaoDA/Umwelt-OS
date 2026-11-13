import { describe, expect, it } from 'vitest';
import { getCatalog } from '@locales';

const en = getCatalog('en-us', 'chat');
const vi = getCatalog('vi-vi', 'chat');

/** Reads a dotted catalog path such as `rail.label` from a nested message tree. */
function lookup(tree: object, path: string): string {
  return path.split('.').reduce<unknown>((node, part) => (node as Record<string, unknown>)[part], tree) as string;
}

const NEW_KEYS = [
  'reasoningLive',
  'reasoningDone',
  'loading',
  'moreActions',
  'jumpToLatest',
  'conversationList.loadFailed',
  'deleteFailed',
  'sandboxedBadge',
  'new.chatShortcut',
  'stopGeneratingShortcut',
] as const;

describe('chat catalog parity for the chat-fixes keys', () => {
  it.each(NEW_KEYS)('%s exists and is non-empty in en-us and vi-vi, and is translated', (key) => {
    const enValue = lookup(en, key);
    const viValue = lookup(vi, key);
    expect(enValue).toBeTruthy();
    expect(viValue).toBeTruthy();
    expect(viValue).not.toBe(enValue);
  });
});

describe('chat review copy (D4, D5, D12)', () => {
  it('names the evidence rail "Sources" and pluralizes the source count', () => {
    expect(lookup(en, 'rail.label')).toBe('Sources');
    expect(lookup(vi, 'rail.label')).toBe('Nguồn');
    expect(lookup(en, 'sourcesCount')).toContain('plural');
    expect(lookup(vi, 'conversationList.archivedEmpty')).toBe('Chưa có cuộc trò chuyện nào được lưu trữ');
  });
});
