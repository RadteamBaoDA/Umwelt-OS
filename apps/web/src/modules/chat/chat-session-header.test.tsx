import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { NextIntlClientProvider } from 'next-intl';
import { describe, expect, it, vi } from 'vitest';
import { messages } from '@/core/messages';

vi.mock('@/modules/pick', () => ({ PickQrButton: () => null }));
vi.mock('@/core/query-provider', () => ({ useDisplayPreferences: () => ({ locale: 'en-us', timezone: 'UTC' }) }));
const automations = vi.hoisted(() => ({ getAutomation: vi.fn(), patchAutomation: vi.fn() }));
vi.mock('@/modules/automations/api', async (original) => ({ ...(await original<object>()), ...automations }));

import { ChatSessionHeader, type ChatSessionHeaderProps } from './chat-session-header';

/** Renders the full-mode header with English messages. */
function renderHeader(props: Partial<ChatSessionHeaderProps>) {
  return render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <NextIntlClientProvider locale="en-US" messages={messages['en-us']}>
        <ChatSessionHeader mode="full" title="T" csrfToken="x" onSendPrompt={vi.fn()} onNewChat={vi.fn()} {...props} />
      </NextIntlClientProvider>
    </QueryClientProvider>,
  );
}

const t = messages['en-us'].chat;

describe('ChatSessionHeader pin and archive', () => {
  it('offers Pin and Archive for an unpinned, active conversation and calls the setters with the inverse state', async () => {
    const onSetPinned = vi.fn();
    const onSetArchived = vi.fn();
    renderHeader({ pinned: false, archived: false, onSetPinned, onSetArchived });
    await userEvent.click(screen.getByRole('button', { name: t.moreActions }));
    await userEvent.click(await screen.findByRole('menuitem', { name: t.pinConversation }));
    expect(onSetPinned).toHaveBeenCalledWith(true);
    await userEvent.click(screen.getByRole('button', { name: t.moreActions }));
    await userEvent.click(await screen.findByRole('menuitem', { name: t.archiveConversation }));
    expect(onSetArchived).toHaveBeenCalledWith(true);
  });

  it('offers Unpin and Restore for a pinned, archived conversation', async () => {
    const onSetPinned = vi.fn();
    const onSetArchived = vi.fn();
    renderHeader({ pinned: true, archived: true, onSetPinned, onSetArchived });
    await userEvent.click(screen.getByRole('button', { name: t.moreActions }));
    await userEvent.click(await screen.findByRole('menuitem', { name: t.unpinConversation }));
    expect(onSetPinned).toHaveBeenCalledWith(false);
    await userEvent.click(screen.getByRole('button', { name: t.moreActions }));
    await userEvent.click(await screen.findByRole('menuitem', { name: t.unarchiveConversation }));
    expect(onSetArchived).toHaveBeenCalledWith(false);
  });

  it('shows no pin/archive items without handlers (new, unsaved chat)', async () => {
    renderHeader({ onRenameConversation: vi.fn() });
    expect(screen.queryByRole('button', { name: t.rename.conversation })).toBeNull(); // C8: actions live only in the More menu
    await userEvent.click(screen.getByRole('button', { name: t.moreActions }));
    expect(screen.queryByRole('menuitem', { name: t.pinConversation })).toBeNull();
    expect(screen.queryByRole('menuitem', { name: t.archiveConversation })).toBeNull();
    expect(screen.getByRole('menuitem', { name: t.rename.conversation })).toBeTruthy();
  });
});

describe('ChatSessionHeader action failure and menu layout', () => {
  it('shows the pin/archive failure in the header (rail may be collapsed) (C14)', () => {
    renderHeader({ actionError: 'Could not update this conversation' });
    expect(screen.getByRole('alert').textContent).toBe('Could not update this conversation');
  });

  it('separates Delete from the other menu items (D6)', async () => {
    renderHeader({ onRenameConversation: vi.fn(), onDeleteConversation: vi.fn() });
    await userEvent.click(screen.getByRole('button', { name: t.moreActions }));
    expect(await screen.findByRole('separator')).toBeTruthy();
  });
});

describe('ChatSessionHeader scheduled task', () => {
  it('shows the task badge and an edit-task link only for task threads', () => {
    const { unmount } = renderHeader({ taskEditHref: '/settings/scheduled-tasks?edit=a1' });
    expect(screen.getByText(t.taskThread.badge)).toBeInTheDocument();
    expect(screen.getByRole('link', { name: t.taskThread.edit })).toHaveAttribute('href', '/settings/scheduled-tasks?edit=a1');
    unmount();
    renderHeader({});
    expect(screen.queryByRole('link', { name: t.taskThread.edit })).toBeNull();
  });
});

describe('ChatSessionHeader scheduled-task thread', () => {
  const rule = { id: 'a1', enabled: true, revision: 3, trigger: { type: 'schedule', cron: '0 7 * * *' } };
  const task = { taskEditHref: '/settings/scheduled-tasks?edit=a1', taskAutomationId: 'a1' };

  it('shows the schedule sentence and pauses with the loaded revision', async () => {
    automations.getAutomation.mockResolvedValue(rule);
    automations.patchAutomation.mockResolvedValue({ ...rule, enabled: false, revision: 4 });
    renderHeader(task);
    expect(await screen.findByText('Every day at 07:00')).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Pause' }));
    expect(automations.patchAutomation).toHaveBeenCalledWith('a1', 3, { enabled: false }, 'x');
    expect(await screen.findByRole('button', { name: 'Resume' })).toBeInTheDocument();
  });

  it('refetches the automation after a failed pause (409) so the revision is current', async () => {
    automations.getAutomation.mockReset();
    automations.getAutomation.mockResolvedValue(rule);
    automations.patchAutomation.mockRejectedValue(new Error('409'));
    renderHeader(task);
    await userEvent.click(await screen.findByRole('button', { name: 'Pause' }));
    await vi.waitFor(() => expect(automations.getAutomation).toHaveBeenCalledTimes(2));
  });

  it('falls back to the static label when the automation cannot be read', async () => {
    automations.getAutomation.mockRejectedValue(new Error('404'));
    renderHeader(task);
    expect(await screen.findByText(t.taskThread.badge)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Pause' })).toBeNull();
  });
});
