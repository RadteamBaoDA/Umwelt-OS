import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { NextIntlClientProvider } from 'next-intl';
import { describe, expect, it, vi } from 'vitest';
import { messages } from '@/core/messages';

vi.mock('@/core/app-shell/workspace-shell', () => ({ useWorkspaceSession: () => ({ csrfToken: 'csrf' }) }));
vi.mock('@/core/app-shell/chat-controller', () => ({ useChatController: () => ({ unbindConversation: () => undefined }) }));

import { useConversationActions } from './conversation-actions';

/** Header-style harness: no ChatHistory is mounted, exactly like the collapsed sidebar. */
function Harness() {
  const actions = useConversationActions('c1', () => undefined);
  return (
    <>
      <button type="button" onClick={() => actions.rename({ id: 'c1', title: 'Old title' })}>header-rename</button>
      <button type="button" onClick={() => actions.remove('c1')}>header-delete</button>
      {actions.dialogs}
    </>
  );
}

const renderHarness = () => render(
  <QueryClientProvider client={new QueryClient()}>
    <NextIntlClientProvider locale="en-US" messages={messages['en-us']}><Harness /></NextIntlClientProvider>
  </QueryClientProvider>,
);

describe('useConversationActions with the history list unmounted', () => {
  it('opens the rename dialog prefilled with the title', async () => {
    renderHarness();
    await userEvent.click(screen.getByText('header-rename'));
    expect((await screen.findByLabelText('Conversation title') as HTMLInputElement).value).toBe('Old title');
  });

  it('opens the delete confirmation', async () => {
    renderHarness();
    await userEvent.click(screen.getByText('header-delete'));
    expect(await screen.findByText(/cannot be undone/)).toBeTruthy();
  });
});
