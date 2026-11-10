import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { NextIntlClientProvider } from 'next-intl';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { messages } from '@/core/messages';

vi.mock('@/core/query-provider', () => ({ useDisplayPreferences: () => ({ confirmedPreferences: { locale: 'en-us', timezone: 'UTC' } }) }));

const topicsMock = vi.hoisted(() => ({ fetchTopics: vi.fn() }));
vi.mock('@/modules/news/api', () => topicsMock);

import { ChatTranscript } from './chat-transcript';
import { onCitationFocus } from './chat-citation-focus';

vi.stubGlobal('matchMedia', (q: string) => ({ matches: false, media: q, addEventListener() {}, removeEventListener() {} }));

/** Renders the transcript with English messages. */
function renderTranscript(props: Partial<React.ComponentProps<typeof ChatTranscript>>) {
  return render(
    <NextIntlClientProvider locale="en-US" messages={messages['en-us']}>
      <ChatTranscript messages={[]} mode="full" {...props} />
    </NextIntlClientProvider>,
  );
}

describe('ChatTranscript empty state', () => {
  beforeEach(() => { topicsMock.fetchTopics.mockReset(); topicsMock.fetchTopics.mockRejectedValue(new Error('offline')); });
  it('shows one sentence and three plain prompt buttons that send their prompt', async () => {
    const onSendPrompt = vi.fn();
    renderTranscript({ onSendPrompt });
    expect(screen.getByText(messages['en-us'].chat.emptyStateDescription)).toBeTruthy();
    const group = screen.getByTestId('starter-suggestions');
    const buttons = group.querySelectorAll('button');
    expect(buttons).toHaveLength(3);
    await userEvent.click(buttons[0]);
    expect(onSendPrompt).toHaveBeenCalledTimes(1);
  });

  it('builds chips from the owner topics and sends the topic prompt', async () => {
    topicsMock.fetchTopics.mockResolvedValue({ items: [{ name: 'AI chips' }, { name: 'Bitcoin' }], next_cursor: null });
    const onSendPrompt = vi.fn();
    renderTranscript({ onSendPrompt });
    const chip = await screen.findByRole('button', { name: "What's new on AI chips today?" });
    await waitFor(() => expect(screen.getByTestId('starter-suggestions').querySelectorAll('button')).toHaveLength(2));
    await userEvent.click(chip);
    expect(onSendPrompt).toHaveBeenCalledWith(expect.stringContaining('AI chips'));
  });

  it('keeps the fixed chips when there are no topics', async () => {
    topicsMock.fetchTopics.mockResolvedValue({ items: [], next_cursor: null });
    renderTranscript({ onSendPrompt: vi.fn() });
    await waitFor(() => expect(topicsMock.fetchTopics).toHaveBeenCalled());
    expect(screen.getByTestId('starter-suggestions').querySelectorAll('button')).toHaveLength(3);
  });
});

describe('ChatTranscript tool status', () => {
  it('shows a polite status line for the running tool', () => {
    renderTranscript({ isPending: true, activeTool: 'web_search' });
    expect(screen.getByTestId('tool-status').textContent).toBe(messages['en-us'].chat.toolSearchingWeb);
  });

  it('shows the data-query copy and nothing when idle', () => {
    const { rerender } = renderTranscript({ activeTool: 'query_collected_data' });
    expect(screen.getByTestId('tool-status').textContent).toBe(messages['en-us'].chat.toolQueryingData);
    rerender(
      <NextIntlClientProvider locale="en-US" messages={messages['en-us']}>
        <ChatTranscript messages={[]} mode="full" activeTool={null} />
      </NextIntlClientProvider>,
    );
    expect(screen.queryByTestId('tool-status')).toBeNull();
  });

  it('shows the searched query for a used web search', () => {
    renderTranscript({
      isStreaming: true,
      streamingWebSearch: { status: 'used', reason: null, result_count: 2, query: 'vinfast stock price' },
    });
    expect(screen.getByTestId('web-search-query').textContent).toBe('Searched “vinfast stock price” · 2 results');
  });
});

describe('ChatTranscript sources and model line', () => {
  const cited = {
    id: 'a1', role: 'assistant', content: 'Answer [1].', model_identity: 'model-x',
    citations: [{ sourceType: 'web', url: 'https://example.com/a', title: 'Example', quote: 'q' }],
  } as unknown as React.ComponentProps<typeof ChatTranscript>['messages'][number];

  beforeEach(() => { Element.prototype.scrollTo = vi.fn() as unknown as typeof Element.prototype.scrollTo; });

  it('shows the model and a clickable sources strip on the full page', async () => {
    const onSelectCitation = vi.fn();
    renderTranscript({ messages: [cited], conversationId: 'c1', onSelectCitation });
    expect(screen.getByTestId('message-model').textContent).toBe('Model: model-x');
    const strip = screen.getByTestId('sources-strip');
    expect(strip.textContent).toBe('1 sources');
    const received: string[][] = [];
    const off = onCitationFocus(() => received.push([]));
    await userEvent.click(strip);
    off();
    expect(received).toHaveLength(1);
    const chip = await waitFor(() => {
      const found = document.querySelector('a.cite');
      expect(found).not.toBeNull();
      return found as Element;
    });
    await userEvent.click(chip);
    expect(onSelectCitation).toHaveBeenCalledWith(cited.citations![0]);
  });

  it('keeps the drawer free of the strip and appends the count to the model line', () => {
    renderTranscript({ messages: [cited], mode: 'drawer' });
    expect(screen.queryByTestId('sources-strip')).toBeNull();
    expect(screen.getByTestId('message-model').textContent).toBe('Model: model-x · 1 sources');
  });
});
