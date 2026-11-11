'use client';

/**
 * Host–Sandbox Prompt Bridge (`bridge/prompt-bridge.tsx`).
 * Connects interactive generative UI widgets (both sandboxed `postMessage` iframes
 * and curated Pillar 2 React templates) to the host Umwelt-OS chat session so
 * clicking a widget action or calling `window.sendPrompt(prompt)` dispatches a
 * validated, rate-limited user follow-up message.
 */

import * as React from 'react';
import { DEFAULT_LINK_CONFIRM_LABELS, WidgetLinkConfirmDialog, useWidgetLinkConfirm, type LinkConfirmLabels } from './link-confirm';
import { genUiCopy } from '../copy';
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
   * @param onResize - Optional receiver of the frame's validated, clamped content height (pixels).
   * @returns Cleanup callback removing the frame from the authorized set.
   */
  registerFrame: (frameWindow: unknown, nonce: string, onResize?: (height: number) => void) => () => void;
  /** Whether the bridge is currently disabled (e.g. while a chat response is already streaming). */
  disabled: boolean;
  /** Current host-side widget prompt status text (drafted / refused), or null; rendered by {@link PromptBridgeNotice}. */
  notice: string | null;
}

const defaultBridgeContext: PromptBridgeContextValue = {
  sendPrompt: () => false,
  registerFrame: () => () => undefined,
  disabled: false,
  notice: null,
};

/** React context providing `sendPrompt` and `registerFrame` to generative UI widgets. */
export const PromptBridgeContext = React.createContext<PromptBridgeContextValue>(defaultBridgeContext);

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
  /** Rate-limit cooldown in milliseconds between consecutive prompt submissions. Defaults to 500ms. */
  cooldownMs?: number;
  /** Disables prompt submission when true (e.g. while an assistant run is already active). */
  disabled?: boolean;
  /** Descendant chat transcript / generative UI tree. */
  children?: React.ReactNode;
}

/** How long the host-side widget prompt status line stays visible. */
const NOTICE_MS = 6000;

/**
 * Host-side status line for widget prompt drafts: a localized message (active locale) that clears itself.
 *
 * @returns The current message (or null) and `showNotice`, which maps a draft outcome to its message and restarts the timer.
 */
function useDraftNotice() {
  const [notice, setNotice] = React.useState<string | null>(null);
  const timerRef = React.useRef<ReturnType<typeof setTimeout> | null>(null);
  const showNotice = React.useCallback((outcome: { kind: 'drafted' | 'blocked'; message?: string }) => {
    setNotice(outcome.kind === 'drafted' ? genUiCopy().genUiPromptDrafted : (outcome.message ?? genUiCopy().genUiPromptComposerBusy));
    if (timerRef.current) clearTimeout(timerRef.current);
    timerRef.current = setTimeout(() => setNotice(null), NOTICE_MS);
  }, []);
  React.useEffect(
    () => () => {
      if (timerRef.current) clearTimeout(timerRef.current);
    },
    [],
  );
  return { notice, showNotice };
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
  cooldownMs = DEFAULT_PROMPT_COOLDOWN_MS,
  disabled = false,
  children,
}: PromptBridgeProviderProps) {
  const onSendPromptRef = useLatest(onSendPrompt);
  const onDraftPromptRef = useLatest(onDraftPrompt);
  const disabledRef = useLatest(disabled);
  // Widget-requested link awaiting the owner's explicit confirmation (one at a time; extra requests are dropped).
  const linkConfirm = useWidgetLinkConfirm();
  const requestLink = linkConfirm.requestLink;
  const frames = useFrameRegistry();
  const dispatchValidatedPrompt = useHostPromptDispatch({ frames, onSendPromptRef, disabledRef, cooldownMs });
  const { notice, showNotice } = useDraftNotice();
  const draftSandboxPrompt = useSandboxPromptDraft({ frames, onDraftPromptRef, disabledRef, cooldownMs, onNotice: showNotice });
  const handleLoadImage = useWidgetImageLoader(frames);
  const { registerFrame } = frames;

  useBridgeMessageListener({ frames, draftSandboxPrompt, handleLoadImage, requestLink });

  const contextValue = React.useMemo<PromptBridgeContextValue>(
    () => ({
      sendPrompt: dispatchValidatedPrompt,
      registerFrame,
      disabled,
      notice,
    }),
    [dispatchValidatedPrompt, registerFrame, disabled, notice],
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

/**
 * Inline status line for widget prompt drafts. The live region is always mounted (only its text toggles) so assistive
 * technology announces the change, and it belongs in the session stack above the composer rather than floating over it.
 *
 * @returns A `role="status"` container; visually empty when there is no notice.
 */
export function PromptBridgeNotice() {
  const { notice } = usePromptBridge();
  return (
    <div role="status" data-testid="widget-prompt-notice" className={notice ? 'shrink-0 px-4' : 'sr-only'}>
      {notice && <p className="rounded-md border border-border bg-surface px-3 py-2 text-sm text-foreground">{notice}</p>}
    </div>
  );
}
