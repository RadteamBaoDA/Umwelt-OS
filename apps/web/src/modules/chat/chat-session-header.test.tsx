import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { NextIntlClientProvider } from 'next-intl';
import { describe, expect, it, vi } from 'vitest';
import { messages } from '@/core/messages';

vi.mock('@/modules/pick', () => ({ PickQrButton: () => null }));

import { ChatSessionHeader, type ChatSessionHeaderProps } from './chat-session-header';

/** Renders the full-mode header with English messages. */
function renderHeader(props: Partial<ChatSessionHeaderProps>) {
  return render(
    <NextIntlClientProvider locale="en-US" messages={messages['en-us']}>
      <ChatSessionHeader
        mode="full"
        title="T"
        csrfToken="x"
        onSendPrompt={vi.fn()}
        onNewChat={vi.fn()}
        profileValue="assistant"
        profilePickerDisabled={false}
        onProfileChange={vi.fn()}
        profiles={[]}
        {...props}
      />
    </NextIntlClientProvider>,
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

  it('shows no pin/archive items without handlers (new, unsaved chat)', () => {
    renderHeader({ onRenameConversation: vi.fn() });
    expect(screen.getByRole('button', { name: t.renameConversation })).toBeTruthy();
  });
});
