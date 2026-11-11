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
  it('links an unconfigured provider to the web search settings page', async () => {
    await openMenu({ available: false, enabled: true, onChange: vi.fn() });
    const link = screen.getByTestId('chat-web-search-setup');
    expect(link.getAttribute('href')).toBe('/settings/web-search');
    expect(link.textContent).toContain('Not set up — Settings › AI');
    expect(screen.queryByRole('menuitemcheckbox', { name: /Search the web/ })).toBeNull();
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

describe('ChatComposer streaming hints and model label (C20, U2)', () => {
  const picker = { options: [{ providerId: 'p', model: 'm', label: 'P · m' }], value: null, defaultModel: 'm', onChange: vi.fn() };
  /** Renders the full composer. */
  const renderFull = (props: Partial<ChatComposerProps>) =>
    render(
      <NextIntlClientProvider locale="en-US" messages={messages['en-us']}>
        <ChatComposer mode="full" value="" onChange={vi.fn()} onSend={vi.fn()} isStreaming={false} {...props} />
      </NextIntlClientProvider>,
    );

  it('switches the placeholder and puts the shortcut in the Stop title while streaming', () => {
    renderFull({ isStreaming: true });
    expect(screen.getByPlaceholderText('Responding — Esc to stop')).toBeTruthy();
    expect(screen.getByTestId('chat-stop').getAttribute('title')).toBe('Stop generating (Esc)');
  });

  it('drops the "Model:" line when a picker names the model, and names the default model in the picker', () => {
    renderFull({ modelLabel: 'claude-x', modelPicker: picker });
    expect(screen.queryByText(/Model: claude-x/)).toBeNull();
    expect(screen.getByRole('combobox', { name: 'Model for this chat' }).textContent).toBe('Default · m');
  });

  it('keeps the model line without a picker', () => {
    renderFull({ modelLabel: 'claude-x' });
    expect(screen.getByText(/Model: claude-x/)).toBeTruthy();
  });
});
