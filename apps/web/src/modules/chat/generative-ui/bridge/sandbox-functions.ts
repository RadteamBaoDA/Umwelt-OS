/**
 * Sandbox bridge message validation and function schemas (`bridge/sandbox-functions.ts`).
 * Validates untrusted `window.postMessage` payloads from sandboxed iframes (`sandbox="allow-scripts"`),
 * enforcing source-window verification, strict schema validation, length limits, and HTTPS URL guards.
 */

import { z } from 'zod';

/** Custom DOM event name for host-side prompt dispatch. */
export const SEND_PROMPT_EVENT = 'umwelt:send-prompt';

/** Maximum character length permitted for a bridge-submitted prompt. */
export const MAX_PROMPT_LENGTH = 4000;

/** Default cooldown in milliseconds between consecutive prompt submissions from sandboxed widgets. */
export const DEFAULT_PROMPT_COOLDOWN_MS = 500;

/** Minimum allowed height in pixels for a sandboxed iframe. */
export const MIN_SANDBOX_HEIGHT = 50;

/** Maximum allowed height in pixels for a sandboxed iframe. */
export const MAX_SANDBOX_HEIGHT = 4000;

/** Zod schema validating prompt text payloads. */
export const sendPromptParameters = z.object({
  text: z.string().trim().min(1).max(MAX_PROMPT_LENGTH),
});

export type ValidatedBridgeMessage =
  | {
      type: 'send-prompt';
      prompt: string;
      rawType: 'umwelt:send-prompt' | 'sendPrompt' | 'opengenui:send-prompt';
    }
  | {
      type: 'resize';
      height: number;
      rawType: 'umwelt:resize' | 'resize' | '__ogui_resize';
    }
  | {
      type: 'open-link';
      url: string;
      rawType: 'umwelt:open-link' | 'openLink';
    };

export interface ValidateBridgeMessageOptions {
  /**
   * Optional set or array of registered `iframe.contentWindow` references.
   * When provided and non-empty (or when `requireSourceMatch` is true), messages whose
   * `event.source` is not in `allowedSources` are rejected.
   */
  allowedSources?: ReadonlySet< unknown > | readonly unknown[];
  /**
   * When true, requires `event.source` to be non-null and present in `allowedSources`.
   */
  requireSourceMatch?: boolean;
}

/**
 * Checks whether a candidate URL is a valid `https:` URL permitted for external opening.
 * Rejects `javascript:`, `data:`, `http:`, and malformed URLs.
 *
 * @param url - Candidate URL string.
 * @param allowedOrigins - Optional allowlist of origins.
 * @returns `true` if the URL is safe to open in a new tab.
 */
export function isAllowedLinkUrl(
  url: string,
  allowedOrigins?: readonly string[],
): boolean {
  let parsed: URL;
  try {
    parsed = new URL(url);
  } catch {
    return false;
  }
  if (parsed.protocol !== 'https:') {
    return false;
  }
  if (allowedOrigins && allowedOrigins.length > 0) {
    return allowedOrigins.includes(parsed.origin);
  }
  return true;
}

/**
 * Checks whether a `MessageEvent.source` matches one of the registered sandbox `contentWindow` sources.
 *
 * @param source - `event.source` from a `MessageEvent`.
 * @param allowedSources - Set or array of registered `WindowProxy` sources.
 * @param requireSourceMatch - Whether to reject when `allowedSources` is empty or `source` is null.
 * @returns `true` if the source is authorized.
 */
function isSourceAuthorized(
  source: unknown,
  allowedSources?: ReadonlySet<unknown> | readonly unknown[],
  requireSourceMatch = false,
): boolean {
  if (!allowedSources) {
    return !requireSourceMatch;
  }
  const sourceList = Array.isArray(allowedSources)
    ? allowedSources
    : Array.from(allowedSources);

  if (sourceList.length === 0) {
    return !requireSourceMatch;
  }
  if (!source) {
    return false;
  }
  return sourceList.includes(source);
}

/**
 * Validates a `MessageEvent` (or message-like envelope) from a sandboxed iframe.
 * Checks `event.source` authorization, discriminates the message `type`
 * (`'umwelt:send-prompt' | 'sendPrompt' | 'umwelt:resize' | 'resize'`),
 * enforces prompt non-empty & max-length bounds (`1..4000`), and clamps resize heights (`50..4000`).
 *
 * @param event - Incoming `MessageEvent` or `{ data, source }` envelope.
 * @param options - Optional source validation configuration.
 * @returns Normalized `ValidatedBridgeMessage` if valid, or `null` if rejected.
 */
export function validateBridgeMessage(
  event: { data: unknown; source?: unknown } | null | undefined,
  options: ValidateBridgeMessageOptions = {},
): ValidatedBridgeMessage | null {
  if (!event || typeof event !== 'object') {
    return null;
  }

  if (
    !isSourceAuthorized(
      event.source,
      options.allowedSources,
      options.requireSourceMatch,
    )
  ) {
    return null;
  }

  const data = event.data;
  if (!data || typeof data !== 'object') {
    return null;
  }

  const record = data as Record<string, unknown>;
  const rawType = record.type;

  if (
    rawType === 'umwelt:send-prompt' ||
    rawType === 'sendPrompt' ||
    rawType === 'opengenui:send-prompt'
  ) {
    const candidate =
      typeof record.prompt === 'string'
        ? record.prompt
        : typeof record.text === 'string'
          ? record.text
          : null;
    if (candidate === null) {
      return null;
    }
    const parsed = sendPromptParameters.safeParse({ text: candidate });
    if (!parsed.success) {
      return null;
    }
    return {
      type: 'send-prompt',
      prompt: parsed.data.text,
      rawType,
    };
  }

  if (
    rawType === 'umwelt:resize' ||
    rawType === 'resize' ||
    rawType === '__ogui_resize'
  ) {
    const rawHeight = record.height;
    if (typeof rawHeight !== 'number' || !Number.isFinite(rawHeight) || rawHeight <= 0) {
      return null;
    }
    const clampedHeight = Math.max(
      MIN_SANDBOX_HEIGHT,
      Math.min(MAX_SANDBOX_HEIGHT, Math.ceil(rawHeight)),
    );
    return {
      type: 'resize',
      height: clampedHeight,
      rawType,
    };
  }

  if (rawType === 'umwelt:open-link' || rawType === 'openLink') {
    const rawUrl = record.url;
    if (typeof rawUrl !== 'string' || !isAllowedLinkUrl(rawUrl)) {
      return null;
    }
    return {
      type: 'open-link',
      url: rawUrl,
      rawType,
    };
  }

  return null;
}
