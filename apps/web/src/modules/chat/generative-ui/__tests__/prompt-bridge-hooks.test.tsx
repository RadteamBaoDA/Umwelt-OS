import { renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { SEND_PROMPT_ACK_TYPE, SEND_PROMPT_ERROR_TYPE } from '../bridge/bridge-constants';
import { useFrameRegistry, useHostPromptDispatch, useSandboxPromptDraft } from '../bridge/prompt-bridge-hooks';

describe('prompt bridge hooks', () => {
  beforeEach(() => { vi.useFakeTimers(); vi.setSystemTime(1_000_000); });
  afterEach(() => { vi.useRealTimers(); });

  it('shares one cooldown between host and sandbox prompts', () => {
    const onSend = vi.fn();
    const onDraft = vi.fn();
    const { result } = renderHook(() => {
      const frames = useFrameRegistry();
      const disabledRef = { current: false };
      const host = useHostPromptDispatch({ frames, onSendPromptRef: { current: onSend }, disabledRef, cooldownMs: 1000 });
      const sandbox = useSandboxPromptDraft({ frames, onDraftPromptRef: { current: onDraft }, disabledRef, cooldownMs: 1000 });
      return { frames, host, sandbox };
    });
    const post = vi.fn();
    const frame = { postMessage: post };
    const cleanup = result.current.frames.registerFrame(frame, 'nonce-1');

    expect(result.current.host('first')).toBe(true);
    // A sandbox prompt right after a host prompt is inside the shared cooldown window.
    result.current.sandbox('second', frame, 'id-2');
    expect(onDraft).not.toHaveBeenCalled();
    expect(post).toHaveBeenCalledWith(expect.objectContaining({ type: SEND_PROMPT_ERROR_TYPE, id: 'id-2', retryable: true, message: 'Prompt rate limited', nonce: 'nonce-1' }), '*');

    vi.setSystemTime(1_000_000 + 1001);
    result.current.sandbox('third', frame, 'id-3');
    expect(onDraft).toHaveBeenCalledWith('third');
    expect(post).toHaveBeenCalledWith(expect.objectContaining({ type: SEND_PROMPT_ACK_TYPE, id: 'id-3' }), '*');
    cleanup();
  });

  it('never submits a sandbox prompt when disabled or when no draft handler exists', () => {
    const { result } = renderHook(() => {
      const frames = useFrameRegistry();
      const disabledRef = { current: true };
      return useSandboxPromptDraft({ frames, onDraftPromptRef: { current: undefined }, disabledRef, cooldownMs: 0 });
    });
    const post = vi.fn();
    result.current('hi', { postMessage: post }, 'x');
    expect(post).toHaveBeenCalledWith(expect.objectContaining({ type: SEND_PROMPT_ERROR_TYPE, message: 'Prompt bridge is disabled' }), '*');
  });

  it('drops a frame and its rate-limit history on cleanup', () => {
    const { result } = renderHook(() => useFrameRegistry());
    const frame = {};
    const cleanup = result.current.registerFrame(frame, 'n');
    result.current.frameRequestsRef.current.set(frame, [1]);
    expect(result.current.framesRef.current.has(frame)).toBe(true);
    cleanup();
    expect(result.current.framesRef.current.has(frame)).toBe(false);
    expect(result.current.frameRequestsRef.current.has(frame)).toBe(false);
    expect(result.current.registerFrame(null, 'n')()).toBeUndefined();
  });
});
