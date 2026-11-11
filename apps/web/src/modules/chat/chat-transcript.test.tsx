import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { NextIntlClientProvider } from 'next-intl';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { messages } from '@/core/messages';

vi.mock('@/core/query-provider', () => ({ useDisplayPreferences: () => ({ confirmedPreferences: { locale: 'en-us', timezone: 'UTC' } }) }));

const topicsMock = vi.hoisted(() => ({ fetchTopics: vi.fn() }));
vi.mock('@/modules/news/api', () => topicsMock);

import { ChatTranscript } from './chat-transcript';
import { onCitationFocus } from './chat-citation-focus';
import { ChatSideRail } from './chat-side-rail';

const queryClient = new QueryClient();
beforeEach(() => queryClient.clear());

vi.stubGlobal('matchMedia', (q: string) => ({ matches: false, media: q, addEventListener() {}, removeEventListener() {} }));

/** Renders the transcript with English messages. */
function renderTranscript(props: Partial<React.ComponentProps<typeof ChatTranscript>>) {
  return render(
    <QueryClientProvider client={queryClient}>
      <NextIntlClientProvider locale="en-US" messages={messages['en-us']}>
        <ChatTranscript messages={[]} mode="full" {...props} />
      </NextIntlClientProvider>
    </QueryClientProvider>,
  );
}

describe('ChatTranscript empty state', () => {
  beforeEach(() => {
    topicsMock.fetchTopics.mockReset();
    topicsMock.fetchTopics.mockRejectedValue(new Error('offline'));
  });
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
      <QueryClientProvider client={queryClient}>
        <NextIntlClientProvider locale="en-US" messages={messages['en-us']}>
          <ChatTranscript messages={[]} mode="full" activeTool={null} />
        </NextIntlClientProvider>
      </QueryClientProvider>,
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
    id: 'a1',
    role: 'assistant',
    content: 'Answer [1].',
    model_identity: 'model-x',
    citations: [{ sourceType: 'web', url: 'https://example.com/a', title: 'Example', quote: 'q' }],
  } as unknown as React.ComponentProps<typeof ChatTranscript>['messages'][number];

  beforeEach(() => {
    Element.prototype.scrollTo = vi.fn() as unknown as typeof Element.prototype.scrollTo;
  });

  it('shows the model and a clickable sources strip on the full page', async () => {
    const onSelectCitation = vi.fn();
    renderTranscript({ messages: [cited], conversationId: 'c1', onSelectCitation });
    expect(screen.getByTestId('message-model').textContent).toBe('model-x');
    const strip = screen.getByTestId('sources-strip');
    expect(strip.textContent).toBe('1 source');
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

  it('chip click opens the rail scoped to that answer and highlights the clicked source (D2)', async () => {
    const two = {
      ...cited,
      content: 'First [1] and second [2].',
      citations: [
        { sourceType: 'web', url: 'https://example.com/a', title: 'Alpha', quote: 'qa' },
        { sourceType: 'web', url: 'https://example.com/b', title: 'Beta', quote: 'qb' },
      ],
    } as unknown as typeof cited;
    const other = { sourceType: 'web', url: 'https://example.com/z', title: 'Zeta', quote: 'qz' };
    render(
      <QueryClientProvider client={queryClient}>
        <NextIntlClientProvider locale="en-US" messages={messages['en-us']}>
          <ChatTranscript messages={[two]} mode="full" conversationId="c-chip" />
          <ChatSideRail citations={[...(two.citations as never[]), other as never]} conversationId="c-chip" />
        </NextIntlClientProvider>
      </QueryClientProvider>,
    );
    const chip = await waitFor(() => {
      const found = document.querySelector('a.cite[data-cite="2"]');
      expect(found).not.toBeNull();
      return found as Element;
    });
    await userEvent.click(chip);
    const cards = screen.getAllByTestId('rail-source');
    expect(cards).toHaveLength(2);
    expect(cards[1].getAttribute('aria-current')).toBe('true');
    expect(cards[0].getAttribute('aria-current')).not.toBe('true');
  });

  it('keeps the drawer free of the strip button and shows the count as text in the meta row', () => {
    renderTranscript({ messages: [cited], mode: 'drawer' });
    expect(screen.queryByTestId('sources-strip')).toBeNull();
    expect(screen.getByTestId('message-model').textContent).toBe('model-x');
    expect(screen.getByText('1 source')).toBeTruthy();
  });

  it('expands the sources inline under the answer in the drawer, tap-friendly (C13)', async () => {
    renderTranscript({ messages: [cited], mode: 'drawer' });
    expect(screen.queryByText('Example')).toBeNull();
    const toggle = screen.getByTestId('sources-toggle');
    expect(toggle.getAttribute('aria-expanded')).toBe('false');
    await userEvent.click(toggle);
    expect(toggle.getAttribute('aria-expanded')).toBe('true');
    expect(screen.getByText(/Example/)).toBeTruthy();
  });
});

describe('ChatTranscript capability chips', () => {
  beforeEach(() => {
    topicsMock.fetchTopics.mockReset();
    topicsMock.fetchTopics.mockRejectedValue(new Error('offline'));
  });

  it('labels the two button rows and names each group by its visible label (C16)', () => {
    renderTranscript({ onSendPrompt: vi.fn(), capabilities: { webSearch: { pressed: true, onActivate: vi.fn() } } });
    expect(screen.getByRole('group', { name: 'Try asking' })).toBe(screen.getByTestId('starter-suggestions'));
    expect(screen.getByRole('group', { name: 'Tools for this message' })).toBe(screen.getByTestId('capability-chips'));
  });

  it('replaces the starters with a model setup notice when no chat model is configured (C16)', () => {
    renderTranscript({ onSendPrompt: vi.fn(), capabilities: { providerMissing: true } });
    expect(screen.queryByTestId('starter-suggestions')).toBeNull();
    expect(screen.getByRole('link', { name: messages['en-us'].chat.providerGateAction }).getAttribute('href')).toBe('/settings/models');
    // The notice says the chat needs a model; the composer-gate wording ("your message is kept") would be wrong here.
    expect(screen.getByText('Chat needs a model. Add one in Settings to start.')).toBeTruthy();
    expect(screen.queryByText(messages['en-us'].chat.providerGateBody)).toBeNull();
  });

  it('toggles composer options and opens the picker without sending a message', async () => {
    const onSendPrompt = vi.fn();
    const web = { pressed: false, onActivate: vi.fn() };
    const attach = { onActivate: vi.fn() };
    renderTranscript({ onSendPrompt, capabilities: { webSearch: web, attach } });
    expect(screen.getByTestId('capability-web').getAttribute('aria-pressed')).toBe('false');
    expect(screen.queryByTestId('capability-data')).toBeNull();
    await userEvent.click(screen.getByTestId('capability-web'));
    await userEvent.click(screen.getByTestId('capability-attach'));
    expect(web.onActivate).toHaveBeenCalledTimes(1);
    expect(attach.onActivate).toHaveBeenCalledTimes(1);
    expect(onSendPrompt).not.toHaveBeenCalled();
  });

  it('disables a chip with its reason', async () => {
    const web = { pressed: false, disabledReason: 'Not set up — Settings › AI', onActivate: vi.fn() };
    renderTranscript({ onSendPrompt: vi.fn(), capabilities: { webSearch: web } });
    const chip = screen.getByTestId('capability-web') as HTMLButtonElement;
    expect(chip.disabled).toBe(true);
    expect(chip.title).toBe('Not set up — Settings › AI');
    await userEvent.click(chip);
    expect(web.onActivate).not.toHaveBeenCalled();
    expect(screen.queryByTestId('capability-attach')).toBeNull();
  });
});

/** Minimal committed message for the stop / edit tests. */
function msg(id: string, role: 'user' | 'assistant', extra: Record<string, unknown> = {}) {
  return {
    id,
    conversation_id: 'c1',
    role,
    content: `${role} ${id}`,
    client_request_id: null,
    model_identity: null,
    citations: [],
    response_id: 'r-' + id,
    revision_of_message_id: null,
    created_at: '2026-01-01T00:00:00Z',
    ...extra,
  } as never;
}

describe('ChatTranscript stop, status line and empty conversation', () => {
  it('keeps the stopped partial without spinner or status label and offers Retry for the unanswered prompt', async () => {
    const onRegenerateMessage = vi.fn();
    const onRetryMessage = vi.fn();
    renderTranscript({
      conversationId: 'c1',
      messages: [msg('u1', 'user')],
      streamingText: 'partial answer',
      cancelled: true,
      onRegenerateMessage,
      onRetryMessage,
    });
    expect(screen.getByText('partial answer')).toBeTruthy();
    expect(screen.queryByText(messages['en-us'].chat.streaming)).toBeNull();
    expect(screen.queryByText(messages['en-us'].chat.thinking)).toBeNull();
    expect(screen.getByTestId('chat-cancelled').textContent).toBe(messages['en-us'].chat.cancelled);
    expect(screen.queryByRole('button', { name: messages['en-us'].chat.regenerateAnswer })).toBeNull();
    await userEvent.click(screen.getByRole('button', { name: messages['en-us'].chat.retry }));
    expect(onRetryMessage).toHaveBeenCalledWith(expect.objectContaining({ id: 'u1' }));
  });

  it('keeps the live partial when stopping a regenerate leaves the OLD answer as the last message', () => {
    renderTranscript({
      conversationId: 'c1',
      messages: [msg('u1', 'user'), msg('a1', 'assistant', { content: 'old answer' })],
      streamingText: 'new partial',
      cancelled: true,
      stoppedResponseId: 'r-new',
      onRegenerateMessage: vi.fn(),
    });
    expect(screen.getByText('new partial')).toBeTruthy();
    // The stopped notice offers no action (the old answer's own message toolbar is separate).
    expect(screen.getByTestId('chat-cancelled').parentElement?.querySelector('button')).toBeNull();
  });

  it('does not duplicate the partial once the stopped run answer is the last message, and Regenerate targets it', async () => {
    const onRegenerateMessage = vi.fn();
    renderTranscript({
      conversationId: 'c1',
      messages: [msg('u1', 'user'), msg('a1', 'assistant', { content: 'partial answer' })],
      streamingText: 'partial answer',
      cancelled: true,
      stoppedResponseId: 'r-a1',
      onRegenerateMessage,
    });
    expect(screen.getAllByText('partial answer')).toHaveLength(1);
    const buttons = screen.getAllByRole('button', { name: messages['en-us'].chat.regenerateAnswer });
    await userEvent.click(buttons[buttons.length - 1]);
    expect(onRegenerateMessage).toHaveBeenCalledWith(expect.objectContaining({ id: 'a1' }));
  });

  it('hides the status line once answer text has started but keeps it while a tool runs', () => {
    const { rerender } = renderTranscript({ conversationId: 'c1', messages: [msg('u1', 'user')], isStreaming: true });
    expect(screen.getByText(messages['en-us'].chat.streaming)).toBeTruthy();
    rerender(
      <QueryClientProvider client={queryClient}>
        <NextIntlClientProvider locale="en-US" messages={messages['en-us']}>
          <ChatTranscript messages={[msg('u1', 'user')]} mode="full" isStreaming streamingText="Hello" />
        </NextIntlClientProvider>
      </QueryClientProvider>,
    );
    expect(screen.queryByText(messages['en-us'].chat.streaming)).toBeNull();
  });

  it('shows the empty state for a loaded conversation without messages, not while it loads', () => {
    const { unmount } = renderTranscript({ conversationId: 'c1', onSendPrompt: vi.fn() });
    expect(screen.getByTestId('starter-suggestions')).toBeTruthy();
    unmount();
    renderTranscript({ conversationId: 'c1', loading: true, onSendPrompt: vi.fn() });
    expect(screen.queryByTestId('starter-suggestions')).toBeNull();
  });

  it('highlights the message being edited and wraps the newest turn in full Chat', () => {
    const { container } = renderTranscript({
      conversationId: 'c1',
      messages: [msg('u1', 'user'), msg('a1', 'assistant'), msg('u2', 'user')],
      editingMessageId: 'u2',
    });
    expect(container.querySelector('[data-message-id="u2"]')?.getAttribute('data-editing')).toBe('true');
    expect(container.querySelector('[data-message-id="u1"]')?.getAttribute('data-editing')).toBeNull();
    expect(container.querySelector('[data-latest-turn] [data-message-id="u2"]')).toBeTruthy();
    expect(container.querySelector('[data-latest-turn] [data-message-id="u1"]')).toBeNull();
  });
});

describe('ChatTranscript meta row and steps (C10, C12, C19, U3)', () => {
  const answer = {
    id: 'a1',
    role: 'assistant',
    content: 'Answer',
    model_identity: 'model-x',
    created_at: '2026-06-15T10:42:00Z',
    response_id: 'r1',
    citations: [
      { sourceType: 'web', url: 'https://example.com/a', title: 'A', quote: 'q' },
      { sourceType: 'web', url: 'https://example.com/b', title: 'B', quote: 'q2' },
    ],
  } as unknown as React.ComponentProps<typeof ChatTranscript>['messages'][number];

  beforeEach(() => {
    Element.prototype.scrollTo = vi.fn() as unknown as typeof Element.prototype.scrollTo;
  });

  it('renders icon-only copy and regenerate with name, title and 44px coarse-pointer targets', () => {
    renderTranscript({ messages: [answer], conversationId: 'c1', onRegenerateMessage: vi.fn() });
    for (const name of ['Copy message', 'Regenerate answer']) {
      const button = screen.getByRole('button', { name });
      expect(button.getAttribute('title')).toBe(name);
      expect(button.textContent).toBe('');
      expect(button.className).toContain('pointer-coarse:min-h-11');
    }
    expect(screen.getByTestId('sources-strip').textContent).toBe('2 sources');
  });

  it('opens the rail on the whole answer from the sources button and from a chip click', async () => {
    renderTranscript({ messages: [answer], conversationId: 'c1' });
    let scope = 0;
    const off = onCitationFocus(() => {
      scope += 1;
    });
    await userEvent.click(screen.getByTestId('sources-strip'));
    off();
    expect(scope).toBe(1);
  });

  it('shows the localized "not based on your documents" label only for an uncited answer (Q2)', () => {
    const { rerender } = renderTranscript({ messages: [{ ...answer, uncited: true }] as never, conversationId: 'c1' });
    expect(screen.getByTestId('message-uncited').textContent).toBe('Not based on your documents');
    rerender(
      <QueryClientProvider client={queryClient}>
        <NextIntlClientProvider locale="en-US" messages={messages['en-us']}>
          <ChatTranscript messages={[answer] as never} mode="full" conversationId="c1" />
        </NextIntlClientProvider>
      </QueryClientProvider>,
    );
    expect(screen.queryByTestId('message-uncited')).toBeNull();
  });

  it('hides a retrieval step that found no documents but keeps one that found some (Q6)', () => {
    renderTranscript({
      messages: [answer],
      conversationId: 'c1',
      steps: [{ tool: 'retrieval', state: 'done', detail: { documents: 0 } }] as never,
    });
    expect(screen.queryByTestId('step-list')).toBeNull();
    cleanup();
    renderTranscript({
      messages: [answer],
      conversationId: 'c1',
      steps: [{ tool: 'retrieval', state: 'done', detail: { documents: 2 } }] as never,
    });
    expect(screen.getByTestId('step-list')).toBeTruthy();
  });

  it('keeps "N steps" above the answer once the run settled', () => {
    renderTranscript({ messages: [answer], conversationId: 'c1', steps: [{ tool: 'retrieval', state: 'done' }] as never });
    const steps = screen.getByTestId('step-list');
    const answerText = screen.getByText('Answer');
    expect(steps.compareDocumentPosition(answerText) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it('anchors the empty state above the composer', () => {
    topicsMock.fetchTopics.mockRejectedValue(new Error('offline'));
    renderTranscript({ onSendPrompt: vi.fn() });
    expect(screen.getByTestId('starter-suggestions').closest('.mt-auto')).not.toBeNull();
  });

  it('turns an unconfigured web-search chip into a settings link', () => {
    topicsMock.fetchTopics.mockRejectedValue(new Error('offline'));
    renderTranscript({
      onSendPrompt: vi.fn(),
      capabilities: { webSearch: { disabledReason: 'Not set up', href: '/settings/web-search', onActivate: vi.fn() } },
    });
    expect(screen.getByTestId('capability-web').getAttribute('href')).toBe('/settings/web-search');
  });
});
