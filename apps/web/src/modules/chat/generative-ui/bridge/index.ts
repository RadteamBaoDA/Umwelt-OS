/**
 * Public exports for the Host–Sandbox Prompt Bridge (`bridge/index.ts`).
 */

export {
  PromptBridgeContext,
  PromptBridgeProvider,
  usePromptBridge,
  type PromptBridgeContextValue,
  type PromptBridgeProviderProps,
} from './prompt-bridge';

export {
  DEFAULT_PROMPT_COOLDOWN_MS,
  MAX_PROMPT_LENGTH,
  MIN_SANDBOX_HEIGHT,
  MAX_SANDBOX_HEIGHT,
  SEND_PROMPT_EVENT,
  sendPromptParameters,
  validateBridgeMessage,
  isAllowedLinkUrl,
  type ValidatedBridgeMessage,
  type ValidateBridgeMessageOptions,
} from './sandbox-functions';
