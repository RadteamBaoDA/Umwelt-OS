'use client';

/**
 * Fullscreen preview dialog and standalone `.html` export toolbar (`sandbox/export-overlay.tsx`).
 * Composes Umwelt-OS shadcn/ui `Dialog` and `Button` primitives using semantic design tokens.
 */

import * as React from 'react';
import { CheckIcon, CopyIcon, DownloadIcon, Maximize2Icon } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { usePromptBridge } from '../bridge';
import { genUiCopy } from '../copy';
import { downloadHtmlDocument, slugify } from './export-utils';
import { frameDocumentProps } from './frame-content';

export interface ExportOverlayProps {
  /** Human-readable title used for the dialog heading and `.html` download filename. */
  title?: string;
  /** Standalone HTML document string for export and fullscreen preview. */
  html?: string;
  /** Frame document HTML (loaded via the frame shell) used for fullscreen preview if different from `html`. */
  previewHtml?: string;
  /** Nonce embedded in `previewHtml`; the fullscreen frame is registered with the bridge under it. */
  frameNonce?: string;
  /** Whether the widget has finished streaming and is ready for export/fullscreen. */
  ready?: boolean;
  /** Optional controlled fullscreen dialog open state. */
  open?: boolean;
  /** Optional callback when fullscreen dialog open state changes. */
  onOpenChange?: (open: boolean) => void;
  /** Localized or custom labels for accessibility and i18n. */
  labels?: {
    expand?: string;
    exportHtml?: string;
    copyHtml?: string;
    copiedHtml?: string;
    close?: string;
    previewDescription?: string;
  };
  /** Wrapped sandboxed widget frame. */
  children?: React.ReactNode;
}

/** Resolved (localized) labels of the overlay. */
type OverlayLabels = Record<'expand' | 'exportHtml' | 'copyHtml' | 'copiedHtml' | 'close' | 'previewDescription', string>;

/**
 * Resolves overlay labels: explicit props win, fallbacks come from the shared catalog (document lang) so no English
 * literals live here.
 */
function resolveOverlayLabels(labels: ExportOverlayProps['labels']): OverlayLabels {
  const fallback = genUiCopy();
  return {
    expand: labels?.expand ?? fallback.genUiFullscreenPreview,
    exportHtml: labels?.exportHtml ?? fallback.genUiExportHtml,
    copyHtml: labels?.copyHtml ?? fallback.genUiCopyHtml,
    copiedHtml: labels?.copiedHtml ?? fallback.genUiCopied,
    close: labels?.close ?? fallback.genUiClose,
    previewDescription: labels?.previewDescription ?? fallback.genUiPreviewDescription,
  };
}

/**
 * Dialog open state that works controlled (`open` given) or uncontrolled; the change callback fires in both modes.
 *
 * @returns `[isOpen, setOpen]`.
 */
function useDialogOpen(
  controlledOpen: boolean | undefined,
  onOpenChange: ((open: boolean) => void) | undefined,
): [boolean, (open: boolean) => void] {
  const [uncontrolledOpen, setUncontrolledOpen] = React.useState(false);
  const setDialogOpen = React.useCallback(
    (nextOpen: boolean) => {
      if (controlledOpen === undefined) {
        setUncontrolledOpen(nextOpen);
      }
      onOpenChange?.(nextOpen);
    },
    [controlledOpen, onOpenChange],
  );
  return [controlledOpen ?? uncontrolledOpen, setDialogOpen];
}

/**
 * Download and clipboard actions for the standalone HTML.
 *
 * @returns The transient copy state (`copied` for 1.8s) and the two handlers. Clipboard permission errors are ignored in
 * restricted contexts.
 */
function useExportActions(html: string | undefined, title: string) {
  const [copyState, setCopyState] = React.useState<'idle' | 'copied'>('idle');
  /** Downloads the standalone HTML as a slugified `.html` file; no-op without HTML. */
  const handleDownload = React.useCallback(() => {
    if (!html) return;
    const filename = `${slugify(title) || 'generated-widget'}.html`;
    downloadHtmlDocument(html, filename);
  }, [html, title]);

  /** Copies the HTML to the clipboard and flashes `copied` for 1.8s; permission errors are ignored. */
  const handleCopy = React.useCallback(() => {
    if (!html || typeof navigator === 'undefined' || !navigator.clipboard) return;
    navigator.clipboard.writeText(html).then(
      () => {
        setCopyState('copied');
        setTimeout(() => setCopyState('idle'), 1800);
      },
      () => {
        // Ignore clipboard permission errors in restricted contexts.
      },
    );
  }, [html]);
  return { copyState, handleDownload, handleCopy };
}

/**
 * Registers the fullscreen frame's window (a new document per load) with the prompt bridge under its nonce so its
 * prompts/resizes validate; unregisters when the dialog closes (iframe unmounts) or the overlay unmounts.
 *
 * @returns The iframe `onLoad` handler.
 */
function usePreviewFrameRegistration(frameNonce: string | undefined, isDialogOpen: boolean) {
  const bridge = usePromptBridge();
  const unregisterPreviewRef = React.useRef<(() => void) | null>(null);
  /** Re-registers the freshly loaded preview document's window with the bridge (a new window per load). */
  const handlePreviewLoad = React.useCallback(
    (event: React.SyntheticEvent<HTMLIFrameElement>) => {
      unregisterPreviewRef.current?.();
      unregisterPreviewRef.current = frameNonce ? bridge.registerFrame(event.currentTarget.contentWindow, frameNonce) : null;
    },
    [bridge, frameNonce],
  );
  React.useEffect(() => {
    if (isDialogOpen) return;
    unregisterPreviewRef.current?.();
    unregisterPreviewRef.current = null;
  }, [isDialogOpen]);
  React.useEffect(() => () => unregisterPreviewRef.current?.(), []);
  return handlePreviewLoad;
}

/** Copy icon that flips to a check while the copied state is active. */
function CopyStateIcon({ copied }: { copied: boolean }) {
  return copied ? <CheckIcon className="size-3.5 text-primary" /> : <CopyIcon className="size-3.5" />;
}

/** Hover/focus toolbar over the widget: fullscreen, copy HTML and download. */
function ExportToolbar({
  labels,
  copied,
  onExpand,
  onCopy,
  onDownload,
}: {
  labels: OverlayLabels;
  copied: boolean;
  onExpand: () => void;
  onCopy: () => void;
  onDownload: () => void;
}) {
  const copyText = copied ? labels.copiedHtml : labels.copyHtml;
  return (
    <div
      className=" absolute top-2 right-2 z-10 flex items-center gap-1.5 rounded-md border border-border bg-surface/95 p-1 opacity-90 backdrop-blur-xs transition-opacity group-hover:opacity-100 focus-within:opacity-100"
      data-testid="generative-ui-toolbar"
    >
      <Button
        type="button"
        variant="ghost"
        size="sm"
        className="min-h-9 px-2.5 py-1 text-xs"
        onClick={onExpand}
        aria-label={labels.expand}
        title={labels.expand}
        data-testid="generative-ui-fullscreen-btn"
      >
        <Maximize2Icon className="size-3.5" />
        <span className="sr-only sm:not-sr-only">{labels.expand}</span>
      </Button>
      <Button
        type="button"
        variant="ghost"
        size="sm"
        className="min-h-9 px-2.5 py-1 text-xs"
        onClick={onCopy}
        aria-label={copyText}
        title={copyText}
        data-testid="generative-ui-copy-btn"
      >
        {copied ? <CheckIcon className="size-3.5 text-primary" /> : <CopyIcon className="size-3.5" />}
        <span className="sr-only">{copyText}</span>
      </Button>
      <Button
        type="button"
        variant="outline"
        size="sm"
        className="min-h-9 px-2.5 py-1 text-xs"
        onClick={onDownload}
        aria-label={labels.exportHtml}
        title={labels.exportHtml}
        data-testid="generative-ui-export-btn"
      >
        <DownloadIcon className="size-3.5" />
        <span className="sr-only sm:not-sr-only">{labels.exportHtml}</span>
      </Button>
    </div>
  );
}

/** Props of {@link FullscreenDialog}. */
interface FullscreenDialogProps {
  title: string;
  labels: OverlayLabels;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  exportable: boolean;
  copied: boolean;
  onCopy: () => void;
  onDownload: () => void;
  previewHtml: string;
  frameNonce?: string;
  onPreviewLoad: (event: React.SyntheticEvent<HTMLIFrameElement>) => void;
}

/**
 * Cheap non-cryptographic string hash (djb2) used only as a React `key` so the preview iframe remounts when its HTML changes.
 *
 * @param value - String to hash.
 * @returns Base-36 hash combined with the length (collisions only skip a remount of identical-length documents).
 */
function hashString(value: string): string {
  let h = 5381;
  for (let i = 0; i < value.length; i += 1) h = ((h << 5) + h + value.charCodeAt(i)) | 0;
  return `${value.length}-${(h >>> 0).toString(36)}`;
}

/** Fullscreen preview dialog hosting a strictly sandboxed `<iframe sandbox="allow-scripts">`. */
function FullscreenDialog(props: FullscreenDialogProps) {
  const { title, labels, open, onOpenChange, exportable, copied, onCopy, onDownload, previewHtml, frameNonce, onPreviewLoad } = props;
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        closeLabel={labels.close}
        className="flex h-[85dvh] max-w-[min(96vw,1120px)] flex-col gap-3 p-4 sm:max-w-[min(94vw,1120px)]"
        data-testid="generative-ui-fullscreen-dialog"
      >
        <DialogHeader className="flex flex-row items-center justify-between gap-3 border-b border-border pr-8 pb-3">
          <div className="min-w-0 flex-1 text-left">
            <DialogTitle className="truncate text-base font-semibold text-foreground">{title}</DialogTitle>
            <DialogDescription className="sr-only">{labels.previewDescription}</DialogDescription>
          </div>
          {exportable && (
            <div className="flex items-center gap-2">
              <Button type="button" variant="outline" size="sm" onClick={onCopy} data-testid="generative-ui-modal-copy-btn">
                <CopyStateIcon copied={copied} />
                <span>{copied ? labels.copiedHtml : labels.copyHtml}</span>
              </Button>
              <Button type="button" variant="default" size="sm" onClick={onDownload} data-testid="generative-ui-modal-export-btn">
                <DownloadIcon className="size-3.5" />
                <span>{labels.exportHtml}</span>
              </Button>
            </div>
          )}
        </DialogHeader>
        <div className="relative min-h-0 flex-1 overflow-hidden rounded-lg border border-border bg-background">
          {open && previewHtml && (
            <iframe
              // The document travels in `name`, which does not navigate on change: remount when the HTML changes.
              key={hashString(previewHtml)}
              title={`${title} fullscreen preview`}
              sandbox="allow-scripts"
              {...frameDocumentProps(previewHtml)}
              onLoad={onPreviewLoad}
              data-umwelt-nonce={frameNonce}
              className="h-full w-full border-0 bg-transparent"
              data-testid="generative-ui-fullscreen-iframe"
            />
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}

/**
 * Wraps a generative UI widget with hover/focus action controls for fullscreen preview
 * and standalone `.html` file export. Fullscreen preview renders inside a strictly
 * sandboxed `<iframe sandbox="allow-scripts">` hosted in the shared `Dialog` primitive.
 *
 * @param props - `ExportOverlayProps` configuring title, standalone HTML, readiness, and children.
 * @returns Wrapped widget container and fullscreen `Dialog` portal.
 */
export function ExportOverlay({
  title = 'generated-widget',
  html,
  previewHtml,
  ready = true,
  frameNonce,
  open: controlledOpen,
  onOpenChange,
  labels,
  children,
}: ExportOverlayProps) {
  const [isDialogOpen, setDialogOpen] = useDialogOpen(controlledOpen, onOpenChange);
  const resolvedLabels = resolveOverlayLabels(labels);
  const exportable = ready && Boolean(html);
  const { copyState, handleDownload, handleCopy } = useExportActions(html, title);
  const handlePreviewLoad = usePreviewFrameRegistration(frameNonce, isDialogOpen);
  const copied = copyState === 'copied';

  return (
    <div className="group relative w-full" data-testid="generative-ui-export-overlay">
      {exportable && (
        <ExportToolbar
          labels={resolvedLabels}
          copied={copied}
          onExpand={() => setDialogOpen(true)}
          onCopy={handleCopy}
          onDownload={handleDownload}
        />
      )}

      {children}

      <FullscreenDialog
        title={title}
        labels={resolvedLabels}
        open={isDialogOpen}
        onOpenChange={setDialogOpen}
        exportable={exportable}
        copied={copied}
        onCopy={handleCopy}
        onDownload={handleDownload}
        previewHtml={previewHtml ?? html ?? ''}
        frameNonce={frameNonce}
        onPreviewLoad={handlePreviewLoad}
      />
    </div>
  );
}
