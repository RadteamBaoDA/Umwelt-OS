import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { NextIntlClientProvider } from 'next-intl';
import { describe, expect, it, vi } from 'vitest';
import { messages } from '@/core/messages';
import type { ChatMessage } from './api';

vi.mock('@/core/query-provider', () => ({ useDisplayPreferences: () => ({ confirmedPreferences: { locale: 'en-us', timezone: 'UTC' } }) }));
vi.mock('@/modules/news/api', () => ({ fetchTopics: vi.fn().mockRejectedValue(new Error('offline')) }));

import { ChatTranscript } from './chat-transcript';

Element.prototype.scrollTo = vi.fn();
vi.stubGlobal('matchMedia', (q: string) => ({ matches: false, media: q, addEventListener() {}, removeEventListener() {} }));

/** Minimal persisted message. */
function msg(id: string, role: 'user' | 'assistant', over: Partial<ChatMessage> = {}): ChatMessage {
  return {
    id, conversation_id: 'c1', role, content: `${role} ${id}`, client_request_id: null, model_identity: null, citations: [],
    response_id: `r-${id}`, revision_of_message_id: null, created_at: '2026-11-10T10:00:00Z', ...over,
  };
}

/** Renders the transcript with English messages. */
function renderTranscript(props: Partial<React.ComponentProps<typeof ChatTranscript>>) {
  return render(
    <NextIntlClientProvider locale="en-US" messages={messages['en-us']}>
      <ChatTranscript conversationId="c1" messages={[]} mode="full" {...props} />
    </NextIntlClientProvider>,
  );
}

describe('follow-up suggestion chips', () => {
  const thread = [
    msg('u1', 'user'),
    msg('a1', 'assistant', { suggestions: ['old one', 'old two'] }),
    msg('u2', 'user'),
    msg('a2', 'assistant', { suggestions: ['What about Q4?', 'Show a chart'] }),
  ];

  it('renders chips for the latest assistant answer only and sends the clicked one', async () => {
    const onSendPrompt = vi.fn();
    renderTranscript({ messages: thread, onSendPrompt });
    const group = screen.getByRole('group', { name: messages['en-us'].chat.suggestionsLabel });
    expect(group.querySelectorAll('button')).toHaveLength(2);
    expect(screen.queryByText('old one')).toBeNull();
    await userEvent.click(screen.getByRole('button', { name: 'Show a chart' }));
    expect(onSendPrompt).toHaveBeenCalledWith('Show a chart');
  });

  it('is keyboard activatable', async () => {
    const onSendPrompt = vi.fn();
    renderTranscript({ messages: thread, onSendPrompt });
    screen.getByRole('button', { name: 'What about Q4?' }).focus();
    await userEvent.keyboard('{Enter}');
    expect(onSendPrompt).toHaveBeenCalledWith('What about Q4?');
  });

  it('is hidden while a response is streaming or pending', () => {
    const { rerender } = renderTranscript({ messages: thread, onSendPrompt: vi.fn(), isStreaming: true });
    expect(screen.queryByTestId('followup-suggestions')).toBeNull();
    rerender(
      <NextIntlClientProvider locale="en-US" messages={messages['en-us']}>
        <ChatTranscript conversationId="c1" messages={thread} mode="full" onSendPrompt={vi.fn()} isPending />
      </NextIntlClientProvider>,
    );
    expect(screen.queryByTestId('followup-suggestions')).toBeNull();
  });

  it('renders nothing when the latest answer has no suggestions', () => {
    renderTranscript({ messages: [msg('u1', 'user'), msg('a1', 'assistant')], onSendPrompt: vi.fn() });
    expect(screen.queryByTestId('followup-suggestions')).toBeNull();
  });
});

describe('processing step list', () => {
  const steps = [
    { tool: 'retrieval', state: 'done' as const, detail: { documents: 3 } },
    { tool: 'web_search', state: 'running' as const },
  ];

  it('lists localized running and finished steps while streaming', () => {
    renderTranscript({ isStreaming: true, steps });
    const list = screen.getByRole('list', { name: messages['en-us'].chat.stepsLabel });
    expect(list.textContent).toContain('Found 3 documents');
    expect(list.textContent).toContain('Searching the web…');
    // The running step replaces the single status line.
    expect(screen.queryByTestId('tool-status')).toBeNull();
  });

  it('uses generic copy and the label for unknown tools', () => {
    renderTranscript({ isStreaming: true, steps: [{ tool: 'frobnicate', state: 'running', label: 'Widget' }, { tool: 'read_skill', state: 'done', label: 'budgeting' }] });
    expect(screen.getByText('Working: Widget…')).toBeTruthy();
    expect(screen.getByText('Read skill budgeting')).toBeTruthy();
  });

  it('collapses to a one-line summary after the answer and expands on demand', async () => {
    renderTranscript({ messages: [msg('u1', 'user'), msg('a1', 'assistant')], steps: [steps[0], { tool: 'web_search', state: 'done' }, { tool: 'read_skill', state: 'done', label: 's' }] });
    const toggle = screen.getByRole('button', { name: /3 steps/ });
    expect(toggle.getAttribute('aria-expanded')).toBe('false');
    expect(screen.queryByRole('list', { name: messages['en-us'].chat.stepsLabel })).toBeNull();
    await userEvent.click(toggle);
    expect(toggle.getAttribute('aria-expanded')).toBe('true');
    expect(screen.getByRole('list', { name: messages['en-us'].chat.stepsLabel }).querySelectorAll('li')).toHaveLength(3);
  });
});
