import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { NextIntlClientProvider } from 'next-intl';
import { describe, expect, it, vi } from 'vitest';
import { messages } from '@/core/messages';
import { ChatComposer, type ChatComposerProps } from './chat-composer';

/** Renders the full composer with the + menu open so the web search item is queryable. */
async function openMenu(webSearch: NonNullable<ChatComposerProps['webSearch']>) {
  render(
    <NextIntlClientProvider locale="en-US" messages={messages['en-us']}>
      <ChatComposer
        mode="full"
        value=""
        onChange={vi.fn()}
        onSend={vi.fn()}
        isStreaming={false}
        webSearch={webSearch}
        menu={{ onAttach: vi.fn(), attachDisabled: false, share: { checked: false, disabled: false, onChange: vi.fn() } }}
      />
    </NextIntlClientProvider>,
  );
  await userEvent.click(screen.getByRole('button', { name: /add|more|menu|\+/i }));
}

describe('ChatComposer web search toggle', () => {
  it('explains an unconfigured provider and stays disabled', async () => {
    await openMenu({ available: false, enabled: true, onChange: vi.fn() });
    expect(screen.getByTestId('chat-web-search-hint').textContent).toBe('Not set up — Settings › AI');
    expect(screen.getByRole('menuitemcheckbox', { name: /Search the web/ }).getAttribute('aria-disabled')).toBe('true');
    expect(screen.queryByTestId('chat-web-search-badge')).toBeNull();
  });

  it('explains the profile restriction', async () => {
    await openMenu({ available: true, profileBlocked: true, enabled: true, onChange: vi.fn() });
    expect(screen.getByTestId('chat-web-search-hint').textContent).toBe('Only for the Assistant profile');
  });

  it('shows no hint and the badge when available and on', async () => {
    await openMenu({ available: true, enabled: true, onChange: vi.fn() });
    expect(screen.queryByTestId('chat-web-search-hint')).toBeNull();
    expect(screen.getByTestId('chat-web-search-badge')).toBeTruthy();
  });
});
