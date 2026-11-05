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
      /** Optional request id echoed back in the host's result/error reply. */
      id?: string;
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
    }
  | {
      type: 'load-image';
      /** Untrusted URL; the host re-validates it with `resolveMediaUrl` before fetching. */
      url: string;
      id: string;
      rawType: 'umwelt:load-image';
    };

export interface ValidateBridgeMessageOptions {
  /**
   * Registry of authorized sandbox frames: `iframe.contentWindow` -> per-frame nonce.
   * Fail-closed: when missing or empty every message is rejected.
   */
  frames?: ReadonlyMap<unknown, string>;
  /** Host origin accepted besides the opaque `'null'`; defaults to `window.location.origin`. */
  hostOrigin?: string;
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
 * Validates a `MessageEvent` (or message-like envelope) from a sandboxed iframe.
 * Fail-closed: requires `event.source` to be a registered sandbox window, `event.origin` to be the
 * opaque `'null'` (sandboxed srcdoc) or the host origin, and `data.nonce` to equal that frame's
 * nonce. Then discriminates the message `type`
 * (`'umwelt:send-prompt' | 'sendPrompt' | 'umwelt:resize' | 'resize'`),
 * enforces prompt non-empty & max-length bounds (`1..4000`), and clamps resize heights (`50..4000`).
 *
 * @param event - Incoming `MessageEvent` or `{ data, source }` envelope.
 * @param options - Frame registry (`frames`) and optional `hostOrigin`; no frames means rejection.
 * @returns Normalized `ValidatedBridgeMessage` if valid, or `null` if rejected.
 */
export function validateBridgeMessage(
  event: { data: unknown; source?: unknown; origin?: string } | null | undefined,
  options: ValidateBridgeMessageOptions = {},
): ValidatedBridgeMessage | null {
  const record = authenticateBridgeEvent(event, options);
  if (!record) return null;
  const rawType = record.type;
  if (rawType === 'umwelt:send-prompt' || rawType === 'sendPrompt' || rawType === 'opengenui:send-prompt') {
    return parseSendPrompt(record, rawType);
  }
  if (rawType === 'umwelt:resize' || rawType === 'resize' || rawType === '__ogui_resize') {
    return parseResize(record, rawType);
  }
  if (rawType === 'umwelt:open-link' || rawType === 'openLink') {
    return parseOpenLink(record, rawType);
  }
  if (rawType === 'umwelt:load-image') {
    return parseLoadImage(record, rawType);
  }
  return null;
}

/** The opaque `'null'` origin (sandboxed srcdoc) or the host origin; an unknown host origin rejects everything else. */
function isTrustedOrigin(origin: string | undefined, configuredHostOrigin: string | undefined): boolean {
  const hostOrigin = configuredHostOrigin ?? (typeof window !== 'undefined' ? window.location.origin : '');
  return origin === 'null' || (Boolean(hostOrigin) && origin === hostOrigin);
}

/**
 * Fail-closed frame authentication: the source must be a registered sandbox window, the origin the opaque `'null'`
 * (sandboxed srcdoc) or the host origin, and `data.nonce` must equal that frame's nonce.
 *
 * @returns The message payload as a record, or null when any check fails.
 */
function authenticateBridgeEvent(
  event: { data: unknown; source?: unknown; origin?: string } | null | undefined,
  options: ValidateBridgeMessageOptions,
): Record<string, unknown> | null {
  if (!event || typeof event !== 'object') {
    return null;
  }

  const frames = options.frames;
  if (!frames || frames.size === 0 || !event.source || !frames.has(event.source)) {
    return null;
  }
  if (!isTrustedOrigin(event.origin, options.hostOrigin)) {
    return null;
  }

  const data = event.data;
  if (!data || typeof data !== 'object') {
    return null;
  }
  const expectedNonce = frames.get(event.source);
  if (!expectedNonce || (data as Record<string, unknown>).nonce !== expectedNonce) {
    return null;
  }
  return data as Record<string, unknown>;
}

/** Raw wire `type` strings accepted for one normalized message kind. */
type BridgeRaw<K extends ValidatedBridgeMessage['type']> = Extract<ValidatedBridgeMessage, { type: K }>['rawType'];

/** Prompt request: text comes from `prompt` or `text` and must satisfy the shared send-prompt schema (1..4000 chars). */
function parseSendPrompt(record: Record<string, unknown>, rawType: BridgeRaw<'send-prompt'>): ValidatedBridgeMessage | null {
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
    id: typeof record.id === 'string' && record.id.length <= 64 ? record.id : undefined,
    rawType,
  };
}

/** Resize request: a finite positive height, clamped to the sandbox bounds. */
function parseResize(record: Record<string, unknown>, rawType: BridgeRaw<'resize'>): ValidatedBridgeMessage | null {
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

/** Open-link request: only URLs accepted by `isAllowedLinkUrl` pass (the host still asks the owner to confirm). */
function parseOpenLink(record: Record<string, unknown>, rawType: BridgeRaw<'open-link'>): ValidatedBridgeMessage | null {
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

/** Image request: a string URL (validated later by the media loader) and a non-empty id of at most 64 chars. */
function parseLoadImage(record: Record<string, unknown>, rawType: BridgeRaw<'load-image'>): ValidatedBridgeMessage | null {
  const { url, id } = record;
  if (typeof url !== 'string' || typeof id !== 'string' || !id || id.length > 64) {
    return null;
  }
  return { type: 'load-image', url, id, rawType };
}
