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

describe('validateBridgeMessage & PromptBridgeProvider', () => {
  it('validates umwelt:send-prompt, sendPrompt, and umwelt:resize message schemas', () => {
    const validPrompt = validateBridgeMessage({
      data: { type: 'umwelt:send-prompt', prompt: '  Compare Q3 vs Q4 revenue  ' },
    });
    expect(validPrompt).toEqual({
      type: 'send-prompt',
      prompt: 'Compare Q3 vs Q4 revenue',
      rawType: 'umwelt:send-prompt',
    });

    const validResize = validateBridgeMessage({
      data: { type: 'umwelt:resize', height: 482.4 },
    });
    expect(validResize).toEqual({
      type: 'resize',
      height: 483,
      rawType: 'umwelt:resize',
    });

    // Reject empty prompts, oversized prompts (>4000 chars), and unknown message types
    expect(
      validateBridgeMessage({ data: { type: 'umwelt:send-prompt', prompt: '   ' } }),
    ).toBeNull();
    expect(
      validateBridgeMessage({
        data: { type: 'umwelt:send-prompt', prompt: 'x'.repeat(4001) },
      }),
    ).toBeNull();
    expect(
      validateBridgeMessage({ data: { type: 'unknown:event', prompt: 'hello' } }),
    ).toBeNull();
  });

  it('rejects messages from unauthorized sources when allowedSources or requireSourceMatch is set', () => {
    const trustedWindow = {} as Window;
    const untrustedWindow = {} as Window;

    expect(
      validateBridgeMessage(
        {
          data: { type: 'umwelt:send-prompt', prompt: 'From untrusted' },
          source: untrustedWindow,
        },
        { allowedSources: [trustedWindow], requireSourceMatch: true },
      ),
    ).toBeNull();

    expect(
      validateBridgeMessage(
        {
          data: { type: 'umwelt:send-prompt', prompt: 'From trusted' },
          source: trustedWindow,
        },
        { allowedSources: [trustedWindow], requireSourceMatch: true },
      ),
    ).toEqual({
      type: 'send-prompt',
      prompt: 'From trusted',
      rawType: 'umwelt:send-prompt',
    });
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

  it('processes window postMessage events from an authorized source', () => {
    const onSendPrompt = vi.fn();
    const onResize = vi.fn();
    const fakeFrameWindow = {} as Window;

    render(
      <PromptBridgeProvider
        onSendPrompt={onSendPrompt}
        onResize={onResize}
        allowedSources={[fakeFrameWindow]}
        cooldownMs={0}
      >
        <div>Host Chat</div>
      </PromptBridgeProvider>,
    );

    act(() => {
      window.dispatchEvent(
        new MessageEvent('message', {
          data: { type: 'umwelt:send-prompt', prompt: 'Drill down into Asia-Pacific' },
          source: fakeFrameWindow,
        }),
      );
      window.dispatchEvent(
        new MessageEvent('message', {
          data: { type: 'umwelt:resize', height: 615 },
          source: fakeFrameWindow,
        }),
      );
    });

    expect(onSendPrompt).toHaveBeenCalledWith('Drill down into Asia-Pacific');
    expect(onResize).toHaveBeenCalledWith(615, fakeFrameWindow);
  });
});
