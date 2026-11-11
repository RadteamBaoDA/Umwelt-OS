import * as React from 'react';
import { act, cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { PromptBridgeProvider, usePromptBridge, validateBridgeMessage } from '../bridge';

afterEach(() => {
  cleanup();
});

function BridgeConsumerButton({ promptText }: { promptText: string }) {
  const { sendPrompt, disabled } = usePromptBridge();
  return (
    <button
      type="button"
      disabled={disabled}
      onClick={() => {
        sendPrompt(promptText);
      }}
    >
      Trigger bridge prompt
    </button>
  );
}

const NONCE = 'nonce-1';

/** Builds a frame registry with one trusted window. */
function framesFor(win: unknown, nonce = NONCE) {
  return new Map<unknown, string>([[win, nonce]]);
}

describe('validateBridgeMessage & PromptBridgeProvider', () => {
  it('validates umwelt:send-prompt and umwelt:resize message schemas', () => {
    const win = {} as Window;
    const opts = { frames: framesFor(win) };
    const base = { source: win, origin: 'null' };

    expect(
      validateBridgeMessage({ ...base, data: { type: 'umwelt:send-prompt', prompt: '  Compare Q3 vs Q4  ', nonce: NONCE } }, opts),
    ).toEqual({ type: 'send-prompt', prompt: 'Compare Q3 vs Q4', rawType: 'umwelt:send-prompt' });

    expect(validateBridgeMessage({ ...base, data: { type: 'umwelt:resize', height: 482.4, nonce: NONCE } }, opts)).toEqual({
      type: 'resize',
      height: 483,
      rawType: 'umwelt:resize',
    });

    for (const data of [
      { type: 'umwelt:send-prompt', prompt: '   ' },
      { type: 'umwelt:send-prompt', prompt: 'x'.repeat(4001) },
      { type: 'unknown:event', prompt: 'hello' },
    ]) {
      expect(validateBridgeMessage({ ...base, data: { ...data, nonce: NONCE } }, opts)).toBeNull();
    }
  });

  it('fails closed: foreign source, wrong/missing nonce, bad origin, no frames registered', () => {
    const trusted = {} as Window;
    const data = { type: 'umwelt:send-prompt', prompt: 'hi', nonce: NONCE };
    const opts = { frames: framesFor(trusted) };

    expect(validateBridgeMessage({ data, source: trusted, origin: 'null' }, opts)).not.toBeNull();
    // Foreign source window.
    expect(validateBridgeMessage({ data, source: {} as Window, origin: 'null' }, opts)).toBeNull();
    // Missing source.
    expect(validateBridgeMessage({ data, origin: 'null' }, opts)).toBeNull();
    // Nonce mismatch / missing.
    expect(validateBridgeMessage({ data: { ...data, nonce: 'other' }, source: trusted, origin: 'null' }, opts)).toBeNull();
    expect(validateBridgeMessage({ data: { type: data.type, prompt: 'hi' }, source: trusted, origin: 'null' }, opts)).toBeNull();
    // Foreign / missing origin.
    expect(validateBridgeMessage({ data, source: trusted, origin: 'https://evil.example' }, opts)).toBeNull();
    expect(validateBridgeMessage({ data, source: trusted }, opts)).toBeNull();
    // Host origin is accepted.
    expect(validateBridgeMessage({ data, source: trusted, origin: window.location.origin }, opts)).not.toBeNull();
    // No frames registered (or option omitted) rejects everything.
    expect(validateBridgeMessage({ data, source: trusted, origin: 'null' })).toBeNull();
    expect(validateBridgeMessage({ data, source: trusted, origin: 'null' }, { frames: new Map() })).toBeNull();
  });

  it('invokes onSendPrompt via context hook and rate-limits rapid duplicate submissions', async () => {
    const onSendPrompt = vi.fn();
    render(
      <PromptBridgeProvider onSendPrompt={onSendPrompt} cooldownMs={500}>
        <BridgeConsumerButton promptText="  Focus on renewable energy  " />
      </PromptBridgeProvider>,
    );

    const button = screen.getByRole('button', { name: 'Trigger bridge prompt' });
    await userEvent.click(button);
    await userEvent.click(button);

    expect(onSendPrompt).toHaveBeenCalledTimes(1);
    expect(onSendPrompt).toHaveBeenCalledWith('Focus on renewable energy');
  });

  it('processes window messages only from a registered frame with the right nonce, and replies', async () => {
    const onSendPrompt = vi.fn().mockResolvedValue(undefined);
    const onDraftPrompt = vi.fn();
    const onResize = vi.fn();
    const frame = { postMessage: vi.fn() };

    function Registrar() {
      const { registerFrame } = usePromptBridge();
      React.useEffect(() => registerFrame(frame, NONCE, onResize), [registerFrame]);
      return null;
    }
    render(
      <PromptBridgeProvider onSendPrompt={onSendPrompt} onDraftPrompt={onDraftPrompt} cooldownMs={0}>
        <Registrar />
      </PromptBridgeProvider>,
    );

    const post = (data: object, source: unknown = frame, origin = 'null') =>
      act(() => {
        window.dispatchEvent(new MessageEvent('message', { data, source: source as Window, origin }));
      });

    post({ type: 'umwelt:send-prompt', prompt: 'bad nonce', nonce: 'x' });
    post({ type: 'umwelt:send-prompt', prompt: 'foreign', nonce: NONCE }, { postMessage: vi.fn() });
    post({ type: 'umwelt:send-prompt', prompt: 'bad origin', nonce: NONCE }, frame, 'https://evil.example');
    expect(onDraftPrompt).not.toHaveBeenCalled();

    post({ type: 'umwelt:send-prompt', id: 'p1', prompt: 'Drill down', nonce: NONCE });
    post({ type: 'umwelt:resize', height: 615, nonce: NONCE });

    expect(onDraftPrompt).toHaveBeenCalledTimes(1);
    expect(onDraftPrompt).toHaveBeenCalledWith('Drill down');
    expect(onSendPrompt).not.toHaveBeenCalled();
    expect(onResize).toHaveBeenCalledWith(615);
    await vi.waitFor(() => expect(frame.postMessage).toHaveBeenCalled());
    expect(frame.postMessage.mock.calls[0][0]).toEqual({
      type: 'umwelt:send-prompt-ack',
      id: 'p1',
      nonce: NONCE,
    });
    expect(frame.postMessage.mock.calls[1][0]).toEqual({
      type: 'umwelt:send-prompt-result',
      id: 'p1',
      ok: true,
      drafted: true,
      nonce: NONCE,
    });
  });

  it('rejects every message when no frame is registered', () => {
    const onSendPrompt = vi.fn();
    const frame = { postMessage: vi.fn() };
    render(
      <PromptBridgeProvider onSendPrompt={onSendPrompt} cooldownMs={0}>
        <span />
      </PromptBridgeProvider>,
    );
    act(() => {
      window.dispatchEvent(
        new MessageEvent('message', {
          data: { type: 'umwelt:send-prompt', prompt: 'hi', nonce: NONCE },
          source: frame as unknown as Window,
          origin: 'null',
        }),
      );
    });
    expect(onSendPrompt).not.toHaveBeenCalled();
  });
});

describe('PromptBridgeProvider sandbox hardening (F2.6)', () => {
  const frame = { postMessage: vi.fn() };

  /** Mounts a provider with one registered frame and returns a poster for frame messages. */
  function mount(props: Partial<React.ComponentProps<typeof PromptBridgeProvider>> = {}) {
    function Registrar() {
      const { registerFrame } = usePromptBridge();
      React.useEffect(() => registerFrame(frame, NONCE), [registerFrame]);
      return null;
    }
    render(
      <PromptBridgeProvider onSendPrompt={vi.fn()} cooldownMs={0} {...props}>
        <Registrar />
      </PromptBridgeProvider>,
    );
    return (data: object, source: unknown = frame) =>
      act(() => {
        window.dispatchEvent(new MessageEvent('message', { data, source: source as Window, origin: 'null' }));
      });
  }

  it('does not open a widget link until the owner confirms, and shows the full URL', async () => {
    const open = vi.spyOn(window, 'open').mockReturnValue(null);
    const post = mount();
    const url = 'https://example.com/path?token=abc#frag';
    post({ type: 'umwelt:open-link', url, nonce: NONCE });
    expect(open).not.toHaveBeenCalled();
    expect(await screen.findByTestId('genui-link-url')).toHaveTextContent(url);

    await userEvent.click(screen.getByRole('button', { name: 'Open link' }));
    expect(open).toHaveBeenCalledTimes(1);
    expect(open).toHaveBeenCalledWith(url, '_blank', 'noopener,noreferrer');
    open.mockRestore();
  });

  it('never opens the link when the owner cancels, and ignores unauthenticated link requests', async () => {
    const open = vi.spyOn(window, 'open').mockReturnValue(null);
    const post = mount();
    post({ type: 'umwelt:open-link', url: 'https://evil.example', nonce: 'wrong' });
    post({ type: 'umwelt:open-link', url: 'https://evil.example' }, { postMessage: vi.fn() });
    expect(screen.queryByTestId('genui-link-url')).toBeNull();

    post({ type: 'umwelt:open-link', url: 'https://example.com/ok', nonce: NONCE });
    await userEvent.click(await screen.findByRole('button', { name: 'Cancel' }));
    expect(open).not.toHaveBeenCalled();
    expect(screen.queryByTestId('genui-link-url')).toBeNull();
    open.mockRestore();
  });

  it('suppresses repeat link requests from a frame after the owner cancels', async () => {
    const open = vi.spyOn(window, 'open').mockReturnValue(null);
    const post = mount();
    post({ type: 'umwelt:open-link', url: 'https://example.com/a', nonce: NONCE });
    await userEvent.click(await screen.findByRole('button', { name: 'Cancel' }));
    post({ type: 'umwelt:open-link', url: 'https://example.com/b', nonce: NONCE });
    expect(screen.queryByTestId('genui-link-url')).toBeNull();
    expect(open).not.toHaveBeenCalled();
    open.mockRestore();
  });

  it('uses localized labels for the confirmation dialog', async () => {
    const post = mount({
      linkConfirmLabels: { title: 'Mở liên kết bên ngoài?', description: 'mô tả', open: 'Mở liên kết', cancel: 'Hủy' },
    });
    post({ type: 'umwelt:open-link', url: 'https://example.com', nonce: NONCE });
    expect(await screen.findByText('Mở liên kết bên ngoài?')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Hủy' })).toBeInTheDocument();
  });

  it('rejects a sandbox send-prompt (fail closed) when the host provides no draft handler', () => {
    const onSendPrompt = vi.fn();
    frame.postMessage.mockClear();
    const post = mount({ onSendPrompt });
    post({ type: 'umwelt:send-prompt', id: 'z', prompt: 'auto submit', nonce: NONCE });
    expect(onSendPrompt).not.toHaveBeenCalled();
    expect(frame.postMessage.mock.calls[0][0]).toMatchObject({ type: 'umwelt:send-prompt-error', id: 'z', retryable: true });
  });

  it('only fills the composer for a sandbox send-prompt and never calls onSendPrompt', () => {
    const onSendPrompt = vi.fn();
    const onDraftPrompt = vi.fn();
    const post = mount({ onSendPrompt, onDraftPrompt });
    post({ type: 'umwelt:send-prompt', prompt: 'hello', nonce: NONCE });
    expect(onDraftPrompt).toHaveBeenCalledWith('hello');
    expect(onSendPrompt).not.toHaveBeenCalled();
  });

  it('shows a host-side status line when a widget drafts a prompt and when the composer refuses it (G2)', () => {
    const onDraftPrompt = vi.fn();
    const post = mount({ onDraftPrompt });
    post({ type: 'umwelt:send-prompt', prompt: 'hello', nonce: NONCE });
    expect(screen.getByTestId('widget-prompt-notice').textContent).toContain('message box');

    onDraftPrompt.mockImplementation(() => {
      throw new Error('Composer has text');
    });
    post({ type: 'umwelt:send-prompt', prompt: 'again', nonce: NONCE });
    expect(screen.getByTestId('widget-prompt-notice').textContent).toBe('Composer has text');
  });

  it('drops legacy wire message aliases (G8)', () => {
    const win = {};
    const opts = { frames: framesFor(win) };
    const base = { source: win, origin: 'null' };
    for (const type of ['sendPrompt', 'opengenui:send-prompt', 'resize', '__ogui_resize', 'openLink']) {
      expect(
        validateBridgeMessage({ ...base, data: { type, prompt: 'x', height: 100, url: 'https://a.example', nonce: NONCE } }, opts),
      ).toBeNull();
    }
  });
});
