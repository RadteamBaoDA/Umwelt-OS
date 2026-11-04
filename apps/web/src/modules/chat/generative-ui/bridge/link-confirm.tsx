'use client';

/**
 * Host-side confirmation for widget-requested links (`bridge/link-confirm.tsx`).
 * A sandboxed widget can post `open-link` without any user gesture, so the host never opens a URL directly: it shows
 * the full address in a dialog and opens it only after the owner confirms.
 */

import * as React from 'react';
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog';

/** Localized strings for the host-side confirmation shown before a widget-requested link is opened. */
export interface LinkConfirmLabels {
  title: string;
  description: string;
  open: string;
  cancel: string;
}

/** English fallback labels used when the host does not provide localized strings. */
export const DEFAULT_LINK_CONFIRM_LABELS: LinkConfirmLabels = {
  title: 'Open external link?',
  description: 'A widget in this conversation wants to open the address below in a new tab. Only continue if you trust it.',
  open: 'Open link',
  cancel: 'Cancel',
};

/** After the owner cancels, further link requests from the same frame are ignored for this long (anti-nagging). */
export const LINK_CANCEL_COOLDOWN_MS = 60_000;

/** Controller returned by `useWidgetLinkConfirm`. */
export interface WidgetLinkConfirm {
  /** Currently pending URL, or null when no dialog is shown. */
  pendingLink: string | null;
  /**
   * Queues a widget link request. Dropped while another is pending or while the requesting frame is in its
   * post-cancel cooldown.
   */
  requestLink: (url: string, source: unknown) => void;
  /** Resolves the dialog: opens the exact confirmed URL (no opener/referrer) or just closes it. */
  resolve: (confirmed: boolean) => void;
}

/**
 * Owns the pending-link state for the confirmation dialog.
 *
 * @returns A `WidgetLinkConfirm` controller. Side effects: `resolve(true)` calls `window.open` with
 * `noopener,noreferrer`; `resolve(false)` starts a cooldown for the frame that asked.
 */
export function useWidgetLinkConfirm(): WidgetLinkConfirm {
  const [pendingLink, setPendingLink] = React.useState<string | null>(null);
  const pendingRef = React.useRef<{ url: string; source: unknown } | null>(null);
  const cancelledAtRef = React.useRef<Map<unknown, number>>(new Map());

  const requestLink = React.useCallback((url: string, source: unknown) => {
    if (pendingRef.current !== null) return;
    const cancelledAt = cancelledAtRef.current.get(source);
    if (cancelledAt !== undefined && Date.now() - cancelledAt < LINK_CANCEL_COOLDOWN_MS) return;
    pendingRef.current = { url, source };
    setPendingLink(url);
  }, []);

  const resolve = React.useCallback((confirmed: boolean) => {
    const pending = pendingRef.current;
    pendingRef.current = null;
    setPendingLink(null);
    if (!pending) return;
    if (confirmed && typeof window !== 'undefined') {
      window.open(pending.url, '_blank', 'noopener,noreferrer');
    } else if (!confirmed) {
      cancelledAtRef.current.set(pending.source, Date.now());
    }
  }, []);

  return { pendingLink, requestLink, resolve };
}

/**
 * Renders the confirmation dialog for a pending widget link, showing the full URL.
 *
 * @param props - `url` (null hides the dialog), localized `labels`, and the `onResolve` callback.
 */
export function WidgetLinkConfirmDialog({
  url,
  labels,
  onResolve,
}: {
  url: string | null;
  labels: LinkConfirmLabels;
  onResolve: (confirmed: boolean) => void;
}) {
  return (
    <AlertDialog open={url !== null} onOpenChange={(open) => { if (!open) onResolve(false); }}>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>{labels.title}</AlertDialogTitle>
          <AlertDialogDescription>{labels.description}</AlertDialogDescription>
        </AlertDialogHeader>
        <p
          data-testid="genui-link-url"
          className="break-all rounded-md border border-border bg-muted px-3 py-2 font-mono text-sm text-foreground"
        >
          {url}
        </p>
        <AlertDialogFooter>
          <AlertDialogCancel onClick={() => onResolve(false)}>{labels.cancel}</AlertDialogCancel>
          <AlertDialogAction onClick={() => onResolve(true)}>{labels.open}</AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}
