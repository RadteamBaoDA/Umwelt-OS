import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen } from '@testing-library/react';
import { NextIntlClientProvider } from 'next-intl';
import { describe, expect, it, vi } from 'vitest';
import { messages } from '@/core/messages';

const ctrl = vi.hoisted(() => ({ activeConversationId: 'c1' as string | null, closeDrawer: vi.fn(), isDrawerOpen: true, context: null }));
vi.mock('@/core/app-shell/chat-controller', () => ({ useChatController: () => ({ ...ctrl, setActiveConversationId: vi.fn() }) }));
vi.mock('./chat-session', () => ({ ChatSession: () => null }));

import { chatKeys } from './api';
import { ChatDrawer } from './chat-drawer';

/** Renders the open drawer with an optional cached conversation title. */
function renderDrawer(title?: string) {
  const client = new QueryClient();
  if (title) client.setQueryData(chatKeys.conversation('c1'), { id: 'c1', title, messages: [] });
  return render(
    <QueryClientProvider client={client}>
      <NextIntlClientProvider locale="en-US" messages={messages['en-us']}>
        <ChatDrawer />
      </NextIntlClientProvider>
    </QueryClientProvider>,
  );
}

describe('ChatDrawer header (C13)', () => {
  it('names the open conversation and links to its full page', () => {
    ctrl.activeConversationId = 'c1';
    renderDrawer('Release plan');
    expect(screen.getByRole('heading', { name: 'Release plan' })).toBeTruthy();
    expect(screen.getByRole('link', { name: /Open in Chat/ }).getAttribute('href')).toBe('/chat/c1');
  });

  it('falls back to "Quick chat" and offers no link before a conversation exists', () => {
    ctrl.activeConversationId = null;
    renderDrawer();
    expect(screen.getByRole('heading', { name: 'Quick chat' })).toBeTruthy();
    expect(screen.queryByRole('link', { name: /Open in Chat/ })).toBeNull();
  });
});
