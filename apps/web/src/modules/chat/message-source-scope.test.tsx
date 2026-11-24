'use client';

import * as React from 'react';
import { NextIntlClientProvider } from 'next-intl';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { messages } from '@/core/messages';

vi.mock('@/core/query-provider', () => ({
  useDisplayPreferences: () => ({ confirmedPreferences: { locale: 'en-us', timezone: 'UTC' } }),
}));

vi.stubGlobal('matchMedia', (q: string) => ({ matches: false, media: q, addEventListener() {}, removeEventListener() {} }));
Element.prototype.scrollTo = () => undefined;

const h = vi.hoisted(() => ({ listChatSourceNames: vi.fn() }));

vi.mock('./api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('./api')>()),
  listChatSourceNames: h.listChatSourceNames,
}));

import { ChatTranscript } from './chat-transcript';
import { MessageSourceScope } from './message-source-scope';

/** Renders the scope line inside the providers the chat tree needs (intl and the query cache). */
function renderScope(sourceIds: string[] | undefined) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <NextIntlClientProvider locale="en-us" messages={messages['en-us']}>
      <QueryClientProvider client={client}>
        <MessageSourceScope sourceIds={sourceIds} />
      </QueryClientProvider>
    </NextIntlClientProvider>,
  );
}

describe('MessageSourceScope', () => {
  beforeEach(() => {
    h.listChatSourceNames.mockReset();
    h.listChatSourceNames.mockResolvedValue([
      { id: 'src-1', name: 'Bank feed' },
      { id: 'src-2', name: 'Calendar' },
    ]);
  });

  it('renders the label and a badge per known source name', async () => {
    renderScope(['src-2', 'src-1']);
    expect(await screen.findByText('Bank feed')).toBeInTheDocument();
    expect(screen.getByText('Calendar')).toBeInTheDocument();
    expect(screen.getByText('Only from')).toBeInTheDocument();
  });

  it('shows the unavailable text for an id that no longer resolves to a source', async () => {
    renderScope(['src-1', 'gone']);
    expect(await screen.findByText('Bank feed')).toBeInTheDocument();
    expect(screen.getByText('Unavailable source')).toBeInTheDocument();
  });

  it('renders nothing and does not query when the message has no scope', () => {
    const { container } = renderScope(undefined);
    expect(container).toBeEmptyDOMElement();
    expect(h.listChatSourceNames).not.toHaveBeenCalled();
  });

  it('renders nothing for an empty scope', () => {
    const { container } = renderScope([]);
    expect(container).toBeEmptyDOMElement();
  });
});

describe('scope line in the transcript', () => {
  it('shows the scope line under a user message that carries source_scope', async () => {
    h.listChatSourceNames.mockResolvedValue([{ id: 'src-1', name: 'Bank feed' }]);
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const scoped = {
      id: 'm1',
      conversation_id: 'c1',
      role: 'user',
      content: 'how much did I spend',
      client_request_id: null,
      model_identity: null,
      citations: [],
      response_id: 'r1',
      revision_of_message_id: null,
      created_at: '2026-01-01T00:00:00Z',
      source_scope: ['src-1'],
    } as never;
    render(
      <NextIntlClientProvider locale="en-US" messages={messages['en-us']}>
        <QueryClientProvider client={client}>
          <ChatTranscript messages={[scoped]} mode="full" />
        </QueryClientProvider>
      </NextIntlClientProvider>,
    );
    expect(await screen.findByText('Bank feed')).toBeInTheDocument();
    expect(screen.getByTestId('message-source-scope')).toBeInTheDocument();
  });
});
