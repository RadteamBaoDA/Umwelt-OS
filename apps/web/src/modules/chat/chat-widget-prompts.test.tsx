import { act, render, screen } from '@testing-library/react';
import { NextIntlClientProvider } from 'next-intl';
import * as React from 'react';
import { describe, expect, it, vi } from 'vitest';
import { messages } from '@/core/messages';
import { useWidgetPromptHandlers } from './use-chat-session';

/** One session root with its own composer textarea; exposes the hook's `onDraftPrompt`. */
function Session({ label, onReady }: { label: string; onReady?: (draft: (prompt: string) => void) => void }) {
  const rootRef = React.useRef<HTMLDivElement>(null);
  const widget = useWidgetPromptHandlers({
    chatCtrl: { draft: '', setDraft: vi.fn() } as never,
    busy: false,
    handleSend: vi.fn(),
    setError: vi.fn(),
    rootRef,
  });
  React.useEffect(() => onReady?.(widget.onDraftPrompt), [onReady, widget.onDraftPrompt]);
  return (
    <div ref={rootRef}>
      <textarea aria-label={label} data-chat-composer-input />
    </div>
  );
}

describe('useWidgetPromptHandlers onDraftPrompt', () => {
  it('focuses the composer of its own session, not the first one in the document (R9)', () => {
    vi.useFakeTimers();
    let draft: (prompt: string) => void = () => undefined;
    render(
      <NextIntlClientProvider locale="en-US" messages={messages['en-us']}>
        <Session label="drawer" />
        <Session label="page" onReady={(fn) => (draft = fn)} />
      </NextIntlClientProvider>,
    );
    act(() => {
      draft('hello');
      vi.runAllTimers();
    });
    vi.useRealTimers();
    expect(document.activeElement).toBe(screen.getByLabelText('page'));
  });
});
