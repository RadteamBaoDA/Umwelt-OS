/**
 * Public exports for the Sandboxed Streaming UI engine (`sandbox/index.ts`).
 * Only symbols used outside this folder (and by its tests) are re-exported; internals import from their own files.
 */

export {
  CANONICAL_STREAM_KEYS,
  SandboxedUiArgsSchema,
  validateSandboxedUiArgs,
  getStreamingStage,
  type CanonicalStreamKey,
  type SandboxedUiArgs,
  type GenerativeUIKind,
  type GenerativeUIPayload,
  type StreamingStage,
} from './schema';

export { OpenGenerativeUIRenderer, type OpenGenerativeUIRendererProps } from './renderer';
