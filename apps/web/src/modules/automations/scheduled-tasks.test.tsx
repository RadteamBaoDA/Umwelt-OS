import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { NextIntlClientProvider } from 'next-intl';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '@/core/api';
import { messages } from '@/core/messages';
import * as api from './api';
import { BriefTaskEditor } from './brief-task-editor';
import { ScheduledTasksWorkspace } from './scheduled-tasks';

let locale: 'en-us' | 'vi-vi' = 'en-us';
vi.mock('@/core/app-shell/workspace-session', () => ({ useWorkspaceSession: () => ({ csrfToken: 'csrf' }) }));
vi.mock('@/core/query-provider', () => ({ useDisplayPreferences: () => ({ locale, timezone: 'UTC' }) }));
vi.mock('next/navigation', () => ({ useRouter: () => ({ push: vi.fn() }) }));
vi.mock('./api', async (original) => ({
  ...(await original<typeof import('./api')>()),
  listAutomations: vi.fn(),
  getLastRun: vi.fn(async () => ({ items: [] })),
  getRuleConversationId: vi.fn(async () => null),
  getBriefTimezone: vi.fn(async () => 'Asia/Ho_Chi_Minh'),
  getBriefModel: vi.fn(async () => 'model-x'),
  createScheduledTask: vi.fn(),
  patchAutomation: vi.fn(),
}));

const task = {
  id: 't1',
  name: 'Daily brief',
  enabled: true,
  revision: 2,
  created_at: '',
  updated_at: '',
  next_run_at: '2026-11-19T00:00:00Z',
  trigger: { type: 'schedule', cron: '0 7 * * *', timezone: 'UTC' },
  conditions: [],
  actions: [{ type: 'generate_brief', scope: 'daily', sections: ['stories'], extra_instruction: null, deliver_chat: true }],
} as api.Automation;
const legacy = { ...task, id: 'l1', actions: [{ type: 'generate_brief', scope: 'daily' }] } as api.Automation;

/** Renders the node under the providers the module needs, in the current locale. */
function view(node: React.ReactNode) {
  return (
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <NextIntlClientProvider locale={locale === 'en-us' ? 'en-US' : 'vi-VN'} messages={messages[locale]}>
        {node}
      </NextIntlClientProvider>
    </QueryClientProvider>
  );
}

beforeEach(() => {
  locale = 'en-us';
  vi.clearAllMocks();
});

describe('scheduled tasks workspace', () => {
  it('shows the template grid in the empty state', async () => {
    vi.mocked(api.listAutomations).mockResolvedValue({ items: [] });
    render(view(<ScheduledTasksWorkspace />));
    expect(await screen.findByText('No scheduled tasks yet')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Set up' })).toBeEnabled();
  });

  it('lists a task with its schedule sentence and next run, and disables the template', async () => {
    vi.mocked(api.listAutomations).mockResolvedValue({ items: [task] });
    render(view(<ScheduledTasksWorkspace />));
    expect(await screen.findByText('Every day at 07:00')).toBeTruthy();
    expect(screen.getByText(/Nov 19, 2026/)).toBeTruthy();
    fireEvent.mouseDown(screen.getByRole('tab', { name: 'Templates' }), { button: 0 });
    expect(await screen.findByRole('button', { name: 'Already set up' })).toBeDisabled();
  });

  it('disables the template and offers conversion when a legacy brief rule exists', async () => {
    vi.mocked(api.listAutomations).mockResolvedValue({ items: [legacy] });
    render(view(<ScheduledTasksWorkspace />));
    expect(await screen.findByRole('button', { name: 'Convert existing brief rule' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Set up' })).toBeDisabled();
  });

  it('renders Vietnamese copy', async () => {
    locale = 'vi-vi';
    vi.mocked(api.listAutomations).mockResolvedValue({ items: [task] });
    render(view(<ScheduledTasksWorkspace />));
    expect(await screen.findByText('Hằng ngày lúc 07:00')).toBeTruthy();
  });
});

describe('brief task editor', () => {
  it('posts the exact task payload', async () => {
    vi.mocked(api.createScheduledTask).mockResolvedValue(task);
    render(view(<BriefTaskEditor csrfToken="csrf" onClose={vi.fn()} />));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Save' })).toBeEnabled());
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(api.createScheduledTask).toHaveBeenCalled());
    expect(api.createScheduledTask).toHaveBeenCalledWith(
      'Daily brief',
      {
        trigger: { type: 'schedule', cron: '0 7 * * *', timezone: 'Asia/Ho_Chi_Minh' },
        conditions: [],
        actions: [
          {
            type: 'generate_brief',
            scope: 'daily',
            sections: ['stories', 'markets', 'weather', 'hazards', 'agenda', 'source_health'],
            extra_instruction: null,
            deliver_chat: true,
          },
        ],
      },
      'csrf',
    );
  });

  it('shows the convert action on a brief_slot_owned conflict and converts the legacy rule', async () => {
    vi.mocked(api.createScheduledTask).mockRejectedValue(new ApiError(409, 'conflict', { code: 'brief_slot_owned' }));
    vi.mocked(api.patchAutomation).mockResolvedValue(legacy);
    render(view(<BriefTaskEditor legacyRule={legacy} csrfToken="csrf" onClose={vi.fn()} />));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Save' })).toBeEnabled());
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Convert existing rule' }));
    await waitFor(() => expect(api.patchAutomation).toHaveBeenCalled());
    const [id, revision, patch] = vi.mocked(api.patchAutomation).mock.calls[0];
    expect([id, revision]).toEqual(['l1', 2]);
    expect(patch.actions?.[0]).toMatchObject({ type: 'generate_brief', deliver_chat: true, extra_instruction: null });
  });

  it('keeps the other fields of the converted action', async () => {
    const rich = { ...legacy, actions: [{ type: 'generate_brief', scope: 'daily', instruction: 'keep me' }] } as api.Automation;
    vi.mocked(api.createScheduledTask).mockRejectedValue(new ApiError(409, 'conflict', { code: 'brief_slot_owned' }));
    vi.mocked(api.patchAutomation).mockResolvedValue(rich);
    render(view(<BriefTaskEditor legacyRule={rich} csrfToken="csrf" onClose={vi.fn()} />));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Save' })).toBeEnabled());
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Convert existing rule' }));
    await waitFor(() => expect(api.patchAutomation).toHaveBeenCalled());
    expect(vi.mocked(api.patchAutomation).mock.calls[0][2].actions?.[0]).toMatchObject({ instruction: 'keep me' });
  });

  it('edits an existing task with its revision and own timezone', async () => {
    vi.mocked(api.patchAutomation).mockResolvedValue(task);
    render(view(<BriefTaskEditor rule={task} csrfToken="csrf" onClose={vi.fn()} />));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Save' })).toBeEnabled());
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(api.patchAutomation).toHaveBeenCalled());
    const [id, revision, patch] = vi.mocked(api.patchAutomation).mock.calls[0];
    expect([id, revision]).toEqual(['t1', 2]);
    expect(patch.trigger).toMatchObject({ cron: '0 7 * * *', timezone: 'UTC' });
  });

  it('renders the editor in Vietnamese', async () => {
    locale = 'vi-vi';
    render(view(<BriefTaskEditor csrfToken="csrf" onClose={vi.fn()} />));
    expect(await screen.findByText('Múi giờ')).toBeTruthy();
  });
});
