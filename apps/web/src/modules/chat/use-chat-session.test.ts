import { describe, expect, it } from 'vitest';
import { canAddFiles } from './use-chat-session';

describe('canAddFiles', () => {
  it('allows files on an idle draft with no sources chosen', () => {
    expect(canAddFiles(false, false, false, false)).toBe(true);
  });

  it('blocks files while chosen @ sources hold the turn to their scope', () => {
    // Paste and drop must not attach files while chips exist: attaching disables the picker and drops the chips.
    expect(canAddFiles(false, false, false, true)).toBe(false);
  });

  it('keeps the existing busy, unavailable and full blocks', () => {
    expect(canAddFiles(true, false, false, false)).toBe(false);
    expect(canAddFiles(false, true, false, false)).toBe(false);
    expect(canAddFiles(false, false, true, false)).toBe(false);
  });
});
