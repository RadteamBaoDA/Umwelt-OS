import { fireEvent, render } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const h = vi.hoisted(() => ({ path: '/chat', reset: vi.fn() }));
vi.mock('next/navigation', () => ({ useRouter: () => ({ replace: vi.fn() }), usePathname: () => h.path }));
vi.mock('@/core/app-shell/chat-controller', () => ({ useChatController: () => ({ resetConversation: h.reset }) }));

import { NewChatShortcut } from './workspace-shell';

describe('NewChatShortcut', () => {
  beforeEach(() => {
    h.reset.mockReset();
    h.path = '/chat';
  });

  it('resets the conversation on Ctrl+Shift+O and Cmd+Shift+O on the chat page', () => {
    render(<NewChatShortcut />);
    fireEvent.keyDown(window, { key: 'O', ctrlKey: true, shiftKey: true });
    fireEvent.keyDown(window, { key: 'o', metaKey: true, shiftKey: true });
    expect(h.reset).toHaveBeenCalledTimes(2);
  });

  it('ignores the key without Shift', () => {
    render(<NewChatShortcut />);
    fireEvent.keyDown(window, { key: 'o', ctrlKey: true });
    expect(h.reset).not.toHaveBeenCalled();
  });

  it('ignores the key outside the chat page', () => {
    h.path = '/dashboard';
    render(<NewChatShortcut />);
    fireEvent.keyDown(window, { key: 'O', ctrlKey: true, shiftKey: true });
    expect(h.reset).not.toHaveBeenCalled();
  });
});
