'use client';

/**
 * Fullscreen preview dialog and standalone `.html` export toolbar (`sandbox/export-overlay.tsx`).
 * Composes Umwelt-OS shadcn/ui `Dialog` and `Button` primitives using semantic design tokens.
 */

import * as React from 'react';
import { CheckIcon, CopyIcon, DownloadIcon, Maximize2Icon } from 'lucide-react';
import { Button } from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { usePromptBridge } from '../bridge';
import { genUiCopy } from '../copy';
import { downloadHtmlDocument, slugify } from './export-utils';

export interface ExportOverlayProps {
  /** Human-readable title used for the dialog heading and `.html` download filename. */
  title?: string;
  /** Standalone HTML document string for export and fullscreen preview. */
  html?: string;
  /** Frame HTML (`srcdoc`) used for fullscreen preview if different from `html`. */
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
  const [uncontrolledOpen, setUncontrolledOpen] = React.useState(false);
  const [copyState, setCopyState] = React.useState<'idle' | 'copied'>('idle');

  const isDialogOpen = controlledOpen ?? uncontrolledOpen;
  const setDialogOpen = React.useCallback(
    (nextOpen: boolean) => {
      if (controlledOpen === undefined) {
        setUncontrolledOpen(nextOpen);
      }
      onOpenChange?.(nextOpen);
    },
    [controlledOpen, onOpenChange],
  );

  // Fallbacks come from the shared catalog (document lang) so no English literals live here.
  const fallback = genUiCopy();
  const expandLabel = labels?.expand ?? fallback.genUiFullscreenPreview;
  const exportLabel = labels?.exportHtml ?? fallback.genUiExportHtml;
  const copyLabel = labels?.copyHtml ?? fallback.genUiCopyHtml;
  const copiedLabel = labels?.copiedHtml ?? fallback.genUiCopied;
  const closeLabel = labels?.close ?? fallback.genUiClose;
  const previewDescription = labels?.previewDescription ?? fallback.genUiPreviewDescription;

  const exportable = ready && Boolean(html);
  const effectivePreviewHtml = previewHtml ?? html ?? '';

  const bridge = usePromptBridge();
  const unregisterPreviewRef = React.useRef<(() => void) | null>(null);

  /** Registers the fullscreen frame's window (new document per load) so its prompts/resizes validate. */
  const handlePreviewLoad = React.useCallback(
    (event: React.SyntheticEvent<HTMLIFrameElement>) => {
      unregisterPreviewRef.current?.();
      unregisterPreviewRef.current = frameNonce
        ? bridge.registerFrame(event.currentTarget.contentWindow, frameNonce)
        : null;
    },
    [bridge, frameNonce],
  );

  // Unregister when the dialog closes (iframe unmounts) or the overlay unmounts.
  React.useEffect(() => {
    if (isDialogOpen) return;
    unregisterPreviewRef.current?.();
    unregisterPreviewRef.current = null;
  }, [isDialogOpen]);
  React.useEffect(() => () => unregisterPreviewRef.current?.(), []);

  const handleDownload = React.useCallback(() => {
    if (!html) return;
    const filename = `${slugify(title) || 'generated-widget'}.html`;
    downloadHtmlDocument(html, filename);
  }, [html, title]);

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

  return (
    <div className="group relative w-full" data-testid="generative-ui-export-overlay">
      {exportable && (
        <div
          className=" absolute top-2 right-2 z-10 flex items-center gap-1.5 rounded-md border border-border bg-surface/95 p-1 opacity-90 shadow-sm backdrop-blur-xs transition-opacity group-hover:opacity-100 focus-within:opacity-100"
          data-testid="generative-ui-toolbar"
        >
          <Button
            type="button"
            variant="ghost"
            size="sm"
            className="min-h-9 px-2.5 py-1 text-xs"
            onClick={() => setDialogOpen(true)}
            aria-label={expandLabel}
            title={expandLabel}
            data-testid="generative-ui-fullscreen-btn"
          >
            <Maximize2Icon className="size-3.5" />
            <span className="sr-only sm:not-sr-only">{expandLabel}</span>
          </Button>
          <Button
            type="button"
            variant="ghost"
            size="sm"
            className="min-h-9 px-2.5 py-1 text-xs"
            onClick={handleCopy}
            aria-label={copyState === 'copied' ? copiedLabel : copyLabel}
            title={copyState === 'copied' ? copiedLabel : copyLabel}
            data-testid="generative-ui-copy-btn"
          >
            {copyState === 'copied' ? (
              <CheckIcon className="size-3.5 text-primary" />
            ) : (
              <CopyIcon className="size-3.5" />
            )}
            <span className="sr-only">{copyState === 'copied' ? copiedLabel : copyLabel}</span>
          </Button>
          <Button
            type="button"
            variant="outline"
            size="sm"
            className="min-h-9 px-2.5 py-1 text-xs"
            onClick={handleDownload}
            aria-label={exportLabel}
            title={exportLabel}
            data-testid="generative-ui-export-btn"
          >
            <DownloadIcon className="size-3.5" />
            <span className="sr-only sm:not-sr-only">{exportLabel}</span>
          </Button>
        </div>
      )}

      {children}

      <Dialog open={isDialogOpen} onOpenChange={setDialogOpen}>
        <DialogContent
          closeLabel={closeLabel}
          className="flex h-[85dvh] max-w-[min(96vw,1120px)] flex-col gap-3 p-4 sm:max-w-[min(94vw,1120px)]"
          data-testid="generative-ui-fullscreen-dialog"
        >
          <DialogHeader className="flex flex-row items-center justify-between gap-3 border-b border-border pr-8 pb-3">
            <div className="min-w-0 flex-1 text-left">
              <DialogTitle className="truncate text-base font-semibold text-foreground">
                {title}
              </DialogTitle>
              <DialogDescription className="sr-only">
                {previewDescription}
              </DialogDescription>
            </div>
            {exportable && (
              <div className="flex items-center gap-2">
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  onClick={handleCopy}
                  data-testid="generative-ui-modal-copy-btn"
                >
                  {copyState === 'copied' ? (
                    <CheckIcon className="size-3.5 text-primary" />
                  ) : (
                    <CopyIcon className="size-3.5" />
                  )}
                  <span>{copyState === 'copied' ? copiedLabel : copyLabel}</span>
                </Button>
                <Button
                  type="button"
                  variant="default"
                  size="sm"
                  onClick={handleDownload}
                  data-testid="generative-ui-modal-export-btn"
                >
                  <DownloadIcon className="size-3.5" />
                  <span>{exportLabel}</span>
                </Button>
              </div>
            )}
          </DialogHeader>
          <div className="relative min-h-0 flex-1 overflow-hidden rounded-lg border border-border bg-background">
            {isDialogOpen && effectivePreviewHtml && (
              <iframe
                title={`${title} fullscreen preview`}
                sandbox="allow-scripts"
                srcDoc={effectivePreviewHtml}
                onLoad={handlePreviewLoad}
                data-umwelt-nonce={frameNonce}
                className="h-full w-full border-0 bg-transparent"
                data-testid="generative-ui-fullscreen-iframe"
              />
            )}
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
}
