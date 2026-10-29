'use client';

/**
 * Sandboxed Streaming Generative UI Renderer (`sandbox/renderer.tsx`).
 * Renders progressive HTML/CSS/JS widgets inside `<iframe sandbox="allow-scripts">`
 * (NEVER `allow-same-origin`), rotating placeholder status messages during streaming,
 * morphing HTML deltas via Idiomorph, deferring JS execution until `status === 'completed'`,
 * synchronizing iframe height via `ResizeObserver` `postMessage`, and exposing
 * fullscreen preview + standalone `.html` export via `ExportOverlay`.
 */

import * as React from 'react';
import { Loader2Icon } from 'lucide-react';
import { usePromptBridge, validateBridgeMessage } from '../bridge';
import { genUiCopy } from '../copy';
import type { SandboxTheme } from '../design-system';
import { ExportOverlay } from './export-overlay';
import { buildStandaloneHtmlDocument } from './export-utils';
import { buildFrameDocument, createFrameNonce } from './frame-content';
import { processPartialHtml } from './process-partial-html';
import {
  getStreamingStage,
  validateSandboxedUiArgs,
  type GenerativeUIPayload,
  type SandboxedUiArgs,
} from './schema';
import {
  enforceStrictSandboxAttribute,
  executeSandboxScripts,
  syncSandboxTheme,
  updateSandboxContent,
} from './websandbox-loader';

/** Default throttle interval in milliseconds for high-frequency streaming snapshots. */
export const THROTTLE_MS = 1000;

const MIN_HEIGHT = 50;
const MAX_HEIGHT = 4000;
const DEFAULT_INITIAL_HEIGHT = 320;

/** Default loading phrases rotated when `placeholderMessages` is not provided. */
export const LOADING_PHRASES = [
  'Sketching pixels',
  'Wiring up nodes',
  'Painting gradients',
  'Compiling visuals',
  'Arranging atoms',
  'Rendering magic',
  'Polishing edges',
] as const;

/**
 * Clamps a sandbox-reported pixel height to the safe viewport range `[50, 4000]`.
 * Returns `null` for non-finite or non-positive values so invalid reports never corrupt layout.
 *
 * @param height - Candidate height reported via `postMessage`.
 * @returns Clamped integer height in pixels, or `null` if invalid.
 */
export function clampReportedHeight(height: unknown): number | null {
  if (typeof height !== 'number' || !Number.isFinite(height) || height <= 0) {
    return null;
  }
  return Math.max(MIN_HEIGHT, Math.min(Math.ceil(height), MAX_HEIGHT));
}

/**
 * Detects the active Umwelt-OS theme (`'light'` or `'dark'`) from `document.documentElement`
 * when an explicit `theme` prop is not passed.
 *
 * @param explicitTheme - Optional theme override prop.
 * @returns Active `SandboxTheme` (`'light'` or `'dark'`).
 */
function useResolvedSandboxTheme(explicitTheme?: SandboxTheme): SandboxTheme {
  const [detectedTheme, setDetectedTheme] = React.useState<SandboxTheme>(() => {
    if (explicitTheme) return explicitTheme;
    if (typeof document !== 'undefined' && document.documentElement.classList.contains('dark')) {
      return 'dark';
    }
    return 'light';
  });

  React.useEffect(() => {
    if (explicitTheme) {
      return;
    }
    if (typeof document === 'undefined' || typeof MutationObserver === 'undefined') {
      return;
    }
    const updateFromDom = () => {
      const isDark =
        document.documentElement.classList.contains('dark') ||
        document.documentElement.getAttribute('data-theme') === 'dark';
      setDetectedTheme(isDark ? 'dark' : 'light');
    };
    const observer = new MutationObserver(updateFromDom);
    observer.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ['class', 'data-theme'],
    });
    return () => observer.disconnect();
  }, [explicitTheme]);

  return explicitTheme ?? detectedTheme;
}

/**
 * Rotates through `messages` on a fixed timer while `active` is true.
 *
 * @param active - Whether rotation is active (`status === 'streaming'`).
 * @param customMessages - Optional custom `placeholderMessages` array from tool args.
 * @param intervalMs - Rotation interval in milliseconds (defaults to 1800ms).
 * @returns Currently active placeholder message string.
 */
export function usePlaceholderRotation(
  active: boolean,
  customMessages?: string[],
  intervalMs = 1800,
): string {
  const phrases =
    customMessages && customMessages.length > 0 ? customMessages : LOADING_PHRASES;
  const [index, setIndex] = React.useState(0);

  React.useEffect(() => {
    if (!active || phrases.length <= 1) return;
    const timer = setInterval(() => {
      setIndex((prev) => (prev + 1) % phrases.length);
    }, intervalMs);
    return () => clearInterval(timer);
  }, [active, phrases.length, intervalMs]);

  return phrases[index % phrases.length] ?? LOADING_PHRASES[0];
}

export interface OpenGenerativeUIRendererProps {
  /** Sandboxed UI arguments (`initialHeight`, `placeholderMessages`, `css`, `html`, `jsFunctions`, `jsExpressions`, `title`). */
  args?: SandboxedUiArgs;
  /** Alias for `args` when passed from `GenerativeUIPayload.sandbox`. */
  sandbox?: SandboxedUiArgs;
  /** Full `GenerativeUIPayload` if passed directly. */
  payload?: GenerativeUIPayload;
  /** Direct `initialHeight` override in pixels. */
  initialHeight?: number;
  /** Direct `placeholderMessages` override. */
  placeholderMessages?: string[];
  /** Direct widget `css` override. */
  css?: string;
  /** Direct widget `html` override. */
  html?: string;
  /** Direct widget `jsFunctions` override. */
  jsFunctions?: string;
  /** Direct widget `jsExpressions` override. */
  jsExpressions?: string;
  /** Widget title used for accessibility, fullscreen modal, and `.html` export. */
  title?: string;
  /** Streaming status (`'streaming'` or `'completed'`). Defaults to `'completed'`. */
  status?: 'streaming' | 'completed';
  /** Explicit theme (`'light'` or `'dark'`). Automatically syncs with `html.dark` when omitted. */
  theme?: SandboxTheme;
  /** Interval in milliseconds for rotating `placeholderMessages` while streaming. Defaults to 1800ms. */
  placeholderIntervalMs?: number;
  /** Optional localized labels for the export/fullscreen toolbar. */
  labels?: {
    expand?: string;
    exportHtml?: string;
    copyHtml?: string;
    copiedHtml?: string;
    close?: string;
    previewDescription?: string;
  };
}

/**
 * Renders a sandboxed streaming generative UI widget inside `<iframe sandbox="allow-scripts">`.
 *
 * Security invariants:
 * - The iframe `sandbox` attribute is strictly `"allow-scripts"` and NEVER `"allow-same-origin"`.
 * - Inline `<script>` tags and external `http(s)` script/stylesheet/iframe injections are stripped
 *   from `html` via `processPartialHtml`.
 * - `jsFunctions` and `jsExpressions` are deferred and only executed when `status === 'completed'`.
 *
 * @param props - `OpenGenerativeUIRendererProps`.
 * @returns Sandboxed widget wrapped in `ExportOverlay`.
 */
export function OpenGenerativeUIRenderer({
  args,
  sandbox,
  payload,
  initialHeight,
  placeholderMessages,
  css,
  html,
  jsFunctions,
  jsExpressions,
  title,
  status: explicitStatus,
  theme: explicitTheme,
  placeholderIntervalMs = 1800,
  labels,
}: OpenGenerativeUIRendererProps) {
  const bridge = usePromptBridge();
  const iframeRef = React.useRef<HTMLIFrameElement | null>(null);
  // Execution bookkeeping for the CURRENT iframe document. All three are reset whenever the
  // iframe (re)loads or streaming restarts so a recreated sandbox is replayed from scratch.
  const frameLoadedRef = React.useRef(false);
  const functionsExecutedRef = React.useRef(false);
  const executedIndexRef = React.useRef(0);
  const [frameEpoch, setFrameEpoch] = React.useState(0);

  const mergedRawArgs = React.useMemo(() => {
    const base = args ?? sandbox ?? payload?.sandbox ?? {};
    return validateSandboxedUiArgs({
      ...base,
      ...(initialHeight !== undefined ? { initialHeight } : {}),
      ...(placeholderMessages !== undefined ? { placeholderMessages } : {}),
      ...(css !== undefined ? { css } : {}),
      ...(html !== undefined ? { html } : {}),
      ...(jsFunctions !== undefined ? { jsFunctions } : {}),
      ...(jsExpressions !== undefined ? { jsExpressions } : {}),
      ...(title !== undefined
        ? { title }
        : payload?.title !== undefined
          ? { title: payload.title }
          : {}),
    });
  }, [
    args,
    sandbox,
    payload,
    initialHeight,
    placeholderMessages,
    css,
    html,
    jsFunctions,
    jsExpressions,
    title,
  ]);

  // Per-expression list (array form preserved) so each expression executes exactly once.
  const expressionList = React.useMemo(() => {
    const base = (args ?? sandbox ?? payload?.sandbox ?? {}) as { jsExpressions?: unknown };
    const raw = jsExpressions !== undefined ? jsExpressions : base.jsExpressions;
    if (Array.isArray(raw)) {
      return raw.filter((e): e is string => typeof e === 'string' && e.trim().length > 0);
    }
    return typeof raw === 'string' && raw.trim().length > 0 ? [raw] : [];
  }, [args, sandbox, payload, jsExpressions]);

  const status: 'streaming' | 'completed' =
    explicitStatus ?? payload?.status ?? 'completed';
  const isStreaming = status === 'streaming';
  const isCompleted = status === 'completed';

  const resolvedTheme = useResolvedSandboxTheme(explicitTheme);
  const stage = getStreamingStage(mergedRawArgs, status);

  const sanitizedHtml = React.useMemo(
    () => processPartialHtml(mergedRawArgs.html ?? ''),
    [mergedRawArgs.html],
  );
  const hasHtmlContent = sanitizedHtml.trim().length > 0;

  const activePlaceholder = usePlaceholderRotation(
    isStreaming,
    mergedRawArgs.placeholderMessages,
    placeholderIntervalMs,
  );

  const [reportedHeight, setReportedHeight] = React.useState<number | null>(null);
  const effectiveHeight =
    reportedHeight ?? mergedRawArgs.initialHeight ?? DEFAULT_INITIAL_HEIGHT;
  const resolvedTitle = mergedRawArgs.title ?? genUiCopy().genUiDefaultWidgetTitle;

  // The inline iframe's srcDoc is created ONCE (empty body, no scripts). Content, theme and JS
  // reach it via postMessage so streaming deltas never reload the frame or replay a stale preview.
  // Per-frame nonce shared with the bridge registry, the fullscreen preview and host->frame posts.
  const [frameNonce] = React.useState(createFrameNonce);
  const [baseSrcDoc] = React.useState(() =>
    buildFrameDocument({
      nonce: frameNonce,
      html: '',
      css: '',
      theme: resolvedTheme,
      executeScripts: false,
      title: resolvedTitle,
    }),
  );

  // Full self-contained document for the fullscreen preview; only built once completed.
  const previewSrcDoc = React.useMemo(
    () =>
      isCompleted
        ? buildFrameDocument({
            html: mergedRawArgs.html ?? '',
            css: mergedRawArgs.css ?? '',
            jsFunctions: mergedRawArgs.jsFunctions ?? '',
            jsExpressions: mergedRawArgs.jsExpressions ?? '',
            theme: resolvedTheme,
            executeScripts: true,
            title: resolvedTitle,
            nonce: frameNonce,
          })
        : baseSrcDoc,
    [
      frameNonce,
      isCompleted,
      baseSrcDoc,
      mergedRawArgs.html,
      mergedRawArgs.css,
      mergedRawArgs.jsFunctions,
      mergedRawArgs.jsExpressions,
      resolvedTheme,
      resolvedTitle,
    ],
  );

  /**
   * Iframe `load` handler: fires on first load AND whenever the browser recreates the sandbox
   * (e.g. the element is re-parented). Resets execution state and bumps the epoch so the
   * morph/theme/JS effects replay everything into the fresh document.
   */
  const handleFrameLoad = React.useCallback(() => {
    frameLoadedRef.current = true;
    functionsExecutedRef.current = false;
    executedIndexRef.current = 0;
    setFrameEpoch((epoch) => epoch + 1);
  }, []);

  // Build standalone HTML document for .html export and fullscreen preview when completed.
  const standaloneExportHtml = React.useMemo(() => {
    if (!isCompleted || !hasHtmlContent) return undefined;
    return buildStandaloneHtmlDocument(
      {
        css: mergedRawArgs.css,
        html: sanitizedHtml,
        jsFunctions: mergedRawArgs.jsFunctions,
        jsExpressions: mergedRawArgs.jsExpressions,
        title: resolvedTitle,
      },
      resolvedTitle,
      resolvedTheme,
    );
  }, [
    isCompleted,
    hasHtmlContent,
    mergedRawArgs.css,
    sanitizedHtml,
    mergedRawArgs.jsFunctions,
    mergedRawArgs.jsExpressions,
    resolvedTitle,
    resolvedTheme,
  ]);

  // Register iframe with PromptBridge and enforce strict sandbox attribute.
  React.useEffect(() => {
    const iframe = iframeRef.current;
    if (!iframe) return;
    enforceStrictSandboxAttribute(iframe);
    const unregister = bridge.registerFrame(iframe.contentWindow, frameNonce);
    return () => {
      unregister();
    };
  }, [bridge, hasHtmlContent, frameNonce]);

  // Morph HTML and CSS deltas inside the sandboxed iframe.
  React.useEffect(() => {
    const iframe = iframeRef.current;
    if (!iframe || !hasHtmlContent) return;
    updateSandboxContent(iframe, mergedRawArgs.html ?? '', mergedRawArgs.css);
  }, [hasHtmlContent, mergedRawArgs.html, mergedRawArgs.css, frameEpoch]);

  // Synchronize active Umwelt-OS theme with the sandboxed iframe.
  React.useEffect(() => {
    const iframe = iframeRef.current;
    if (!iframe) return;
    syncSandboxTheme(iframe, resolvedTheme);
  }, [resolvedTheme, hasHtmlContent, frameEpoch]);

  // Streaming (re)start: a partial widget must never run JS, and a later completion must re-run all of it.
  React.useEffect(() => {
    if (isStreaming) {
      functionsExecutedRef.current = false;
      executedIndexRef.current = 0;
    }
  }, [isStreaming]);

  // Once completed (and the frame is loaded): run jsFunctions once, then every jsExpression not yet
  // executed in this document. Partial streaming JS is never executed.
  React.useEffect(() => {
    const iframe = iframeRef.current;
    if (!iframe || !isCompleted || !frameLoadedRef.current) return;
    if (!functionsExecutedRef.current && mergedRawArgs.jsFunctions?.trim()) {
      functionsExecutedRef.current = true;
      executeSandboxScripts(iframe, mergedRawArgs.jsFunctions, undefined);
    }
    for (let i = executedIndexRef.current; i < expressionList.length; i += 1) {
      executeSandboxScripts(iframe, undefined, expressionList[i]);
    }
    executedIndexRef.current = expressionList.length;
  }, [isCompleted, mergedRawArgs.jsFunctions, expressionList, frameEpoch]);

  // Listen for ResizeObserver height reports from this iframe only. `send-prompt` and `open-link`
  // are owned exclusively by PromptBridgeProvider so a prompt is never dispatched twice.
  React.useEffect(() => {
    const handleMessage = (event: MessageEvent) => {
      const iframe = iframeRef.current;
      if (!iframe?.contentWindow) return;
      const validated = validateBridgeMessage(event, {
        frames: new Map([[iframe.contentWindow, frameNonce]]),
      });
      if (validated?.type !== 'resize') return;
      const clamped = clampReportedHeight(validated.height);
      if (clamped !== null) {
        setReportedHeight(clamped);
      }
    };

    window.addEventListener('message', handleMessage);
    return () => {
      window.removeEventListener('message', handleMessage);
    };
  }, [frameNonce]);

  return (
    <ExportOverlay
      title={resolvedTitle}
      html={standaloneExportHtml}
      previewHtml={previewSrcDoc}
      ready={isCompleted && hasHtmlContent}
      frameNonce={frameNonce}
      labels={labels}
    >
      <div
        className="relative w-full overflow-hidden rounded-xl border border-border bg-surface transition-[height] duration-200 ease-out"
        style={{ height: `${effectiveHeight}px` }}
        data-testid="sandboxed-ui-container"
        data-stage={stage}
        data-status={status}
      >
        {isStreaming && (
          <div
            role="status"
            aria-live="polite"
            className={
              hasHtmlContent
                ? 'pointer-events-none absolute top-2 left-3 z-10 inline-flex items-center gap-1.5 rounded-md border border-border bg-surface/90 px-2.5 py-1 text-xs font-medium text-muted-foreground shadow-xs backdrop-blur-xs'
                : 'absolute inset-0 z-10 flex flex-col items-center justify-center gap-2 bg-surface/95 p-6 text-center text-sm text-muted-foreground'
            }
            data-testid="sandboxed-ui-placeholder"
          >
            <Loader2Icon className="size-4 animate-spin text-primary" />
            <span>{activePlaceholder}</span>
          </div>
        )}

        <iframe
          ref={iframeRef}
          title={resolvedTitle}
          sandbox="allow-scripts"
          srcDoc={baseSrcDoc}
          data-umwelt-nonce={frameNonce}
          onLoad={handleFrameLoad}
          className="h-full w-full border-0 bg-transparent"
          style={{
            opacity: hasHtmlContent ? 1 : 0,
            pointerEvents: hasHtmlContent ? 'auto' : 'none',
          }}
          data-testid="sandboxed-ui-iframe"
        />
        {/* Provenance cue: this widget is model-generated code running in an isolated iframe. */}
        <span
          data-testid="sandboxed-ui-badge"
          className="pointer-events-none absolute bottom-1.5 right-2 z-10 rounded-md border border-border bg-surface/90 px-1.5 py-0.5 text-[10px] font-medium text-muted-foreground"
        >
          {genUiCopy().sandboxedBadge}
        </span>
      </div>
    </ExportOverlay>
  );
}

/** Alias matching OpenIntelligentUI activity renderer naming. */
export const OpenGenUIActivityRenderer = OpenGenerativeUIRenderer;
