import * as React from 'react';
import { apiRequest } from '@/core/api';
import {
  IMAGE_RATE_MAX,
  IMAGE_RATE_WINDOW_MS,
  LOAD_IMAGE_ERROR_TYPE,
  LOAD_IMAGE_RESULT_TYPE,
  PRIVACY_CACHE_MS,
  SEND_PROMPT_ACK_TYPE,
  SEND_PROMPT_ERROR_TYPE,
  SEND_PROMPT_RESULT_TYPE,
} from './bridge-constants';
import { blobToDataUrl, fetchImageBlob, resolveMediaUrl } from './image-loader';
import { sendPromptParameters, validateBridgeMessage } from './sandbox-functions';

/**
 * Keeps a ref pointing at the latest value, updated in an effect (async callbacks read it without re-subscribing).
 *
 * @param value - Latest value.
 * @returns Stable ref whose `current` follows `value` after each commit.
 */
export function useLatest<T>(value: T): React.MutableRefObject<T> {
  const ref = React.useRef(value);
  React.useEffect(() => {
    ref.current = value;
  }, [value]);
  return ref;
}

/** Authorized sandbox frames, their per-frame image request timestamps and the host-to-frame reply channel. */
export interface FrameRegistry {
  /** `contentWindow` -> nonce of every registered sandbox frame. */
  framesRef: React.MutableRefObject<Map<unknown, string>>;
  /** Per-frame image request timestamps (rate limit). */
  frameRequestsRef: React.MutableRefObject<Map<unknown, number[]>>;
  registerFrame: (frameWindow: unknown, nonce: string) => () => void;
  replyToFrame: (target: unknown, message: Record<string, unknown>) => void;
  /** Last accepted prompt time shared by host and sandbox prompts (single cooldown). */
  lastAcceptedAtRef: React.MutableRefObject<number | null>;
}

/**
 * Owns the set of authorized sandbox frames.
 *
 * @returns The registry; `registerFrame` ignores empty windows/nonces and returns a cleanup that also drops the frame's
 * rate-limit history. `replyToFrame` posts with the frame's nonce so the in-frame listener accepts it and swallows
 * failures (a detached frame has nobody left to notify).
 */
export function useFrameRegistry(): FrameRegistry {
  const framesRef = React.useRef<Map<unknown, string>>(new Map());
  const frameRequestsRef = React.useRef<Map<unknown, number[]>>(new Map());
  const lastAcceptedAtRef = React.useRef<number | null>(null);

  const registerFrame = React.useCallback((frameWindow: unknown, nonce: string) => {
    if (!frameWindow || !nonce) {
      return () => undefined;
    }
    framesRef.current.set(frameWindow, nonce);
    return () => {
      framesRef.current.delete(frameWindow);
      frameRequestsRef.current.delete(frameWindow);
    };
  }, []);

  const replyToFrame = React.useCallback((target: unknown, message: Record<string, unknown>) => {
    const frame = target as { postMessage?: (data: unknown, origin: string) => void } | null | undefined;
    try {
      frame?.postMessage?.({ ...message, nonce: framesRef.current.get(target) }, '*');
    } catch {
      // Frame already detached; nothing left to notify.
    }
  }, []);

  return React.useMemo(
    () => ({ framesRef, frameRequestsRef, registerFrame, replyToFrame, lastAcceptedAtRef }),
    [registerFrame, replyToFrame],
  );
}

/** True when a prompt arrived inside the cooldown window of the previously accepted one. */
function isRateLimited(lastAcceptedAt: number | null, now: number, cooldownMs: number): boolean {
  return cooldownMs > 0 && lastAcceptedAt !== null && now - lastAcceptedAt < cooldownMs;
}

/** Options shared by the prompt hooks. */
interface PromptHookOptions {
  frames: FrameRegistry;
  disabledRef: React.MutableRefObject<boolean>;
  cooldownMs: number;
}

/**
 * Host-side `sendPrompt` (trusted React widgets): validates, rate-limits and forwards to `onSendPrompt`.
 *
 * Every outcome is reported to the originating iframe (when any) so `window.sendPrompt` settles: result on success,
 * retryable error on rejection (busy, cooldown, invalid).
 *
 * @returns `(rawPrompt, replyTo?, requestId?) => accepted`.
 */
export function useHostPromptDispatch({ frames, onSendPromptRef, disabledRef, cooldownMs }: PromptHookOptions & {
  onSendPromptRef: React.MutableRefObject<(prompt: string) => void | Promise<void>>;
}) {
  const { replyToFrame, lastAcceptedAtRef } = frames;
  return React.useCallback(
    (rawPrompt: string, replyTo?: unknown, requestId?: string): boolean => {
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
      if (isRateLimited(lastAcceptedAtRef.current, now, cooldownMs)) {
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
    [cooldownMs, replyToFrame, lastAcceptedAtRef, disabledRef, onSendPromptRef],
  );
}

/**
 * Handles a prompt posted by a sandboxed widget: validates and rate-limits it like any prompt, then ONLY fills the
 * chat composer through `onDraftPrompt`. It is never submitted: a widget can run script without any user gesture, so
 * the owner must read the text and press send themselves. Replies to the frame with ack + result (or a retryable error).
 *
 * @returns `(rawPrompt, replyTo, requestId?) => void`.
 */
export function useSandboxPromptDraft({ frames, onDraftPromptRef, disabledRef, cooldownMs }: PromptHookOptions & {
  onDraftPromptRef: React.MutableRefObject<((prompt: string) => void) | undefined>;
}) {
  const { replyToFrame, lastAcceptedAtRef } = frames;
  return React.useCallback(
    (rawPrompt: string, replyTo: unknown, requestId?: string): void => {
      const fail = (message: string) =>
        replyToFrame(replyTo, { type: SEND_PROMPT_ERROR_TYPE, id: requestId, retryable: true, message });
      const draft = onDraftPromptRef.current;
      if (disabledRef.current || !draft) {
        fail('Prompt bridge is disabled');
        return;
      }
      const parsed = sendPromptParameters.safeParse({ text: rawPrompt });
      if (!parsed.success) {
        fail('Invalid prompt');
        return;
      }
      const now = Date.now();
      if (isRateLimited(lastAcceptedAtRef.current, now, cooldownMs)) {
        fail('Prompt rate limited');
        return;
      }
      lastAcceptedAtRef.current = now;
      replyToFrame(replyTo, { type: SEND_PROMPT_ACK_TYPE, id: requestId });
      try {
        draft(parsed.data.text);
        replyToFrame(replyTo, { type: SEND_PROMPT_RESULT_TYPE, id: requestId, ok: true, drafted: true });
      } catch (err) {
        fail(err instanceof Error ? err.message : String(err));
      }
    },
    [cooldownMs, replyToFrame, lastAcceptedAtRef, disabledRef, onDraftPromptRef],
  );
}

/**
 * Sliding-window per-frame limiter: records this request and reports whether the frame is within `IMAGE_RATE_MAX`.
 *
 * @returns False (and keeps the pruned history without recording) when the frame is over the limit.
 */
function admitImageRequest(requests: Map<unknown, number[]>, source: unknown, now: number): boolean {
  const recent = (requests.get(source) ?? []).filter((at) => now - at < IMAGE_RATE_WINDOW_MS);
  if (recent.length >= IMAGE_RATE_MAX) {
    requests.set(source, recent);
    return false;
  }
  recent.push(now);
  requests.set(source, recent);
  return true;
}

/**
 * Whether the owner allows widget remote media. Tile-heavy maps fire bursts of concurrent requests, so one in-flight or
 * recent settings read is shared for `PRIVACY_CACHE_MS`; a failed read is evicted so the next request retries.
 */
function readRemoteMediaAllowed(cacheRef: React.MutableRefObject<{ allowed: Promise<boolean>; at: number } | null>): Promise<boolean> {
  let cached = cacheRef.current;
  if (!cached || Date.now() - cached.at > PRIVACY_CACHE_MS) {
    const allowed = apiRequest<{ privacy?: { allow_widget_remote_media?: boolean } }>(
      '/api/v1/settings/ai',
    ).then((settings) => settings.privacy?.allow_widget_remote_media === true);
    cached = { allowed, at: Date.now() };
    cacheRef.current = cached;
    const entry = cached;
    allowed.catch(() => {
      if (cacheRef.current === entry) cacheRef.current = null;
    });
  }
  return cached.allowed;
}

/**
 * Serves a widget `loadImage` request: URL-allowlisted, rate limited, gated by the owner's
 * `allow_widget_remote_media` privacy flag, fetched with credentials and size/type capped, then answered
 * with a `data:` URL (opaque-origin frames cannot reliably load host-origin `blob:` URLs; no blob lifetime to manage).
 * All failures reply with an error message and never throw.
 *
 * @param frames - Frame registry (rate-limit history and reply channel).
 * @returns `(source, id, rawUrl) => Promise<void>`.
 */
export function useWidgetImageLoader(frames: FrameRegistry) {
  const { replyToFrame, frameRequestsRef, framesRef } = frames;
  const privacyCacheRef = React.useRef<{ allowed: Promise<boolean>; at: number } | null>(null);
  return React.useCallback(
    async (source: unknown, id: string, rawUrl: string) => {
      const fail = (message: string) =>
        replyToFrame(source, { type: LOAD_IMAGE_ERROR_TYPE, id, message });
      const url = resolveMediaUrl(rawUrl, window.location.origin);
      if (!url) {
        fail('Image URL not allowed');
        return;
      }
      if (!admitImageRequest(frameRequestsRef.current, source, Date.now())) {
        fail('Image requests rate limited');
        return;
      }
      try {
        if (!(await readRemoteMediaAllowed(privacyCacheRef))) {
          fail('Remote widget media is disabled');
          return;
        }
        const dataUrl = await blobToDataUrl(await fetchImageBlob(url));
        // Frame unregistered while fetching: nobody is left to receive the reply.
        if (!framesRef.current.has(source)) {
          fail('Image not available');
          return;
        }
        replyToFrame(source, { type: LOAD_IMAGE_RESULT_TYPE, id, dataUrl });
      } catch (err) {
        fail(err instanceof Error ? err.message : 'Image request failed');
      }
    },
    [replyToFrame, frameRequestsRef, framesRef],
  );
}

/**
 * Attaches the single window `message` listener owning frame -> host messages (send-prompt, resize, load-image,
 * open-link). Unauthenticated or malformed messages are dropped by `validateBridgeMessage`.
 */
export function useBridgeMessageListener({ frames, onResizeRef, draftSandboxPrompt, handleLoadImage, requestLink }: {
  frames: FrameRegistry;
  onResizeRef: React.MutableRefObject<((height: number, source: unknown) => void) | undefined>;
  draftSandboxPrompt: (rawPrompt: string, replyTo: unknown, requestId?: string) => void;
  handleLoadImage: (source: unknown, id: string, rawUrl: string) => Promise<void>;
  requestLink: (url: string, source: unknown) => void;
}): void {
  const { framesRef } = frames;
  React.useEffect(() => {
    const handleWindowMessage = (event: MessageEvent) => {
      const validated = validateBridgeMessage(event, { frames: framesRef.current });

      if (!validated) {
        return;
      }

      if (validated.type === 'send-prompt') {
        draftSandboxPrompt(validated.prompt, event.source, validated.id);
      } else if (validated.type === 'resize') {
        onResizeRef.current?.(validated.height, event.source);
      } else if (validated.type === 'load-image') {
        void handleLoadImage(event.source, validated.id, validated.url);
      } else if (validated.type === 'open-link') {
        // Never open directly: a widget can fire this without any user gesture. Ask the owner first.
        requestLink(validated.url, event.source);
      }
    };

    window.addEventListener('message', handleWindowMessage);
    return () => {
      window.removeEventListener('message', handleWindowMessage);
    };
  }, [draftSandboxPrompt, handleLoadImage, requestLink, framesRef, onResizeRef]);
}
