'use client';

import { useIsFetching } from '@tanstack/react-query';
import { usePathname } from 'next/navigation';
import { useEffect, useState } from 'react';
import { useTranslations } from 'next-intl';
import { Loader2Icon } from 'lucide-react';

/** Poll interval and number of consecutive "nothing loading" polls required before the overlay closes; lets lazy chunks and effect-started queries begin first. */
const POLL_MS = 100;
const STABLE_POLLS = 4;
/** Anything still showing a skeleton, busy state or spinner outside the overlay means the page is not finished rendering. */
const BUSY_SELECTOR = '.skeleton, [aria-busy="true"]:not([data-route-loader]), .animate-spin:not([data-route-loader] *)';
/** Hard cap after the new route arrived so a stuck or endlessly refetching query can never trap the owner behind the overlay. */
const MAX_BLOCK_MS = 10_000;
/** A click that never changes the pathname (cancelled by a leave guard, hash-only, failed navigation) releases the overlay after this. */
const NO_ARRIVAL_MS = 3_000;
/** Polling/background queries that must not hold the overlay open. */
const BACKGROUND_KEYS = new Set(['shell-api-health']);

/**
 * Full-page blocking loader for client route changes.
 *
 * Opens on a click to a same-origin link with a different pathname (before the route even resolves) or on any pathname
 * change (programmatic navigation), and closes only after the new route has arrived and no tracked react-query fetch is
 * in flight and no skeleton/spinner remains in the DOM for STABLE_POLLS consecutive polls. Also covers the initial document load.
 *
 * @returns The overlay while a route transition is loading, otherwise null.
 */
export function RouteLoader() {
  const pathname = usePathname();
  /**
   * Pathname the transition started from; null when idle. The route has arrived once `pathname` differs from it.
   * Starts as '' (already "arrived") so full document loads — first visit, refresh, and the source routes that
   * guarded navigation reloads on purpose — are blocked until their data renders too.
   */
  const [from, setFrom] = useState<string | null>('');
  const [seenPath, setSeenPath] = useState(pathname);
  const fetching = useIsFetching({ predicate: (query) => !BACKGROUND_KEYS.has(String(query.queryKey[0])) });
  const arrived = from !== null && pathname !== from;

  // Adjust state during render (not in an effect) so the overlay covers the very first frame of the new route.
  if (pathname !== seenPath) {
    setSeenPath(pathname);
    if (from === null) setFrom(seenPath);
  }

  useEffect(() => {
    /** Starts the overlay for plain left-clicks on same-origin links that lead to another pathname. */
    const onClick = (event: MouseEvent) => {
      if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
      if (!(event.target instanceof Element)) return;
      const anchor = event.target.closest<HTMLAnchorElement>('a[href]');
      if (!anchor || anchor.download || (anchor.target && anchor.target !== '_self')) return;
      const destination = new URL(anchor.href, window.location.href);
      if (destination.origin !== window.location.origin || destination.pathname === window.location.pathname) return;
      setFrom(window.location.pathname);
    };
    document.addEventListener('click', onClick, true);
    return () => document.removeEventListener('click', onClick, true);
  }, []);

  useEffect(() => {
    if (from === null) return;
    const cap = window.setTimeout(() => setFrom(null), arrived ? MAX_BLOCK_MS : NO_ARRIVAL_MS);
    return () => window.clearTimeout(cap);
  }, [from, arrived]);

  useEffect(() => {
    if (!arrived || fetching > 0) return;
    // Query state alone is not enough: lazy chunks, Suspense fallbacks and disabled queries render skeletons without fetching.
    let stable = 0;
    const poll = window.setInterval(() => {
      stable = document.querySelector(BUSY_SELECTOR) ? 0 : stable + 1;
      if (stable >= STABLE_POLLS) setFrom(null);
    }, POLL_MS);
    return () => window.clearInterval(poll);
  }, [arrived, fetching, pathname]);

  return from === null ? null : <RouteSpinner />;
}

/** Opaque full-viewport spinner; shared by the route-change overlay and the root Suspense fallback so both look identical. */
export function RouteSpinner() {
  const t = useTranslations('shell');
  return <div className="fixed inset-0 z-[100] flex items-center justify-center bg-background" role="status" aria-live="polite" aria-busy="true" data-route-loader="">
    <Loader2Icon className="size-8 animate-spin text-primary" aria-hidden="true" />
    <span className="sr-only">{t('loadingWorkspace')}</span>
  </div>;
}
