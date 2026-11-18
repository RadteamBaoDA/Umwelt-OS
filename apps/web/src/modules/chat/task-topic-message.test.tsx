import { render, screen } from '@testing-library/react';
import { NextIntlClientProvider } from 'next-intl';
import { describe, expect, it, vi } from 'vitest';
import { messages } from '@/core/messages';

vi.mock('@/core/query-provider', () => ({ useDisplayPreferences: () => ({ locale: 'en-us', timezone: 'UTC' }) }));

import { TaskTopicMessage, isTaskTopicMessage } from './task-topic-message';
import type { ChatMessage } from './api';

const msg = {
  id: 'm1',
  role: 'assistant',
  content: '- First: Summary one',
  created_at: '2026-11-18T07:00:00Z',
  citations: [],
  // Shape of the generated MessageRead.task for a topic-watch post (source_id is never exposed).
  task: {
    task_kind: 'topic_watch',
    run_id: 'run-9',
    automation_id: 'auto-1',
    coverage: 'complete',
    stats: { items: 2, queries: 3, fetched: 4, tokens: 900 },
    items: [
      { title: 'First', url: 'https://example.com/a', site: 'example.com', summary: 'Summary one', document_id: 'd1' },
      { title: '<b>Bold</b> second', url: 'https://example.org/b', site: 'example.org', summary: '<img src=x onerror=alert(1)>' },
    ],
  },
} as unknown as ChatMessage;

function view(m: ChatMessage, locale: 'en-us' | 'vi-vi' = 'en-us') {
  return render(
    <NextIntlClientProvider locale={locale === 'en-us' ? 'en-US' : 'vi-VN'} messages={messages[locale]}>
      <TaskTopicMessage msg={m} />
    </NextIntlClientProvider>,
  );
}

describe('TaskTopicMessage', () => {
  it.each([['en-us'], ['vi-vi']] as const)('renders run line, linked items and site in %s', (locale) => {
    view(msg, locale);
    expect(screen.getByTestId('task-topic-message')).toHaveTextContent(messages[locale].chat.taskThread.topicRunLine.split('{')[0]);
    expect(screen.getByRole('link', { name: 'First' })).toHaveAttribute('href', '/knowledge/documents/d1');
    const external = screen.getAllByRole('link', { name: messages[locale].chat.taskThread.openOriginal });
    expect(external[0]).toHaveAttribute('href', 'https://example.com/a');
    expect(external[0]).toHaveAttribute('rel', 'noopener noreferrer');
    expect(external[0]).toHaveAttribute('target', '_blank');
    expect(screen.getByText('example.com')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: messages[locale].chat.taskThread.runLog })).toHaveAttribute(
      'href',
      '/settings/scheduled-tasks?run=run-9&task=auto-1',
    );
  });

  it('uses the external URL for the title when there is no document id, and renders text only', () => {
    const { container } = view(msg);
    const second = screen.getByRole('link', { name: '<b>Bold</b> second' });
    expect(second).toHaveAttribute('href', 'https://example.org/b');
    expect(second).toHaveAttribute('target', '_blank');
    expect(screen.getByText('<img src=x onerror=alert(1)>')).toBeInTheDocument();
    expect(container.querySelector('img, b')).toBeNull();
  });

  it('shows a Partial badge only for partial coverage', () => {
    view({ ...msg, task: { ...msg.task, coverage: 'partial', stats: { reason: 'budget' } } } as unknown as ChatMessage);
    expect(screen.getByText(messages['en-us'].chat.taskThread.partial)).toBeInTheDocument();
  });

  it('shows the localized stats line and partial reason', () => {
    view({
      ...msg,
      task: { ...msg.task, coverage: 'partial', stats: { ...(msg.task as object as { stats: object }).stats, reason: 'budget' } },
    } as unknown as ChatMessage);
    expect(screen.getByText('3 searches · 4 pages · 2 kept · 900 tokens')).toBeInTheDocument();
    expect(screen.getByText('Token budget reached')).toBeInTheDocument();
  });

  it('does not render a complete post as partial', () => {
    view(msg);
    expect(screen.queryByText(messages['en-us'].chat.taskThread.partial)).toBeNull();
  });

  it('refuses non-http urls and survives malformed items', () => {
    const bad = {
      ...msg,
      task: { ...msg.task, items: [null, { title: 5 }, { title: 'Evil', url: 'javascript:alert(1)' }, 'x'] },
    } as unknown as ChatMessage;
    view(bad);
    expect(screen.getByText('Evil')).toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'Evil' })).toBeNull();
  });

  it('survives a non-array items payload and detects only topic posts', () => {
    view({ ...msg, task: { ...msg.task, items: 'oops' } } as unknown as ChatMessage);
    expect(screen.getByTestId('task-topic-message')).toBeInTheDocument();
    expect(isTaskTopicMessage(msg)).toBe(true);
    expect(isTaskTopicMessage({ ...msg, task: { task_kind: 'daily_brief' } } as unknown as ChatMessage)).toBe(false);
    expect(isTaskTopicMessage({ ...msg, task: null } as ChatMessage)).toBe(false);
  });
});
