import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { NextIntlClientProvider } from 'next-intl';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { todosMessages } from '@/core/messages/todos';

const api = vi.hoisted(() => ({ listTodos: vi.fn(), replaceTodos: vi.fn(), patchTodo: vi.fn() }));
vi.mock('./api', async (original) => ({ ...(await original<typeof import('./api')>()), ...api }));
vi.mock('@/core/app-shell/workspace-shell', () => ({ useWorkspaceSession: () => ({ csrfToken: 'csrf' }) }));

import { TodosPanel } from './todos-panel';

const todo = { id: '11111111-1111-4111-8111-111111111111', title: 'Write draft', description: 'First pass', emoji: '📝', status: 'pending' as const, position: 0 };

function renderPanel(isStreaming = false) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const ui = (streaming: boolean) => (
    <QueryClientProvider client={client}>
      <NextIntlClientProvider locale="en-US" messages={{ todos: todosMessages['en-us'] }}>
        <TodosPanel conversationId="c1" isStreaming={streaming} />
      </NextIntlClientProvider>
    </QueryClientProvider>
  );
  const view = render(ui(isStreaming));
  return { rerenderStreaming: (s: boolean) => view.rerender(ui(s)) };
}

describe('TodosPanel', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.listTodos.mockResolvedValue([todo]);
  });

  it('lists todos with emoji, description and progress when expanded', async () => {
    renderPanel();
    await userEvent.click(await screen.findByRole('button', { name: 'Show tasks' }));
    expect(await screen.findByText('Write draft')).toBeTruthy();
    expect(screen.getByText('First pass')).toBeTruthy();
    expect(screen.getByText('0 of 1 done')).toBeTruthy();
  });

  it('completes a todo optimistically and persists through PATCH', async () => {
    api.patchTodo.mockResolvedValue({ ...todo, status: 'completed' });
    renderPanel();
    await userEvent.click(await screen.findByRole('button', { name: 'Show tasks' }));
    await userEvent.click(await screen.findByRole('checkbox', { name: 'Mark "Write draft" as done' }));
    await waitFor(() => expect(api.patchTodo).toHaveBeenCalledWith('c1', todo.id, { status: 'completed' }, 'csrf'));
    expect(await screen.findByText('1 of 1 done')).toBeTruthy();
  });

  it('rolls back and reports a failed change', async () => {
    api.patchTodo.mockRejectedValue(new Error('boom'));
    renderPanel();
    await userEvent.click(await screen.findByRole('button', { name: 'Show tasks' }));
    await userEvent.click(await screen.findByRole('checkbox', { name: 'Mark "Write draft" as done' }));
    expect((await screen.findByRole('alert')).textContent).toContain('undone');
    expect(screen.getByText('0 of 1 done')).toBeTruthy();
  });

  it('adds a todo through a full-list replace and refetches when streaming ends', async () => {
    api.replaceTodos.mockResolvedValue([todo, { ...todo, id: '22222222-2222-4222-8222-222222222222', title: 'Ship', position: 1 }]);
    const { rerenderStreaming } = renderPanel(true);
    await userEvent.click(await screen.findByRole('button', { name: 'Show tasks' }));
    await userEvent.type(await screen.findByLabelText('New task'), 'Ship');
    await userEvent.click(screen.getByRole('button', { name: 'Add' }));
    await waitFor(() => expect(api.replaceTodos).toHaveBeenCalled());
    expect(api.replaceTodos.mock.calls[0][1].map((x: { title: string }) => x.title)).toEqual(['Write draft', 'Ship']);
    const before = api.listTodos.mock.calls.length;
    rerenderStreaming(false);
    await waitFor(() => expect(api.listTodos.mock.calls.length).toBeGreaterThan(before));
  });
});
