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

describe('ChatComposer auto-focus', () => {
  /** Renders the composer with an auto-focus key and a pointer-type stub. */
  function renderWith(key: string | undefined, fine: boolean) {
    vi.stubGlobal('matchMedia', (q: string) => ({
      matches: fine && q.includes('fine'),
      media: q,
      addEventListener() {},
      removeEventListener() {},
    }));
    return render(
      <NextIntlClientProvider locale="en-US" messages={messages['en-us']}>
        <ChatComposer mode="full" value="" onChange={vi.fn()} onSend={vi.fn()} isStreaming={false} autoFocusKey={key} />
      </NextIntlClientProvider>,
    );
  }

  it('focuses the textarea on fine pointers and skips touch devices', () => {
    const fineView = renderWith('c1', true);
    expect(document.activeElement).toBe(screen.getByRole('textbox'));
    fineView.unmount();
    (document.activeElement as HTMLElement | null)?.blur();
    renderWith('c1', false);
    expect(document.activeElement).not.toBe(screen.getByRole('textbox'));
  });
});

describe('ChatComposer edit mode', () => {
  it('shows the banner inside the box and cancels from its button and from Escape', async () => {
    const onCancel = vi.fn();
    render(
      <NextIntlClientProvider locale="en-US" messages={messages['en-us']}>
        <ChatComposer mode="full" value="draft" onChange={vi.fn()} onSend={vi.fn()} isStreaming={false} editing={{ onCancel }} />
      </NextIntlClientProvider>,
    );
    expect(screen.getByTestId('chat-edit-banner').textContent).toContain(messages['en-us'].chat.editingPrompt);
    await userEvent.click(screen.getByRole('button', { name: messages['en-us'].chat.cancelEdit }));
    expect(onCancel).toHaveBeenCalledTimes(1);
    await userEvent.type(screen.getByRole('textbox'), '{Escape}');
    expect(onCancel).toHaveBeenCalledTimes(2);
  });
});
