import { describe, expect, it, vi } from 'vitest';
import type { ChatMessage } from './api';
import { conversationToMarkdown, downloadMarkdown, exportFilename } from './conversation-export';

const labels = { user: 'You', assistant: 'Assistant', sources: 'Sources', untitled: 'Untitled' };

/** Minimal message fixture. */
const msg = (extra: Record<string, unknown>) => ({ id: 'm', created_at: '2026-01-01T00:00:00Z', ...extra }) as unknown as ChatMessage;

describe('conversationToMarkdown', () => {
  it('writes turns in order with numbered sources and skips system and empty turns', () => {
    const md = conversationToMarkdown(
      'Trip plan',
      [
        msg({ role: 'system', content: 'hidden' }),
        msg({ role: 'user', content: 'Where to?' }),
        msg({
          role: 'assistant',
          content: 'Hanoi [1] and more [2].',
          model_identity: 'model-x',
          citations: [
            { sourceType: 'web', url: 'https://example.com/a', title: 'Guide [2026]' },
            { sourceType: 'web', url: 'javascript:alert(1)', title: 'Bad' },
            { sourceType: 'document', documentId: 'd', documentVersionId: 'v', chunkId: 'c', title: 'My notes' },
          ],
        }),
        msg({ role: 'assistant', content: '   ' }),
      ],
      labels,
    );
    expect(md).toBe(
      [
        '# Trip plan',
        '## You\n\nWhere to?',
        '## model-x\n\nHanoi [1] and more [2].\n\n**Sources**\n\n1. [Guide \\[2026\\]](https://example.com/a)\n2. Bad\n3. My notes',
      ].join('\n\n') + '\n',
    );
    expect(md).not.toContain('javascript:');
  });

  it('falls back to the untitled label', () => {
    expect(conversationToMarkdown('  ', [], labels)).toBe('# Untitled\n');
  });
});

describe('exportFilename', () => {
  it('sanitizes the title, keeps Vietnamese letters and bounds the length', () => {
    expect(exportFilename('Kế hoạch: "Đà Nẵng"/2026?')).toBe('Kế hoạch Đà Nẵng 2026.md');
    expect(exportFilename('')).toBe('conversation.md');
    expect(exportFilename('..hidden')).toBe('hidden.md');
    expect(exportFilename('x'.repeat(200)).length).toBe(83);
  });
});

describe('downloadMarkdown', () => {
  it('downloads a Blob through a temporary link and revokes the URL', () => {
    const create = vi.fn(() => 'blob:x');
    const revoke = vi.fn();
    Object.assign(URL, { createObjectURL: create, revokeObjectURL: revoke });
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined);
    downloadMarkdown('a.md', '# hi');
    expect(create).toHaveBeenCalledTimes(1);
    expect((create.mock.calls[0] as unknown as [Blob])[0].type).toBe('text/markdown;charset=utf-8');
    expect(click).toHaveBeenCalledTimes(1);
    expect(revoke).toHaveBeenCalledWith('blob:x');
  });
});
