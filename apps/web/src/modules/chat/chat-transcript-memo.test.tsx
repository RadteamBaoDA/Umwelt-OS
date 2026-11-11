import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render } from '@testing-library/react';
import { NextIntlClientProvider } from 'next-intl';
import { describe, expect, it, vi } from 'vitest';
import { messages } from '@/core/messages';
import type { ChatMessage } from './api';

vi.mock('@/core/query-provider', () => ({ useDisplayPreferences: () => ({ confirmedPreferences: { locale: 'en-us', timezone: 'UTC' } }) }));
vi.mock('@/modules/news/api', () => ({ fetchTopics: vi.fn() }));
const rendered = vi.hoisted(() => ({ settled: 0 }));
vi.mock('./chat-markdown', () => ({
  ChatMarkdown: ({ content }: { content: string }) => {
    if (content.startsWith('settled')) rendered.settled += 1;
    return <div>{content}</div>;
  },
}));

import { ChatTranscript } from './chat-transcript';

vi.stubGlobal('matchMedia', (q: string) => ({ matches: false, media: q, addEventListener() {}, removeEventListener() {} }));

Element.prototype.scrollTo = () => {};
Element.prototype.scrollIntoView = () => {};

const msgs = [
  { id: 'u1', role: 'user', content: 'hi', created_at: '2026-01-01T00:00:00Z' },
  { id: 'a1', role: 'assistant', content: 'settled answer one', created_at: '2026-01-01T00:00:01Z' },
  { id: 'u2', role: 'user', content: 'again', created_at: '2026-01-01T00:00:02Z' },
] as unknown as ChatMessage[];

/** Renders the transcript with fresh inline edit/regenerate/send handlers on every call (the transcript must stabilise them). */
function tree(streamingText: string) {
  return (
    <QueryClientProvider client={new QueryClient()}>
      <NextIntlClientProvider locale="en-US" messages={messages['en-us']}>
        <ChatTranscript
          messages={msgs}
          mode="full"
          isStreaming
          streamingText={streamingText}
          onEditMessage={() => {}}
          onRegenerateMessage={() => {}}
          onSendPrompt={() => {}}
          onWidgetSendPrompt={() => {}}
        />
      </NextIntlClientProvider>
    </QueryClientProvider>
  );
}

describe('ChatTranscript memoization (P4)', () => {
  it('does not re-render settled messages while streaming text grows', () => {
    const { rerender } = render(tree('a'));
    const before = rendered.settled;
    expect(before).toBeGreaterThan(0);
    for (const text of ['ab', 'abc', 'abcd', 'abcde']) rerender(tree(text));
    expect(rendered.settled).toBe(before);
  });
});

describe('ChatTranscript offscreen layout skipping (P7)', () => {
  it('defers older settled messages but never the newest message or the streaming bubble', () => {
    const { container } = render(tree('live'));
    const deferred = (el: Element | null) => (el?.className ?? '').includes('content-visibility');
    expect(deferred(container.querySelector('[data-message-id="u1"]'))).toBe(true);
    expect(deferred(container.querySelector('[data-message-id="a1"]'))).toBe(true);
    expect(deferred(container.querySelector('[data-message-id="u2"]'))).toBe(false);
    expect(deferred(container.querySelector('[data-streaming-answer]'))).toBe(false);
  });
});
