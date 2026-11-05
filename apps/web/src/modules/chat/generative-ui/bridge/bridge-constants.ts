// Wire message types and limits shared by the prompt bridge provider and its hooks.

/** Message type posted back to a widget iframe when the host rejects its `sendPrompt` (retryable). */
export const SEND_PROMPT_ERROR_TYPE = 'umwelt:send-prompt-error';

/** Message type posted immediately once the host accepted a `sendPrompt` for processing (stops the in-frame ack timeout). */
export const SEND_PROMPT_ACK_TYPE = 'umwelt:send-prompt-ack';

/** Message type posted back to a widget iframe when the host accepted and handled its `sendPrompt`. */
export const SEND_PROMPT_RESULT_TYPE = 'umwelt:send-prompt-result';

/** Message type posted to a widget with the loaded image's `blob:` URL. */
export const LOAD_IMAGE_RESULT_TYPE = 'umwelt:load-image-result';

/** Message type posted to a widget when an image request is refused or fails. */
export const LOAD_IMAGE_ERROR_TYPE = 'umwelt:load-image-error';

/** Sliding window length for per-frame image request limiting. */
export const IMAGE_RATE_WINDOW_MS = 10_000;

/** Maximum image requests per frame inside `IMAGE_RATE_WINDOW_MS`. */
export const IMAGE_RATE_MAX = 60;

/** How long the owner's remote-media privacy flag is reused before re-reading settings. */
export const PRIVACY_CACHE_MS = 30_000;
