'use client';

import * as React from 'react';
import { NextIntlClientProvider } from 'next-intl';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, act } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { messages } from '@/core/messages';

const h = vi.hoisted(() => ({ listChatSourceOptions: vi.fn() }));

vi.mock('./api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('./api')>()),
  listChatSourceOptions: h.listChatSourceOptions,
}));

import { ChatComposer } from './chat-composer';
import { SourceScopeChips, useSourceMention } from './chat-source-picker';

const SOURCES = [
  { id: 'src-1', name: 'Bank feed' },
  { id: 'src-2', name: 'Calendar' },
  { id: 'src-3', name: 'Bank savings' },
];

/** Harness: owns the draft like the session does and wires the picker into the full composer. */
function Harness({
  initial = '',
  enabled = true,
  onSend = vi.fn(),
  onCancel,
  onValue,
}: {
  initial?: string;
  enabled?: boolean;
  onSend?: (content: string) => void;
  onCancel?: () => void;
  onValue?: (value: string) => void;
}) {
  const [value, setValueState] = React.useState(initial);
  const textareaRef = React.useRef<HTMLTextAreaElement>(null);
  const setValue = React.useCallback(
    (next: string) => {
      setValueState(next);
      onValue?.(next);
    },
    [onValue],
  );
  const sourceMention = useSourceMention({ value, setValue, textareaRef, enabled });
  return (
    <ChatComposer
      mode="full"
      value={value}
      onChange={setValue}
      onSend={onSend}
      isStreaming={false}
      textareaRef={textareaRef}
      sourceMention={sourceMention}
      editing={onCancel ? { onCancel } : undefined}
    />
  );
}

/** Harness wrapped in the intl and query providers the picker needs. */
function harnessTree(props: Parameters<typeof Harness>[0], queryClient: QueryClient) {
  return (
    <NextIntlClientProvider locale="en-US" messages={messages['en-us']}>
      <QueryClientProvider client={queryClient}>
        <Harness {...props} />
      </QueryClientProvider>
    </NextIntlClientProvider>
  );
}

/** Renders the harness; `rerender` takes new props (for example a flipped `enabled`). */
function renderHarness(props: Parameters<typeof Harness>[0] = {}) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const result = render(harnessTree(props, queryClient));
  return { ...result, rerender: (next: Parameters<typeof Harness>[0]) => result.rerender(harnessTree(next, queryClient)) };
}

function textarea() {
  return screen.getByRole('combobox');
}

beforeEach(() => {
  h.listChatSourceOptions.mockReset();
  h.listChatSourceOptions.mockResolvedValue(SOURCES);
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe('@ source picker trigger', () => {
  it('opens the list when @ starts a word and filters by the typed name', async () => {
    renderHarness();
    await userEvent.click(textarea());
    await userEvent.keyboard('@bank');
    expect(textarea().getAttribute('aria-expanded')).toBe('true');
    const options = await screen.findAllByRole('option');
    expect(options.map((o) => o.textContent)).toEqual(['Bank feed', 'Bank savings']);
  });

  it('does not open for an @ inside an email-like word', async () => {
    renderHarness();
    await userEvent.click(textarea());
    await userEvent.keyboard('foo@bar');
    expect(textarea().getAttribute('aria-expanded')).toBe('false');
    expect(screen.queryByRole('listbox')).toBeNull();
    expect(h.listChatSourceOptions).not.toHaveBeenCalled();
  });

  it('stays closed for selection context (picker disabled)', async () => {
    renderHarness({ enabled: false });
    await userEvent.click(textarea());
    await userEvent.keyboard('@');
    expect(textarea().getAttribute('aria-expanded')).toBe('false');
    expect(screen.queryByRole('listbox')).toBeNull();
  });
});

describe('@ source picker keyboard', () => {
  it('ArrowDown then Enter adds a chip, strips the @ token and does not send', async () => {
    const onSend = vi.fn();
    const onValue = vi.fn();
    renderHarness({ onSend, onValue });
    await userEvent.click(textarea());
    await userEvent.keyboard('hi @cal');
    await screen.findByRole('option', { name: 'Calendar' });
    await userEvent.keyboard('{ArrowDown}');
    await userEvent.keyboard('{Enter}');
    expect(onSend).not.toHaveBeenCalled();
    expect(onValue).toHaveBeenLastCalledWith('hi ');
    expect(screen.getByText('Calendar')).toBeTruthy();
    expect(textarea().getAttribute('aria-expanded')).toBe('false');
  });

  it('Escape closes the list without cancelling the edit', async () => {
    const onCancel = vi.fn();
    renderHarness({ onCancel });
    await userEvent.click(textarea());
    await userEvent.keyboard('@');
    await screen.findByRole('listbox');
    await userEvent.keyboard('{Escape}');
    expect(textarea().getAttribute('aria-expanded')).toBe('false');
    expect(onCancel).not.toHaveBeenCalled();
  });

  it('Enter with no matching source closes without sending', async () => {
    const onSend = vi.fn();
    renderHarness({ onSend });
    await userEvent.click(textarea());
    await userEvent.keyboard('@zzz');
    await screen.findByText(/No source matches/);
    await userEvent.keyboard('{Enter}');
    expect(onSend).not.toHaveBeenCalled();
    expect(textarea().getAttribute('aria-expanded')).toBe('false');
  });
});

describe('@ source picker list states', () => {
  it('shows the loading state while sources are fetched', async () => {
    h.listChatSourceOptions.mockReturnValue(new Promise(() => undefined));
    renderHarness();
    await userEvent.click(textarea());
    await userEvent.keyboard('@');
    expect(await screen.findByText('Loading sources…')).toBeTruthy();
  });

  it('shows the error state when sources cannot be loaded', async () => {
    h.listChatSourceOptions.mockRejectedValue(new Error('boom'));
    renderHarness();
    await userEvent.click(textarea());
    await userEvent.keyboard('@');
    expect(await screen.findByText("Couldn't load sources")).toBeTruthy();
  });

  it('shows the empty state when no source has data yet', async () => {
    h.listChatSourceOptions.mockResolvedValue([]);
    renderHarness();
    await userEvent.click(textarea());
    await userEvent.keyboard('@');
    expect(await screen.findByText('No source has collected data yet')).toBeTruthy();
  });
});

describe('SourceScopeChips', () => {
  it('removes a chip when its remove button is pressed', async () => {
    const onRemove = vi.fn();
    render(
      <NextIntlClientProvider locale="en-US" messages={messages['en-us']}>
        <SourceScopeChips chips={[{ id: 'src-2', name: 'Calendar' }]} onRemove={onRemove} />
      </NextIntlClientProvider>,
    );
    expect(screen.getByText('Only from')).toBeTruthy();
    await userEvent.click(screen.getByRole('button', { name: 'Remove Calendar' }));
    expect(onRemove).toHaveBeenCalledWith('src-2');
  });
});

describe('chip lifecycle in the composer', () => {
  it('shows picked chips above the textarea and removes them via the chip button', async () => {
    renderHarness();
    await userEvent.click(textarea());
    await userEvent.keyboard('@cal');
    await screen.findByRole('option', { name: 'Calendar' });
    await userEvent.keyboard('{Enter}');
    await userEvent.click(screen.getByRole('button', { name: 'Remove Calendar' }));
    expect(screen.queryByRole('button', { name: 'Remove Calendar' })).toBeNull();
  });

  it('selecting with the mouse adds a chip', async () => {
    renderHarness();
    await userEvent.click(textarea());
    await userEvent.keyboard('@');
    const option = await screen.findByRole('option', { name: 'Calendar' });
    await act(async () => {
      option.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true }));
    });
    expect(screen.getByRole('button', { name: 'Remove Calendar' })).toBeTruthy();
  });
});

describe('source scope lifecycle', () => {
  it('clears chosen chips and closes the list when the picker becomes disabled', async () => {
    const view = renderHarness();
    await userEvent.click(textarea());
    await userEvent.keyboard('@cal');
    await screen.findByRole('option', { name: 'Calendar' });
    await userEvent.keyboard('{Enter}');
    expect(screen.getByRole('button', { name: 'Remove Calendar' })).toBeTruthy();

    // A profile switch, selection/gadget context or an attachment all arrive as enabled=false.
    view.rerender({ enabled: false });
    expect(screen.queryByRole('button', { name: 'Remove Calendar' })).toBeNull();
    expect(screen.queryByRole('listbox')).toBeNull();

    view.rerender({ enabled: true });
    expect(screen.queryByRole('button', { name: 'Remove Calendar' })).toBeNull();
  });

  it('hides chips and does not open on @ while editing a previous message', async () => {
    const onCancel = vi.fn();
    const view = renderHarness({ onCancel });
    await userEvent.click(textarea());
    await userEvent.keyboard('@cal');
    await screen.findByRole('option', { name: 'Calendar' });
    await userEvent.keyboard('{Enter}');
    expect(screen.getByRole('button', { name: 'Remove Calendar' })).toBeTruthy();

    // Entering edit mode disables the picker for that send.
    view.rerender({ enabled: false, onCancel });
    expect(screen.queryByRole('button', { name: 'Remove Calendar' })).toBeNull();

    await userEvent.keyboard('@');
    expect(textarea().getAttribute('aria-expanded')).toBe('false');
    expect(screen.queryByRole('listbox')).toBeNull();
  });
});
