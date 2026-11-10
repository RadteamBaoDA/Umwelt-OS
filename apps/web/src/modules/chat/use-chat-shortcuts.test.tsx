import { fireEvent, render, screen } from '@testing-library/react';
import * as React from 'react';
import { describe, expect, it, vi } from 'vitest';
import { useStopOnEscape } from './use-chat-shortcuts';

/** Host with a chat root, an outside input and an optional open dialog. */
function Host({
  streaming,
  onStop,
  dialog = false,
  enabled = true,
}: {
  streaming: boolean;
  onStop: () => void;
  dialog?: boolean;
  enabled?: boolean;
}) {
  const ref = React.useRef<HTMLDivElement>(null);
  useStopOnEscape(ref, { enabled, isStreaming: streaming, onStop });
  return (
    <>
      <div ref={ref}>
        <textarea aria-label="in" />
      </div>
      <input aria-label="out" />
      {dialog && <div role="dialog">d</div>}
    </>
  );
}

describe('useStopOnEscape', () => {
  it('stops generation on Escape from the chat composer', () => {
    const onStop = vi.fn();
    render(<Host streaming onStop={onStop} />);
    screen.getByLabelText('in').focus();
    fireEvent.keyDown(screen.getByLabelText('in'), { key: 'Escape' });
    expect(onStop).toHaveBeenCalledTimes(1);
  });

  it('does nothing when idle, outside the chat, with a dialog open or when disabled', () => {
    const onStop = vi.fn();
    const { rerender } = render(<Host streaming={false} onStop={onStop} />);
    fireEvent.keyDown(window, { key: 'Escape' });
    rerender(<Host streaming onStop={onStop} />);
    screen.getByLabelText('out').focus();
    fireEvent.keyDown(window, { key: 'Escape' });
    screen.getByLabelText('in').focus();
    rerender(<Host streaming onStop={onStop} dialog />);
    fireEvent.keyDown(window, { key: 'Escape' });
    rerender(<Host streaming onStop={onStop} enabled={false} />);
    fireEvent.keyDown(window, { key: 'Escape' });
    expect(onStop).not.toHaveBeenCalled();
  });
});
