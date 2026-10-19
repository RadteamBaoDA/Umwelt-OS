'use client';

/**
 * Host–Sandbox Prompt Bridge (`bridge/prompt-bridge.tsx`).
 * Connects interactive generative UI widgets (both sandboxed `postMessage` iframes
 * and curated Pillar 2 React templates) to the host Umwelt-OS chat session so
 * clicking a widget action or calling `window.sendPrompt(prompt)` dispatches a
 * validated, rate-limited user follow-up message.
 */

import * as React from 'react';
import {
  DEFAULT_PROMPT_COOLDOWN_MS,
  MAX_PROMPT_LENGTH,
  SEND_PROMPT_EVENT,
  sendPromptParameters,
  validateBridgeMessage,
  isAllowedLinkUrl,
  type ValidatedBridgeMessage,
  type ValidateBridgeMessageOptions,
} from './sandbox-functions';

/** Message type posted back to a widget iframe when the host rejects its `sendPrompt` (retryable). */
export const SEND_PROMPT_ERROR_TYPE = 'umwelt:send-prompt-error';

/** Message type posted immediately once the host accepted a `sendPrompt` for processing (stops the in-frame ack timeout). */
export const SEND_PROMPT_ACK_TYPE = 'umwelt:send-prompt-ack';

/** Message type posted back to a widget iframe when the host accepted and handled its `sendPrompt`. */
export const SEND_PROMPT_RESULT_TYPE = 'umwelt:send-prompt-result';

export {
  DEFAULT_PROMPT_COOLDOWN_MS,
  MAX_PROMPT_LENGTH,
  SEND_PROMPT_EVENT,
  sendPromptParameters,
  validateBridgeMessage,
  isAllowedLinkUrl,
  type ValidatedBridgeMessage,
  type ValidateBridgeMessageOptions,
};

export interface PromptBridgeContextValue {
  /**
   * Validates and submits a follow-up prompt to the active chat session.
   * Enforces non-empty trimming, 4000-character bound, and rate-limit cooldown.
   *
   * @param prompt - Raw prompt string from a widget action.
   * @returns `true` if the prompt was accepted and forwarded to `onSendPrompt`; `false` otherwise.
   */
  sendPrompt: (prompt: string) => boolean;
  /**
   * Registers a sandboxed iframe's `contentWindow` as an authorized `postMessage` source.
   * Messages are accepted only from registered windows that echo the frame's `nonce`; host
   * replies to the frame carry the same nonce.
   *
   * @param frameWindow - `iframe.contentWindow` to authorize.
   * @param nonce - Per-frame secret embedded in the frame's `srcdoc`.
   * @returns Cleanup callback removing the frame from the authorized set.
   */
  registerFrame: (frameWindow: unknown, nonce: string) => () => void;
  /** Whether the bridge is currently disabled (e.g. while a chat response is already streaming). */
  disabled: boolean;
}

const defaultBridgeContext: PromptBridgeContextValue = {
  sendPrompt: () => false,
  registerFrame: () => () => undefined,
  disabled: false,
};

/** React context providing `sendPrompt` and `registerFrame` to generative UI widgets. */
export const PromptBridgeContext =
  React.createContext<PromptBridgeContextValue>(defaultBridgeContext);

export interface PromptBridgeProviderProps {
  /** Callback invoked when a validated prompt is accepted from a widget or sandboxed iframe. */
  onSendPrompt: (prompt: string) => void | Promise<void>;
  /** Optional callback invoked when a sandboxed iframe reports a validated resize height. */
  onResize?: (height: number, source: unknown) => void;
  /** Rate-limit cooldown in milliseconds between consecutive prompt submissions. Defaults to 500ms. */
  cooldownMs?: number;
  /** Disables prompt submission when true (e.g. while an assistant run is already active). */
  disabled?: boolean;
  /** Descendant chat transcript / generative UI tree. */
  children?: React.ReactNode;
}

/**
 * Provides the Host–Sandbox Prompt Bridge to descendant components and attaches a
 * `window` `message` event listener that validates incoming `umwelt:send-prompt` and
 * `umwelt:resize` events from sandboxed iframes.
 *
 * @param props - `PromptBridgeProviderProps` configuring `onSendPrompt`, `cooldownMs`, source validation, and children.
 * @returns Context provider wrapping `children`.
 */
export function PromptBridgeProvider({
  onSendPrompt,
  onResize,
  cooldownMs = DEFAULT_PROMPT_COOLDOWN_MS,
  disabled = false,
  children,
}: PromptBridgeProviderProps) {
  const onSendPromptRef = React.useRef(onSendPrompt);
  const onResizeRef = React.useRef(onResize);
  const disabledRef = React.useRef(disabled);
  const lastAcceptedAtRef = React.useRef<number | null>(null);
  const registeredFramesRef = React.useRef<Map<unknown, string>>(new Map());

  React.useEffect(() => {
    onSendPromptRef.current = onSendPrompt;
  }, [onSendPrompt]);

  React.useEffect(() => {
    onResizeRef.current = onResize;
  }, [onResize]);

  React.useEffect(() => {
    disabledRef.current = disabled;
  }, [disabled]);

  const registerFrame = React.useCallback((frameWindow: unknown, nonce: string) => {
    if (!frameWindow || !nonce) {
      return () => undefined;
    }
    registeredFramesRef.current.set(frameWindow, nonce);
    return () => {
      registeredFramesRef.current.delete(frameWindow);
    };
  }, []);

  /**
   * Posts a host-to-frame reply carrying the frame's nonce so the in-frame listener accepts it.
   * Failures (detached frame) are swallowed: there is nobody left to notify.
   */
  const replyToFrame = React.useCallback((target: unknown, message: Record<string, unknown>) => {
    const frame = target as { postMessage?: (data: unknown, origin: string) => void } | null | undefined;
    try {
      frame?.postMessage?.({ ...message, nonce: registeredFramesRef.current.get(target) }, '*');
    } catch {
      // Frame already detached; nothing left to notify.
    }
  }, []);

  const dispatchValidatedPrompt = React.useCallback(
    (rawPrompt: string, replyTo?: unknown, requestId?: string): boolean => {
      // Every outcome is reported to the originating iframe (when any) so `window.sendPrompt`
      // settles: result on success, retryable error on rejection (busy, cooldown, invalid).
      const reportFailure = (err: unknown) => {
        const message = err instanceof Error ? err.message : String(err);
        replyToFrame(replyTo, { type: SEND_PROMPT_ERROR_TYPE, id: requestId, retryable: true, message });
      };
      if (disabledRef.current) {
        reportFailure('Prompt bridge is disabled');
        return false;
      }
      const parsed = sendPromptParameters.safeParse({ text: rawPrompt });
      if (!parsed.success) {
        reportFailure('Invalid prompt');
        return false;
      }
      const now = Date.now();
      if (
        cooldownMs > 0 &&
        lastAcceptedAtRef.current !== null &&
        now - lastAcceptedAtRef.current < cooldownMs
      ) {
        reportFailure('Prompt rate limited');
        return false;
      }
      lastAcceptedAtRef.current = now;
      // Ack before the (possibly long) handler runs: the frame's timeout covers delivery only.
      replyToFrame(replyTo, { type: SEND_PROMPT_ACK_TYPE, id: requestId });
      // A rejected/throwing handler must not be silently dropped nor leak an unhandled rejection.
      try {
        Promise.resolve(onSendPromptRef.current(parsed.data.text)).then(
          () => replyToFrame(replyTo, { type: SEND_PROMPT_RESULT_TYPE, id: requestId, ok: true }),
          reportFailure,
        );
      } catch (err) {
        reportFailure(err);
      }
      return true;
    },
    [cooldownMs, replyToFrame],
  );

  React.useEffect(() => {
    /** Single owner of frame -> host messages (send-prompt, resize, open-link). */
    const handleWindowMessage = (event: MessageEvent) => {
      const validated = validateBridgeMessage(event, { frames: registeredFramesRef.current });

      if (!validated) {
        return;
      }

      if (validated.type === 'send-prompt') {
        dispatchValidatedPrompt(validated.prompt, event.source, validated.id);
      } else if (validated.type === 'resize') {
        onResizeRef.current?.(validated.height, event.source);
      } else if (validated.type === 'open-link') {
        if (typeof window !== 'undefined') {
          window.open(validated.url, '_blank', 'noopener,noreferrer');
        }
      }
    };

    window.addEventListener('message', handleWindowMessage);
    return () => {
      window.removeEventListener('message', handleWindowMessage);
    };
  }, [dispatchValidatedPrompt]);

  const contextValue = React.useMemo<PromptBridgeContextValue>(
    () => ({
      sendPrompt: dispatchValidatedPrompt,
      registerFrame,
      disabled,
    }),
    [dispatchValidatedPrompt, registerFrame, disabled],
  );

  return (
    <PromptBridgeContext.Provider value={contextValue}>
      {children}
    </PromptBridgeContext.Provider>
  );
}

/**
 * Hook returning the active `PromptBridgeContextValue` (`sendPrompt`, `registerFrame`, `disabled`).
 *
 * @returns `PromptBridgeContextValue` from the nearest `PromptBridgeProvider`.
 */
export function usePromptBridge(): PromptBridgeContextValue {
  return React.useContext(PromptBridgeContext);
}
