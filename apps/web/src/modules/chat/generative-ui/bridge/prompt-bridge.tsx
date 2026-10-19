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
   *
   * @param frameWindow - `iframe.contentWindow` to authorize.
   * @returns Cleanup callback removing the frame from the authorized set.
   */
  registerFrame: (frameWindow: unknown) => () => void;
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
  /** Optional explicit set/array of allowed `MessageEvent.source` windows. */
  allowedSources?: ReadonlySet<unknown> | readonly unknown[];
  /** When true, rejects `window.postMessage` events unless `event.source` matches a registered or allowed source. */
  requireSourceMatch?: boolean;
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
  allowedSources,
  requireSourceMatch = false,
  children,
}: PromptBridgeProviderProps) {
  const onSendPromptRef = React.useRef(onSendPrompt);
  const onResizeRef = React.useRef(onResize);
  const disabledRef = React.useRef(disabled);
  const lastAcceptedAtRef = React.useRef<number | null>(null);
  const registeredFramesRef = React.useRef<Set<unknown>>(new Set());

  React.useEffect(() => {
    onSendPromptRef.current = onSendPrompt;
  }, [onSendPrompt]);

  React.useEffect(() => {
    onResizeRef.current = onResize;
  }, [onResize]);

  React.useEffect(() => {
    disabledRef.current = disabled;
  }, [disabled]);

  const registerFrame = React.useCallback((frameWindow: unknown) => {
    if (!frameWindow) {
      return () => undefined;
    }
    registeredFramesRef.current.add(frameWindow);
    return () => {
      registeredFramesRef.current.delete(frameWindow);
    };
  }, []);

  const dispatchValidatedPrompt = React.useCallback(
    (rawPrompt: string): boolean => {
      if (disabledRef.current) {
        return false;
      }
      const parsed = sendPromptParameters.safeParse({ text: rawPrompt });
      if (!parsed.success) {
        return false;
      }
      const now = Date.now();
      if (
        cooldownMs > 0 &&
        lastAcceptedAtRef.current !== null &&
        now - lastAcceptedAtRef.current < cooldownMs
      ) {
        return false;
      }
      lastAcceptedAtRef.current = now;
      void onSendPromptRef.current(parsed.data.text);
      return true;
    },
    [cooldownMs],
  );

  React.useEffect(() => {
    const handleWindowMessage = (event: MessageEvent) => {
      const effectiveSources =
        allowedSources ??
        (registeredFramesRef.current.size > 0
          ? registeredFramesRef.current
          : undefined);
      const effectiveRequireMatch =
        requireSourceMatch ||
        Boolean(allowedSources) ||
        registeredFramesRef.current.size > 0;

      const validated = validateBridgeMessage(event, {
        allowedSources: effectiveSources,
        requireSourceMatch: effectiveRequireMatch,
      });

      if (!validated) {
        return;
      }

      if (validated.type === 'send-prompt') {
        dispatchValidatedPrompt(validated.prompt);
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
  }, [allowedSources, dispatchValidatedPrompt, requireSourceMatch]);

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
