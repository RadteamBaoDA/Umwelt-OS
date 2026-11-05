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
import { buildFrameDocument, createFrameNonce, frameDocumentProps } from './frame-content';
import { processPartialHtml } from './process-partial-html';
import { getStreamingStage, validateSandboxedUiArgs, type GenerativeUIPayload, type SandboxedUiArgs } from './schema';
import { enforceStrictSandboxAttribute, executeSandboxScripts, syncSandboxTheme, updateSandboxContent } from './websandbox-loader';

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
      const isDark = document.documentElement.classList.contains('dark') || document.documentElement.getAttribute('data-theme') === 'dark';
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
export function usePlaceholderRotation(active: boolean, customMessages?: string[], intervalMs = 1800): string {
  const phrases = customMessages && customMessages.length > 0 ? customMessages : LOADING_PHRASES;
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

/** Merged, validated widget arguments plus the per-expression list. */
type MergedArgs = ReturnType<typeof validateSandboxedUiArgs>;

/**
 * Merges the widget source (`args` > `sandbox` > `payload.sandbox`) with direct prop overrides and validates it.
 * `expressionList` keeps the array form (non-blank strings) so each expression executes exactly once.
 */
function useMergedSandboxArgs(
  p: Pick<
    OpenGenerativeUIRendererProps,
    'args' | 'sandbox' | 'payload' | 'initialHeight' | 'placeholderMessages' | 'css' | 'html' | 'jsFunctions' | 'jsExpressions' | 'title'
  >,
): { mergedRawArgs: MergedArgs; expressionList: string[] } {
  const { args, sandbox, payload, initialHeight, placeholderMessages, css, html, jsFunctions, jsExpressions, title } = p;
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
      ...(title !== undefined ? { title } : payload?.title !== undefined ? { title: payload.title } : {}),
    });
  }, [args, sandbox, payload, initialHeight, placeholderMessages, css, html, jsFunctions, jsExpressions, title]);

  const expressionList = React.useMemo(() => {
    const base = (args ?? sandbox ?? payload?.sandbox ?? {}) as { jsExpressions?: unknown };
    const raw = jsExpressions !== undefined ? jsExpressions : base.jsExpressions;
    if (Array.isArray(raw)) {
      return raw.filter((e): e is string => typeof e === 'string' && e.trim().length > 0);
    }
    return typeof raw === 'string' && raw.trim().length > 0 ? [raw] : [];
  }, [args, sandbox, payload, jsExpressions]);
  return { mergedRawArgs, expressionList };
}

/** Inputs of {@link useSandboxFrameSync}. */
interface FrameSyncArgs {
  iframeRef: React.RefObject<HTMLIFrameElement | null>;
  frameNonce: string;
  merged: MergedArgs;
  expressionList: string[];
  hasHtmlContent: boolean;
  isStreaming: boolean;
  isCompleted: boolean;
  theme: SandboxTheme;
}

/**
 * Keeps the single long-lived sandbox iframe in sync: registers it with the prompt bridge (enforcing the strict
 * sandbox attribute), morphs HTML/CSS deltas, mirrors the theme and runs the widget JS exactly once after completion.
 *
 * Execution bookkeeping is per iframe document and reset whenever the frame (re)loads or streaming restarts, so a
 * recreated sandbox is replayed from scratch and partial streaming JS never runs.
 *
 * @returns The iframe `onLoad` handler (bumps the epoch that re-triggers the sync effects).
 */
function useSandboxFrameSync({
  iframeRef,
  frameNonce,
  merged,
  expressionList,
  hasHtmlContent,
  isStreaming,
  isCompleted,
  theme,
}: FrameSyncArgs): () => void {
  const bridge = usePromptBridge();
  const frameLoadedRef = React.useRef(false);
  const functionsExecutedRef = React.useRef(false);
  const executedIndexRef = React.useRef(0);
  const [frameEpoch, setFrameEpoch] = React.useState(0);

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

  // Register iframe with PromptBridge and enforce strict sandbox attribute.
  React.useEffect(() => {
    const iframe = iframeRef.current;
    if (!iframe) return;
    enforceStrictSandboxAttribute(iframe);
    const unregister = bridge.registerFrame(iframe.contentWindow, frameNonce);
    return () => {
      unregister();
    };
  }, [bridge, hasHtmlContent, frameNonce, iframeRef]);

  // Morph HTML and CSS deltas inside the sandboxed iframe.
  React.useEffect(() => {
    const iframe = iframeRef.current;
    if (!iframe || !hasHtmlContent) return;
    updateSandboxContent(iframe, merged.html ?? '', merged.css);
  }, [hasHtmlContent, merged.html, merged.css, frameEpoch, iframeRef]);

  // Synchronize active Umwelt-OS theme with the sandboxed iframe.
  React.useEffect(() => {
    const iframe = iframeRef.current;
    if (!iframe) return;
    syncSandboxTheme(iframe, theme);
  }, [theme, hasHtmlContent, frameEpoch, iframeRef]);

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
    if (!functionsExecutedRef.current && merged.jsFunctions?.trim()) {
      functionsExecutedRef.current = true;
      executeSandboxScripts(iframe, merged.jsFunctions, undefined);
    }
    for (let i = executedIndexRef.current; i < expressionList.length; i += 1) {
      executeSandboxScripts(iframe, undefined, expressionList[i]);
    }
    executedIndexRef.current = expressionList.length;
  }, [isCompleted, merged.jsFunctions, expressionList, frameEpoch, iframeRef]);

  return handleFrameLoad;
}

/**
 * Listens for ResizeObserver height reports from this iframe only. `send-prompt` and `open-link` are owned exclusively
 * by PromptBridgeProvider so a prompt is never dispatched twice.
 *
 * @returns The last valid (clamped) reported height, or null before any report.
 */
function useReportedFrameHeight(iframeRef: React.RefObject<HTMLIFrameElement | null>, frameNonce: string): number | null {
  const [reportedHeight, setReportedHeight] = React.useState<number | null>(null);
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
  }, [frameNonce, iframeRef]);
  return reportedHeight;
}

/**
 * Frame documents: the inline iframe's document (loaded via `frameDocumentProps`) is created ONCE (empty body, no scripts); content, theme and JS reach
 * it via postMessage so streaming deltas never reload the frame or replay a stale preview. The fullscreen preview
 * gets the full self-contained document only once completed.
 *
 * @returns Per-frame nonce (shared with the bridge registry, the fullscreen preview and host->frame posts) and docs.
 */
function useSandboxDocuments(merged: MergedArgs, isCompleted: boolean, theme: SandboxTheme, title: string) {
  const [frameNonce] = React.useState(createFrameNonce);
  const [baseSrcDoc] = React.useState(() =>
    buildFrameDocument({ nonce: frameNonce, html: '', css: '', theme, executeScripts: false, title }),
  );
  const previewSrcDoc = React.useMemo(
    () =>
      isCompleted
        ? buildFrameDocument({
            html: merged.html ?? '',
            css: merged.css ?? '',
            jsFunctions: merged.jsFunctions ?? '',
            jsExpressions: merged.jsExpressions ?? '',
            theme,
            executeScripts: true,
            title,
            nonce: frameNonce,
          })
        : baseSrcDoc,
    [frameNonce, isCompleted, baseSrcDoc, merged.html, merged.css, merged.jsFunctions, merged.jsExpressions, theme, title],
  );
  return { frameNonce, baseSrcDoc, previewSrcDoc };
}

/** Streaming overlay: a corner chip once content exists, otherwise a full cover with the rotating phrase. */
function StreamingPlaceholder({ hasHtmlContent, message }: { hasHtmlContent: boolean; message: string }) {
  return (
    <div
      role="status"
      aria-live="polite"
      className={
        hasHtmlContent
          ? 'pointer-events-none absolute top-2 left-3 z-10 inline-flex items-center gap-1.5 rounded-md border border-border bg-surface/90 px-2.5 py-1 text-xs font-medium text-muted-foreground backdrop-blur-xs'
          : 'absolute inset-0 z-10 flex flex-col items-center justify-center gap-2 bg-surface/95 p-6 text-center text-sm text-muted-foreground'
      }
      data-testid="sandboxed-ui-placeholder"
    >
      <Loader2Icon className="size-4 animate-spin text-primary" />
      <span>{message}</span>
    </div>
  );
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
export function OpenGenerativeUIRenderer(props: OpenGenerativeUIRendererProps) {
  const { payload, status: explicitStatus, theme: explicitTheme, placeholderIntervalMs = 1800, labels } = props;
  const iframeRef = React.useRef<HTMLIFrameElement | null>(null);
  const { mergedRawArgs, expressionList } = useMergedSandboxArgs(props);

  const status: 'streaming' | 'completed' = explicitStatus ?? payload?.status ?? 'completed';
  const isStreaming = status === 'streaming';
  const isCompleted = status === 'completed';

  const resolvedTheme = useResolvedSandboxTheme(explicitTheme);
  const stage = getStreamingStage(mergedRawArgs, status);
  const sanitizedHtml = React.useMemo(() => processPartialHtml(mergedRawArgs.html ?? ''), [mergedRawArgs.html]);
  const hasHtmlContent = sanitizedHtml.trim().length > 0;
  const activePlaceholder = usePlaceholderRotation(isStreaming, mergedRawArgs.placeholderMessages, placeholderIntervalMs);
  const resolvedTitle = mergedRawArgs.title ?? genUiCopy().genUiDefaultWidgetTitle;
  const { frameNonce, baseSrcDoc, previewSrcDoc } = useSandboxDocuments(mergedRawArgs, isCompleted, resolvedTheme, resolvedTitle);
  const handleFrameLoad = useSandboxFrameSync({
    iframeRef,
    frameNonce,
    merged: mergedRawArgs,
    expressionList,
    hasHtmlContent,
    isStreaming,
    isCompleted,
    theme: resolvedTheme,
  });
  const reportedHeight = useReportedFrameHeight(iframeRef, frameNonce);
  const effectiveHeight = reportedHeight ?? mergedRawArgs.initialHeight ?? DEFAULT_INITIAL_HEIGHT;

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
        className="relative w-full overflow-hidden rounded-lg border border-border bg-surface transition-[height] duration-200 ease-out"
        style={{ height: `${effectiveHeight}px` }}
        data-testid="sandboxed-ui-container"
        data-stage={stage}
        data-status={status}
      >
        {isStreaming && <StreamingPlaceholder hasHtmlContent={hasHtmlContent} message={activePlaceholder} />}

        <iframe
          ref={iframeRef}
          title={resolvedTitle}
          sandbox="allow-scripts"
          {...frameDocumentProps(baseSrcDoc)}
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
          className="pointer-events-none absolute bottom-1.5 right-2 z-10 rounded-md border border-border bg-surface/90 px-1.5 py-0.5 text-[11px] font-medium text-muted-foreground"
        >
          {genUiCopy().sandboxedBadge}
        </span>
      </div>
    </ExportOverlay>
  );
}

/** Alias matching OpenIntelligentUI activity renderer naming. */
export const OpenGenUIActivityRenderer = OpenGenerativeUIRenderer;
