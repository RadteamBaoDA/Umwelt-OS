import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { NextIntlClientProvider } from 'next-intl';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { messages } from '@/core/messages';

vi.mock('@/core/query-provider', () => ({
  useDisplayPreferences: () => ({ confirmedPreferences: { timezone: 'UTC', locale: 'en-us' } }),
}));
const listConversations = vi.fn();
vi.mock('./api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('./api')>()),
  listConversations: (...args: unknown[]) => listConversations(...args),
  getConversation: vi.fn(),
}));

import { ChatHistory, groupConversationsByDay, HISTORY_PAGE_SIZE } from './chat-history';
import type { Conversation } from './api';

/** Minimal conversation fixture; only the fields the list reads matter. */
const conv = (id: string, over: Partial<Conversation> = {}): Conversation =>
  ({
    id,
    title: `Title ${id}`,
    context_kind: null,
    pinned: false,
    archived: false,
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
    metadata: {},
    ...over,
  }) as unknown as Conversation;

const daysAgo = (n: number, now: Date) => new Date(now.getTime() - n * 86_400_000).toISOString();

const actions = { rename: vi.fn(), remove: vi.fn(), setPinned: vi.fn(), setArchived: vi.fn() };

const renderHistory = () =>
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <NextIntlClientProvider locale="en-US" messages={messages['en-us']}>
        <ChatHistory activeConversationId={null} onSelectConversation={vi.fn()} onNewConversation={vi.fn()} actions={actions} />
      </NextIntlClientProvider>
    </QueryClientProvider>,
  );

beforeEach(() => {
  vi.clearAllMocks();
  listConversations.mockResolvedValue([]);
});

describe('groupConversationsByDay', () => {
  it('buckets pinned, today, yesterday, 7 days, 30 days and older in preference timezone', () => {
    const now = new Date('2026-06-15T12:00:00Z');
    const list = [
      conv('pin', { pinned: true, updated_at: daysAgo(40, now) }),
      conv('t', { updated_at: daysAgo(0, now) }),
      conv('y', { updated_at: daysAgo(1, now) }),
      conv('w', { updated_at: daysAgo(5, now) }),
      conv('m', { updated_at: daysAgo(20, now) }),
      conv('o', { updated_at: daysAgo(90, now) }),
    ];
    const groups = groupConversationsByDay(list, 'UTC', now);
    expect(groups.map((g) => [g.key, g.items[0].id])).toEqual([
      ['pinned', 'pin'],
      ['today', 't'],
      ['yesterday', 'y'],
      ['last7', 'w'],
      ['last30', 'm'],
      ['older', 'o'],
    ]);
  });

  it('uses the preference timezone for the day boundary', () => {
    const now = new Date('2026-06-15T20:00:00Z'); // 03:00 on the 16th in Asia/Ho_Chi_Minh
    const list = [conv('a', { updated_at: '2026-06-15T10:00:00Z' })]; // 17:00 on the 15th there
    expect(groupConversationsByDay(list, 'Asia/Ho_Chi_Minh', now)[0].key).toBe('yesterday');
    expect(groupConversationsByDay(list, 'UTC', now)[0].key).toBe('today');
  });
});

describe('ChatHistory', () => {
  it('renders a Pinned group and wires the row menu to pin and archive', async () => {
    listConversations.mockResolvedValue([conv('p1', { pinned: true }), conv('n1')]);
    renderHistory();
    expect(await screen.findByRole('region', { name: 'Pinned' })).toBeTruthy();
    const [pinnedMenu] = screen.getAllByLabelText('Conversation actions');
    await userEvent.click(pinnedMenu);
    await userEvent.click(await screen.findByRole('menuitem', { name: 'Unpin' }));
    expect(actions.setPinned).toHaveBeenCalledWith('p1', false);
    await userEvent.click(screen.getAllByLabelText('Conversation actions')[1]);
    await userEvent.click(await screen.findByRole('menuitem', { name: 'Archive' }));
    expect(actions.setArchived).toHaveBeenCalledWith('n1', true);
  });

  it('queries archived conversations when the archive filter is toggled', async () => {
    renderHistory();
    await waitFor(() => expect(listConversations).toHaveBeenCalled());
    await userEvent.click(screen.getByRole('button', { name: 'Show archived conversations' }));
    await waitFor(() => expect(listConversations).toHaveBeenLastCalledWith(expect.objectContaining({ archived: true })));
  });

  it('debounces typing into one server search with q', async () => {
    renderHistory();
    await waitFor(() => expect(listConversations).toHaveBeenCalledTimes(1));
    await userEvent.type(screen.getByRole('searchbox'), 'budget');
    await waitFor(() => expect(listConversations).toHaveBeenLastCalledWith(expect.objectContaining({ q: 'budget', offset: 0 })));
    // Six keystrokes must not produce six requests.
    expect(listConversations.mock.calls.length).toBeLessThanOrEqual(3);
  });

  it('loads the next page with offset when a full page was returned', async () => {
    listConversations
      .mockResolvedValueOnce(Array.from({ length: HISTORY_PAGE_SIZE }, (_, i) => conv(`c${i}`)))
      .mockResolvedValueOnce([conv('last')]);
    renderHistory();
    await userEvent.click(await screen.findByRole('button', { name: 'Load more' }));
    await waitFor(() =>
      expect(listConversations).toHaveBeenLastCalledWith(expect.objectContaining({ offset: HISTORY_PAGE_SIZE, limit: HISTORY_PAGE_SIZE })),
    );
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Load more' })).toBeNull());
  });

  it('explains an empty search and clears only the search', async () => {
    renderHistory();
    await userEvent.type(screen.getByRole('searchbox'), 'zzz');
    expect(await screen.findByText('No conversations match your search')).toBeTruthy();
    await userEvent.click(screen.getByRole('button', { name: 'Clear search' }));
    expect((screen.getByRole('searchbox') as HTMLInputElement).value).toBe('');
  });

  it('labels the archived view, explains an empty archive and goes back to the list', async () => {
    renderHistory();
    await userEvent.click(screen.getByRole('button', { name: 'Show archived conversations' }));
    expect(await screen.findByText('No archived conversations')).toBeTruthy();
    expect(screen.getByTestId('history-archived-label').textContent).toBe('Archived');
    await userEvent.click(screen.getByRole('button', { name: 'Back to list' }));
    expect(screen.queryByTestId('history-archived-label')).toBeNull();
  });

  it('shows the time only under Today and an inline error when pin or archive failed', async () => {
    listConversations.mockResolvedValue([conv('n1', { updated_at: new Date().toISOString() })]);
    render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <NextIntlClientProvider locale="en-US" messages={messages['en-us']}>
          <ChatHistory
            activeConversationId={null}
            onSelectConversation={vi.fn()}
            onNewConversation={vi.fn()}
            actions={{ ...actions, flagError: 'n1' }}
          />
        </NextIntlClientProvider>
      </QueryClientProvider>,
    );
    expect(await screen.findByRole('alert')).toBeTruthy();
    const row = screen.getByText('Title n1').closest('button')!;
    expect(row.textContent).not.toMatch(/20\d\d/);
  });

  it('clears a stale pin/archive error when the archived view is toggled (D7)', async () => {
    const clearFlagError = vi.fn();
    render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <NextIntlClientProvider locale="en-US" messages={messages['en-us']}>
          <ChatHistory
            activeConversationId={null}
            onSelectConversation={vi.fn()}
            onNewConversation={vi.fn()}
            actions={{ ...actions, flagError: 'n1', clearFlagError }}
          />
        </NextIntlClientProvider>
      </QueryClientProvider>,
    );
    await userEvent.click(screen.getByRole('button', { name: 'Show archived conversations' }));
    expect(clearFlagError).toHaveBeenCalledTimes(1);
  });

  it('puts the localized shortcut in the New chat title (D8)', () => {
    renderHistory();
    expect(screen.getByRole('button', { name: /New chat/ }).getAttribute('title')).toBe(messages['en-us'].chat.newChatShortcut);
  });
});
