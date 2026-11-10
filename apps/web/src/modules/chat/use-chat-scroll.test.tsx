import { act, render, screen } from '@testing-library/react';
import * as React from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { useChatScroll } from './use-chat-scroll';

vi.stubGlobal('matchMedia', (q: string) => ({ matches: false, media: q, addEventListener() {}, removeEventListener() {} }));

/** Minimal transcript host exposing the hook's containers and flags. */
function Host({ streaming, signal, lastId }: { streaming: boolean; signal: string; lastId: string }) {
  const { containerRef, scrollHandlers, hasUnread } = useChatScroll({
    conversationId: 'c1',
    messageCount: 2,
    lastMessageId: lastId,
    lastMessageRole: 'user',
    isStreaming: streaming,
    contentSignal: signal,
  });
  return (
    <div ref={containerRef} data-testid="box" {...scrollHandlers}>
      <div data-message-id="other" />
      <div data-message-id={lastId} data-testid="target" />
      <span data-testid="unread">{String(hasUnread)}</span>
    </div>
  );
}

describe('useChatScroll', () => {
  beforeEach(() => {
    Element.prototype.scrollIntoView = vi.fn();
    Element.prototype.scrollTo = vi.fn() as unknown as typeof Element.prototype.scrollTo;
  });

  it('scrolls the message that matches the id when a run starts', () => {
    const { rerender } = render(<Host streaming={false} signal="a" lastId="m-2" />);
    rerender(<Host streaming signal="a" lastId="m-2" />);
    const spy = Element.prototype.scrollIntoView as ReturnType<typeof vi.fn>;
    expect(spy).toHaveBeenCalledTimes(1);
    expect(spy.mock.instances[0]).toBe(screen.getByTestId('target'));
  });

  it('flags unread content only while scrolled above the bottom', () => {
    const { rerender } = render(<Host streaming={false} signal="a" lastId="m-2" />);
    const box = screen.getByTestId('box');
    Object.defineProperty(box, 'scrollHeight', { configurable: true, value: 1000 });
    Object.defineProperty(box, 'clientHeight', { configurable: true, value: 300 });
    box.scrollTop = 100;
    act(() => {
      box.dispatchEvent(new Event('scroll', { bubbles: true }));
    });
    expect(screen.getByTestId('unread').textContent).toBe('false');
    rerender(<Host streaming={false} signal="b" lastId="m-2" />);
    expect(screen.getByTestId('unread').textContent).toBe('true');
    box.scrollTop = 700;
    act(() => {
      box.dispatchEvent(new Event('scroll', { bubbles: true }));
    });
    expect(screen.getByTestId('unread').textContent).toBe('false');
  });
});
