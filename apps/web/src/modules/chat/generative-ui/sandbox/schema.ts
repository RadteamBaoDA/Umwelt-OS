import { z } from 'zod';

/**
 * Canonical streaming key order for `generate_sandboxed_ui` tool arguments.
 * Ordering ensures initialHeight and placeholderMessages arrive first, followed by
 * CSS rules, progressive HTML markup, and finally JavaScript functions/expressions.
 */
export const CANONICAL_STREAM_KEYS = [
  'initialHeight',
  'placeholderMessages',
  'css',
  'html',
  'jsFunctions',
  'jsExpressions',
] as const;

export type CanonicalStreamKey = (typeof CANONICAL_STREAM_KEYS)[number];

/** Activity identifier compatible with OpenIntelligentUI activity streams. */
export const OPEN_GENERATIVE_UI_ACTIVITY_TYPE = 'open-generative-ui';

/**
 * Arguments emitted by `generate_sandboxed_ui` for progressive iframe rendering.
 */
export interface SandboxedUiArgs {
  initialHeight?: number;
  placeholderMessages?: string[];
  css?: string;
  html?: string;
  jsFunctions?: string;
  jsExpressions?: string;
  title?: string;
}

/** Supported generative UI widget kinds across Pillar 1 (sandboxed) and Pillar 2 (curated). */
export type GenerativeUIKind =
  | 'sandboxed_ui'
  | 'plan'
  | 'table'
  | 'bar_chart'
  | 'pie_chart'
  | 'meeting_time_picker';

/**
 * Unified generative UI payload attached to streaming SSE events and persisted chat messages.
 */
export interface GenerativeUIPayload {
  kind: GenerativeUIKind;
  status?: 'streaming' | 'completed';
  visualization?: string;
  title?: string;
  sandbox?: SandboxedUiArgs;
  plan?: {
    title?: string;
    description?: string;
    approach?: string;
    technology?: string;
    keyElements?: string[];
    key_elements?: string[];
    steps: Array<{
      id: string;
      label?: string;
      title?: string;
      emoji?: string;
      description?: string;
      status?: 'pending' | 'in_progress' | 'completed';
    }>;
    todos?: Array<Record<string, unknown>>;
  };
  table?: {
    title?: string;
    description?: string;
    subtitle?: string;
    source?: string;
    is_sample?: boolean;
    columns: Array<{
      key: string;
      label: string;
      type?: 'text' | 'number' | 'currency' | 'percent' | 'badge';
      format?: 'text' | 'number' | 'currency' | 'percent' | 'badge';
      currency?: string;
      align?: 'left' | 'right' | 'center';
    }>;
    rows?: Array<Record<string, unknown>>;
    records?: Array<Record<string, unknown>>;
  };
  chart?: {
    title?: string;
    description?: string;
    subtitle?: string;
    data: Array<Record<string, unknown>>;
    categoryKey?: string;
    x_key?: string;
    series?: Array<{
      dataKey?: string;
      key?: string;
      name?: string;
      label?: string;
      color?: string;
      color_token?: string;
      unit?: string;
      currency?: string;
    }>;
    unit?: string;
    currency?: string;
    source?: string;
    is_sample?: boolean;
  };
  form?: {
    title?: string;
    description?: string;
    subtitle?: string;
    durationMinutes?: number;
    duration_minutes?: number;
    timezone?: string;
    selected_slot_id?: string | null;
    slots: Array<{
      id: string;
      date: string;
      time?: string;
      start_time?: string;
      end_time?: string;
      label?: string;
      available?: boolean;
      recommended?: boolean;
    }>;
  };
}

/** Progressive streaming lifecycle stage derived from canonical key arrival. */
export type StreamingStage = 'placeholder' | 'css' | 'html' | 'js' | 'completed';

/** Zod schema validating raw or partial `SandboxedUiArgs` payloads. */
export const SandboxedUiArgsSchema = z.object({
  initialHeight: z.number().finite().min(50).max(4000).optional(),
  placeholderMessages: z.array(z.string()).optional(),
  css: z.string().optional(),
  html: z.union([z.string(), z.array(z.string())]).optional(),
  jsFunctions: z.string().optional(),
  jsExpressions: z.union([z.string(), z.array(z.string())]).optional(),
  title: z.string().optional(),
});

/**
 * Normalizes and validates raw `SandboxedUiArgs` input from an SSE delta or persisted payload.
 * Coerces string-array `html` or `jsExpressions` chunks into strings and clamps `initialHeight`
 * to safe viewport bounds (`[50, 4000]`).
 *
 * @param input - Untrusted or partial sandbox arguments object.
 * @returns Sanitized `SandboxedUiArgs` object.
 */
export function validateSandboxedUiArgs(input: unknown): SandboxedUiArgs {
  if (!input || typeof input !== 'object') {
    return {};
  }
  const raw = input as Record<string, unknown>;
  const normalized: SandboxedUiArgs = {};

  if (typeof raw.initialHeight === 'number' && Number.isFinite(raw.initialHeight)) {
    normalized.initialHeight = Math.max(50, Math.min(4000, Math.round(raw.initialHeight)));
  }

  if (Array.isArray(raw.placeholderMessages)) {
    const messages = raw.placeholderMessages
      .filter((item): item is string => typeof item === 'string' && item.trim().length > 0)
      .map((item) => item.trim());
    if (messages.length > 0) {
      normalized.placeholderMessages = messages;
    }
  }

  if (typeof raw.css === 'string') {
    normalized.css = raw.css;
  }

  if (typeof raw.html === 'string') {
    normalized.html = raw.html;
  } else if (Array.isArray(raw.html)) {
    normalized.html = raw.html.filter((chunk): chunk is string => typeof chunk === 'string').join('');
  }

  if (typeof raw.jsFunctions === 'string') {
    normalized.jsFunctions = raw.jsFunctions;
  }

  if (typeof raw.jsExpressions === 'string') {
    normalized.jsExpressions = raw.jsExpressions;
  } else if (Array.isArray(raw.jsExpressions)) {
    normalized.jsExpressions = raw.jsExpressions
      .filter((expr): expr is string => typeof expr === 'string')
      .join('\n');
  }

  if (typeof raw.title === 'string' && raw.title.trim().length > 0) {
    normalized.title = raw.title.trim();
  }

  return normalized;
}

/**
 * Determines the current progressive rendering stage based on which canonical stream keys
 * have arrived and whether the tool call has completed.
 *
 * @param args - Current partial or completed `SandboxedUiArgs`.
 * @param status - Stream lifecycle status (`'streaming'` or `'completed'`).
 * @returns Active `StreamingStage` (`'placeholder'`, `'css'`, `'html'`, `'js'`, or `'completed'`).
 */
export function getStreamingStage(
  args?: SandboxedUiArgs | null,
  status: 'streaming' | 'completed' = 'streaming',
): StreamingStage {
  if (status === 'completed') {
    return 'completed';
  }
  if (!args) {
    return 'placeholder';
  }
  if (
    (typeof args.jsExpressions === 'string' && args.jsExpressions.trim().length > 0) ||
    (typeof args.jsFunctions === 'string' && args.jsFunctions.trim().length > 0)
  ) {
    return 'js';
  }
  if (typeof args.html === 'string' && args.html.trim().length > 0) {
    return 'html';
  }
  if (typeof args.css === 'string' && args.css.trim().length > 0) {
    return 'css';
  }
  return 'placeholder';
}
