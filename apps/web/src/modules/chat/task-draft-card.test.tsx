import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { NextIntlClientProvider } from 'next-intl';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { messages } from '@/core/messages';
import * as api from '@/modules/automations/api';
import { TaskDraftCard } from './task-draft-card';

let locale: 'en-us' | 'vi-vi' = 'en-us';
vi.mock('@/core/app-shell/workspace-session', () => ({ useWorkspaceSession: () => ({ csrfToken: 'csrf' }) }));
vi.mock('@/core/query-provider', () => ({ useDisplayPreferences: () => ({ locale, timezone: 'UTC' }) }));
vi.mock('@/modules/automations/api', async (original) => ({
  ...(await original<typeof import('@/modules/automations/api')>()),
  listAutomations: vi.fn(),
  getBriefTimezone: vi.fn(async () => 'UTC'),
  getBriefRoleModels: vi.fn(async () => ({ brief: 'm', chat: 'm', background: 'm' })),
  createScheduledTask: vi.fn(async () => ({})),
}));

const draft = {
  kind: 'daily_brief' as const,
  name: 'Morning brief',
  time: '07:00',
  days: 'weekdays' as const,
  sections: ['stories', 'weather'] as ('stories' | 'weather')[],
  extra_instruction: 'Reply in Vietnamese',
  model_role: 'brief' as const,
};

/** Renders the card under the providers it needs. */
function view() {
  return (
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <NextIntlClientProvider locale={locale === 'en-us' ? 'en-US' : 'vi-VN'} messages={messages[locale]}>
        <TaskDraftCard draft={draft} />
      </NextIntlClientProvider>
    </QueryClientProvider>
  );
}

beforeEach(() => {
  locale = 'en-us';
  vi.clearAllMocks();
  vi.mocked(api.listAutomations).mockResolvedValue({ items: [] });
});

describe('TaskDraftCard', () => {
  it('shows the unsaved summary and creates only after Save in the pre-filled editor', async () => {
    render(view());
    expect(screen.getByText('Not saved yet')).toBeTruthy();
    expect(screen.getByText('Morning brief')).toBeTruthy();
    fireEvent.click(await screen.findByRole('button', { name: 'Review and create' }));
    const time = (await screen.findByDisplayValue('07:00')) as HTMLInputElement;
    expect(time.value).toBe('07:00');
    expect(screen.getByDisplayValue('Reply in Vietnamese')).toBeTruthy();
    expect(api.createScheduledTask).not.toHaveBeenCalled();
    await waitFor(() => expect((screen.getByRole('button', { name: /save/i }) as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(screen.getByRole('button', { name: /save/i }));
    await waitFor(() => expect(api.createScheduledTask).toHaveBeenCalledTimes(1));
    const rule = vi.mocked(api.createScheduledTask).mock.calls[0][1] as unknown as { actions: Record<string, unknown>[] };
    expect(rule.actions[0]).toMatchObject({
      deliver_chat: true,
      sections: ['stories', 'weather'],
      extra_instruction: 'Reply in Vietnamese',
    });
    expect(await screen.findByText('Created')).toBeTruthy();
  });

  it('shows the already-exists state without a create button', async () => {
    vi.mocked(api.listAutomations).mockResolvedValue({
      items: [
        {
          id: 't1',
          trigger: { type: 'schedule', cron: '0 7 * * *', timezone: 'UTC' },
          actions: [{ type: 'generate_brief', deliver_chat: true }],
        } as unknown as api.Automation,
      ],
    });
    render(view());
    expect(await screen.findByText('A Daily brief task already exists')).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Review and create' })).toBeNull();
    expect(screen.getByRole('link', { name: 'Open Scheduled tasks' }).getAttribute('href')).toBe('/settings/scheduled-tasks?edit=t1');
  });

  it('renders in Vietnamese', async () => {
    locale = 'vi-vi';
    render(view());
    expect(await screen.findByRole('button', { name: 'Xem lại và tạo' })).toBeTruthy();
    expect(screen.getByText('Chưa lưu')).toBeTruthy();
  });
});
