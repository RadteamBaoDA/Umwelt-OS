import { createEvent, fireEvent, render, screen } from '@testing-library/react';
import { NextIntlClientProvider } from 'next-intl';
import { describe, expect, it, vi } from 'vitest';
import { messages } from '@/core/messages';
import { ChatDropZone } from './chat-drop-zone';

/** Renders the zone with a probe child. */
function renderZone(props: { enabled?: boolean; accepting?: boolean; onFiles?: (files: File[]) => void }) {
  return render(
    <NextIntlClientProvider locale="en-US" messages={messages['en-us']}>
      <ChatDropZone enabled={props.enabled ?? true} accepting={props.accepting ?? true} onFiles={props.onFiles ?? vi.fn()}>
        <p>chat</p>
      </ChatDropZone>
    </NextIntlClientProvider>,
  );
}

const file = new File(['x'], 'note.txt', { type: 'text/plain' });
const transfer = { types: ['Files'], files: [file], dropEffect: 'none' };

describe('ChatDropZone', () => {
  it('shows the overlay during a file drag and forwards the dropped files', () => {
    const onFiles = vi.fn();
    renderZone({ onFiles });
    const zone = screen.getByText('chat').parentElement as HTMLElement;
    fireEvent.dragEnter(zone, { dataTransfer: transfer });
    expect(screen.getByTestId('chat-drop-overlay').textContent).toContain('Drop files to attach');
    fireEvent.drop(zone, { dataTransfer: transfer });
    expect(onFiles).toHaveBeenCalledWith([file]);
    expect(screen.queryByTestId('chat-drop-overlay')).toBeNull();
  });

  it('ignores non-file drags and stays inert when disabled', () => {
    const onFiles = vi.fn();
    const { rerender } = renderZone({ onFiles });
    const zone = screen.getByText('chat').parentElement as HTMLElement;
    fireEvent.dragEnter(zone, { dataTransfer: { types: ['text/plain'], files: [] } });
    expect(screen.queryByTestId('chat-drop-overlay')).toBeNull();
    rerender(
      <NextIntlClientProvider locale="en-US" messages={messages['en-us']}>
        <ChatDropZone enabled={false} accepting onFiles={onFiles}>
          <p>chat</p>
        </ChatDropZone>
      </NextIntlClientProvider>,
    );
    const drop = createEvent.drop(zone, { dataTransfer: transfer });
    fireEvent(zone, drop);
    expect(drop.defaultPrevented).toBe(false);
    expect(onFiles).not.toHaveBeenCalled();
  });

  it('swallows the drop without attaching while attachments are blocked', () => {
    const onFiles = vi.fn();
    renderZone({ accepting: false, onFiles });
    const zone = screen.getByText('chat').parentElement as HTMLElement;
    const drop = createEvent.drop(zone, { dataTransfer: transfer });
    fireEvent(zone, drop);
    expect(drop.defaultPrevented).toBe(true);
    expect(onFiles).not.toHaveBeenCalled();
  });
});
