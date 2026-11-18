import { render, screen } from '@testing-library/react';
import { NextIntlClientProvider } from 'next-intl';
import { describe, expect, it, vi } from 'vitest';
import { messages } from '@/core/messages';

vi.mock('@/core/query-provider', () => ({ useDisplayPreferences: () => ({ locale: 'en-us', timezone: 'UTC' }) }));
vi.mock('@/modules/dashboard/public-api', () => ({
  BriefText: ({ brief }: { brief: { content: string } }) => <p>{brief.content}</p>,
}));

import { TaskBriefMessage, isTaskBriefMessage, scheduledTaskThread } from './task-brief-message';
import type { ChatMessage } from './api';

const msg = {
  id: 'm1',
  role: 'assistant',
  content: 'Markets are up [1].',
  created_at: '2026-11-18T07:00:00Z',
  citations: [],
  // Shape of the generated MessageRead.task (private metadata_json keys are never exposed).
  task: {
    task_kind: 'daily_brief',
    run_id: 'run-9',
    brief_id: 'b1',
    brief_revision: 2,
    brief_citations: [{ ref: 1, kind: 'story', id: 's1', title: 'Market wrap', source_ids: [] }],
  },
} as unknown as ChatMessage;

describe('TaskBriefMessage', () => {
  it.each([['en-us'], ['vi-vi']] as const)('renders run line, text and brief citation in %s', (locale) => {
    render(
      <NextIntlClientProvider locale={locale === 'en-us' ? 'en-US' : 'vi-VN'} messages={messages[locale]}>
        <TaskBriefMessage msg={msg} />
      </NextIntlClientProvider>,
    );
    expect(screen.getByText('Markets are up [1].')).toBeInTheDocument();
    expect(screen.getByRole('listitem')).toHaveTextContent('[1] Market wrap');
    expect(screen.getByRole('link', { name: messages[locale].chat.taskThread.runLog })).toHaveAttribute(
      'href',
      '/settings/scheduled-tasks?run=run-9',
    );
  });

  it('detects only daily-brief messages and task threads', () => {
    expect(isTaskBriefMessage(msg)).toBe(true);
    expect(isTaskBriefMessage({ ...msg, task: null } as ChatMessage)).toBe(false);
    expect(scheduledTaskThread({ context_kind: 'automation', metadata: { scheduled_task: { kind: 'daily_brief' } } })).toBe(true);
    expect(scheduledTaskThread({ context_kind: 'chat', metadata: {} })).toBe(false);
  });

  it('renders without crashing on malformed brief_citations', () => {
    const bad = { ...msg, task: { ...msg.task, brief_citations: 'oops' } } as unknown as ChatMessage;
    const mixed = {
      ...msg,
      task: { ...msg.task, brief_citations: [null, { ref: 'x' }, { ref: 2, title: 'Ok' }] },
    } as unknown as ChatMessage;
    for (const m of [bad, mixed]) {
      const { unmount } = render(
        <NextIntlClientProvider locale="en-US" messages={messages['en-us']}>
          <TaskBriefMessage msg={m} />
        </NextIntlClientProvider>,
      );
      expect(screen.getByText('Markets are up [1].')).toBeInTheDocument();
      unmount();
    }
  });

  it('links a story citation and leaves other kinds as text', () => {
    const m = {
      ...msg,
      task: {
        ...msg.task,
        brief_citations: [
          { ref: 1, kind: 'stories', id: 's1', title: 'Story one', source_ids: [] },
          { ref: 2, kind: 'weather', id: 'w1', title: 'Rain', source_ids: [] },
        ],
      },
    } as unknown as ChatMessage;
    render(
      <NextIntlClientProvider locale="en-US" messages={messages['en-us']}>
        <TaskBriefMessage msg={m} />
      </NextIntlClientProvider>,
    );
    expect(screen.getByRole('link', { name: 'Story one' })).toHaveAttribute('href', '/news');
    expect(screen.queryByRole('link', { name: 'Rain' })).toBeNull();
    expect(screen.getByText(/Rain/)).toBeInTheDocument();
  });
});
