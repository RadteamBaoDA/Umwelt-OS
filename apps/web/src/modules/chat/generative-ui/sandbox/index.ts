/**
 * Public exports for the Sandboxed Streaming UI engine (`sandbox/index.ts`).
 */

export {
  CANONICAL_STREAM_KEYS,
  OPEN_GENERATIVE_UI_ACTIVITY_TYPE,
  SandboxedUiArgsSchema,
  validateSandboxedUiArgs,
  getStreamingStage,
  type CanonicalStreamKey,
  type SandboxedUiArgs,
  type GenerativeUIKind,
  type GenerativeUIPayload,
  type StreamingStage,
} from './schema';

export {
  extractCompleteStyles,
  balanceUnclosedTags,
  processPartialHtml,
  type ProcessPartialHtmlOptions,
} from './process-partial-html';

export {
  IDIOMORPH_INLINE_SCRIPT,
  IDIOMORPH_JS,
} from './idiomorph-inline';

export {
  CSP_META_TAG,
  buildCspMetaTag,
  MEASUREMENT_JS,
  PREVIEW_FRAME_CONTENT,
  RESIZE_MESSAGE_TYPE,
  LEGACY_RESIZE_MESSAGE_TYPE,
  SANDBOX_BRIDGE_JS,
  ensureHead,
  escapeScriptTagContent,
  escapeStyleTagContent,
  buildPreviewBodyMorph,
  buildPreviewHeadContent,
  buildFinalFrameContent,
  buildFrameDocument,
  assembleFrameHtml,
  type FrameDocumentOptions,
} from './frame-content';

export {
  enforceStrictSandboxAttribute,
  updateSandboxContent,
  executeSandboxScripts,
  syncSandboxTheme,
  createSandboxController,
  type SandboxController,
} from './websandbox-loader';

export {
  createTripAnimator,
  TripAnimator,
  TRIP_ANIMATOR_SCRIPT,
  type TripFrame,
  type TripAnimatorState,
  type TripAnimationOptions,
  type TripAnimatorController,
} from './trip-animator';

export {
  buildStandaloneHtmlDocument,
  assembleStandaloneHtmlFromActivity,
  downloadHtmlDocument,
  triggerDownload,
  slugify,
  escapeHtml,
  escapeScriptClose,
  escapeStyleClose,
  type StandaloneActivityContent,
} from './export-utils';

export {
  ExportOverlay,
  type ExportOverlayProps,
} from './export-overlay';

export {
  OpenGenerativeUIRenderer,
  OpenGenUIActivityRenderer,
  clampReportedHeight,
  usePlaceholderRotation,
  LOADING_PHRASES,
  THROTTLE_MS,
  type OpenGenerativeUIRendererProps,
} from './renderer';
