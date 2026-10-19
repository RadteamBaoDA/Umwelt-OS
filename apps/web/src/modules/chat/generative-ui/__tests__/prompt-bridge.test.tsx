import * as React from 'react';
import { act, cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  PromptBridgeProvider,
  usePromptBridge,
  validateBridgeMessage,
} from '../bridge';

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
      validateBridgeMessage(
        { ...base, data: { type: 'umwelt:send-prompt', prompt: '  Compare Q3 vs Q4  ', nonce: NONCE } },
        opts,
      ),
    ).toEqual({ type: 'send-prompt', prompt: 'Compare Q3 vs Q4', rawType: 'umwelt:send-prompt' });

    expect(
      validateBridgeMessage({ ...base, data: { type: 'umwelt:resize', height: 482.4, nonce: NONCE } }, opts),
    ).toEqual({ type: 'resize', height: 483, rawType: 'umwelt:resize' });

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
    expect(
      validateBridgeMessage({ data: { ...data, nonce: 'other' }, source: trusted, origin: 'null' }, opts),
    ).toBeNull();
    expect(
      validateBridgeMessage({ data: { type: data.type, prompt: 'hi' }, source: trusted, origin: 'null' }, opts),
    ).toBeNull();
    // Foreign / missing origin.
    expect(
      validateBridgeMessage({ data, source: trusted, origin: 'https://evil.example' }, opts),
    ).toBeNull();
    expect(validateBridgeMessage({ data, source: trusted }, opts)).toBeNull();
    // Host origin is accepted.
    expect(
      validateBridgeMessage({ data, source: trusted, origin: window.location.origin }, opts),
    ).not.toBeNull();
    // No frames registered (or option omitted) rejects everything.
    expect(validateBridgeMessage({ data, source: trusted, origin: 'null' })).toBeNull();
    expect(
      validateBridgeMessage({ data, source: trusted, origin: 'null' }, { frames: new Map() }),
    ).toBeNull();
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
    const onResize = vi.fn();
    const frame = { postMessage: vi.fn() };

    function Registrar() {
      const { registerFrame } = usePromptBridge();
      React.useEffect(() => registerFrame(frame, NONCE), [registerFrame]);
      return null;
    }
    render(
      <PromptBridgeProvider onSendPrompt={onSendPrompt} onResize={onResize} cooldownMs={0}>
        <Registrar />
      </PromptBridgeProvider>,
    );

    const post = (data: object, source: unknown = frame, origin = 'null') =>
      act(() => {
        window.dispatchEvent(
          new MessageEvent('message', { data, source: source as Window, origin }),
        );
      });

    post({ type: 'umwelt:send-prompt', prompt: 'bad nonce', nonce: 'x' });
    post({ type: 'umwelt:send-prompt', prompt: 'foreign', nonce: NONCE }, { postMessage: vi.fn() });
    post({ type: 'umwelt:send-prompt', prompt: 'bad origin', nonce: NONCE }, frame, 'https://evil.example');
    expect(onSendPrompt).not.toHaveBeenCalled();

    post({ type: 'umwelt:send-prompt', id: 'p1', prompt: 'Drill down', nonce: NONCE });
    post({ type: 'umwelt:resize', height: 615, nonce: NONCE });

    expect(onSendPrompt).toHaveBeenCalledTimes(1);
    expect(onSendPrompt).toHaveBeenCalledWith('Drill down');
    expect(onResize).toHaveBeenCalledWith(615, frame);
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
