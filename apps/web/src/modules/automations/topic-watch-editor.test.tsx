import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { NextIntlClientProvider } from 'next-intl';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { messages } from '@/core/messages';
import * as api from './api';
import { TopicWatchEditor } from './topic-watch-editor';

vi.mock('./api', async (original) => ({
  ...(await original<typeof import('./api')>()),
  getBriefTimezone: vi.fn(async () => 'Asia/Ho_Chi_Minh'),
  getBriefRoleModels: vi.fn(async () => ({ brief: 'model-x', chat: 'model-c', background: 'model-bg' })),
  createScheduledTask: vi.fn(),
  patchAutomation: vi.fn(),
}));

const watch = { topic: 'EV', keep_rule: null, languages: ['en'], include_sites: [], exclude_sites: [], max_items: 5 };
const existing = {
  id: 'w1',
  name: 'EV watch',
  enabled: true,
  revision: 3,
  trigger: { type: 'schedule', cron: '0 8 * * *', timezone: 'UTC' },
  conditions: [],
  actions: [
    { type: 'collect_source', watch, model_role: 'background', source_id: 's', consent: { hash: 'h', granted_at: '', est_tokens: 15600 } },
  ],
} as unknown as api.Automation;

/** Renders the node in the given locale under the providers the editor needs. */
function view(node: React.ReactNode, locale: 'en-us' | 'vi-vi' = 'en-us') {
  return (
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <NextIntlClientProvider locale={locale === 'en-us' ? 'en-US' : 'vi-VN'} messages={messages[locale]}>
        {node}
      </NextIntlClientProvider>
    </QueryClientProvider>
  );
}

beforeEach(() => vi.clearAllMocks());

describe('topic watch editor', () => {
  it('keeps Save disabled until consent is ticked, then posts the exact payload', async () => {
    vi.mocked(api.createScheduledTask).mockResolvedValue(existing);
    render(view(<TopicWatchEditor csrfToken="csrf" onClose={vi.fn()} />));
    const save = screen.getByRole('button', { name: 'Save' });
    fireEvent.change(screen.getByLabelText('Topic'), { target: { value: 'EV' } });
    expect(save).toBeDisabled();
    expect(screen.getByText(/Estimated up to 15,600 tokens per run, capped at 23,400/)).toBeTruthy();
    fireEvent.click(screen.getByRole('checkbox', { name: 'I allow this task to run unattended' }));
    await waitFor(() => expect(save).toBeEnabled());
    fireEvent.click(save);
    await waitFor(() => expect(api.createScheduledTask).toHaveBeenCalled());
    expect(api.createScheduledTask).toHaveBeenCalledWith(
      'EV',
      {
        trigger: { type: 'schedule', cron: '0 8 * * *', timezone: 'Asia/Ho_Chi_Minh' },
        conditions: [],
        actions: [{ type: 'collect_source', watch, model_role: 'background', grant_consent_est_tokens: 15600 }],
      },
      'csrf',
    );
  });

  it('requires consent again only after a watched field changes', async () => {
    vi.mocked(api.patchAutomation).mockResolvedValue(existing);
    render(view(<TopicWatchEditor rule={existing} csrfToken="csrf" onClose={vi.fn()} />));
    const save = screen.getByRole('button', { name: 'Save' });
    expect(screen.queryByRole('checkbox', { name: 'I allow this task to run unattended' })).toBeNull();
    fireEvent.change(screen.getByLabelText('Max items per run'), { target: { value: '3' } });
    expect(save).toBeDisabled();
    fireEvent.click(screen.getByRole('checkbox', { name: 'I allow this task to run unattended' }));
    await waitFor(() => expect(save).toBeEnabled());
    fireEvent.click(save);
    await waitFor(() => expect(api.patchAutomation).toHaveBeenCalled());
    const [, revision, patch] = vi.mocked(api.patchAutomation).mock.calls[0];
    expect(revision).toBe(3);
    expect(patch.actions?.[0]).toMatchObject({ grant_consent_est_tokens: 9600, watch: { max_items: 3 } });
  });

  it('an unchanged edit sends no consent grant', async () => {
    vi.mocked(api.patchAutomation).mockResolvedValue(existing);
    render(view(<TopicWatchEditor rule={existing} csrfToken="csrf" onClose={vi.fn()} />));
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(api.patchAutomation).toHaveBeenCalled());
    expect(vi.mocked(api.patchAutomation).mock.calls[0][2].actions?.[0]).not.toHaveProperty('grant_consent_est_tokens');
  });

  it('renders Vietnamese copy', async () => {
    render(view(<TopicWatchEditor csrfToken="csrf" onClose={vi.fn()} />, 'vi-vi'));
    expect(await screen.findByText('Tôi cho phép tác vụ này chạy không giám sát')).toBeTruthy();
  });
});
