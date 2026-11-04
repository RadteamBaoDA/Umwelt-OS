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
  DEFAULT_LINK_CONFIRM_LABELS,
  WidgetLinkConfirmDialog,
  useWidgetLinkConfirm,
  type LinkConfirmLabels,
} from './link-confirm';
import {
  useBridgeMessageListener,
  useFrameRegistry,
  useHostPromptDispatch,
  useLatest,
  useSandboxPromptDraft,
  useWidgetImageLoader,
} from './prompt-bridge-hooks';
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
  SEND_PROMPT_ERROR_TYPE,
  SEND_PROMPT_ACK_TYPE,
  SEND_PROMPT_RESULT_TYPE,
  LOAD_IMAGE_RESULT_TYPE,
  LOAD_IMAGE_ERROR_TYPE,
  IMAGE_RATE_WINDOW_MS,
  IMAGE_RATE_MAX,
  PRIVACY_CACHE_MS,
} from './bridge-constants';

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

export type { LinkConfirmLabels };

export interface PromptBridgeProviderProps {
  /** Callback invoked for prompts submitted through the in-host `sendPrompt` context function (trusted host code only). */
  onSendPrompt: (prompt: string) => void | Promise<void>;
  /**
   * Receives a prompt requested by a SANDBOXED widget. It must only place the text in the chat composer; the user
   * presses send. When omitted, sandbox prompts are rejected (fail closed) because the host cannot verify a user gesture.
   */
  onDraftPrompt?: (prompt: string) => void;
  /** Localized strings for the open-link confirmation dialog (English fallback). */
  linkConfirmLabels?: LinkConfirmLabels;
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
  onDraftPrompt,
  linkConfirmLabels = DEFAULT_LINK_CONFIRM_LABELS,
  onResize,
  cooldownMs = DEFAULT_PROMPT_COOLDOWN_MS,
  disabled = false,
  children,
}: PromptBridgeProviderProps) {
  const onSendPromptRef = useLatest(onSendPrompt);
  const onDraftPromptRef = useLatest(onDraftPrompt);
  const onResizeRef = useLatest(onResize);
  const disabledRef = useLatest(disabled);
  // Widget-requested link awaiting the owner's explicit confirmation (one at a time; extra requests are dropped).
  const linkConfirm = useWidgetLinkConfirm();
  const requestLink = linkConfirm.requestLink;
  const frames = useFrameRegistry();
  const dispatchValidatedPrompt = useHostPromptDispatch({ frames, onSendPromptRef, disabledRef, cooldownMs });
  const draftSandboxPrompt = useSandboxPromptDraft({ frames, onDraftPromptRef, disabledRef, cooldownMs });
  const handleLoadImage = useWidgetImageLoader(frames);
  const { registerFrame } = frames;

  useBridgeMessageListener({ frames, onResizeRef, draftSandboxPrompt, handleLoadImage, requestLink });

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
      <WidgetLinkConfirmDialog url={linkConfirm.pendingLink} labels={linkConfirmLabels} onResolve={linkConfirm.resolve} />
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
