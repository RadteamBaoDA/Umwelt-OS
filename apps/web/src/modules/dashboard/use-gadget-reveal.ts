'use client';

import { useEffect, useRef, type RefObject } from 'react';

/**
 * How long the reveal mark stays on the body. It must cover the longest staggered CSS reveal
 * (the 360ms type-in plus up to 14 child steps of 45ms), after which the mark is removed.
 */
const REVEAL_MS = 1500;

/**
 * Reports whether the user asked for reduced motion. Hosts without `matchMedia` (jsdom, old engines)
 * are treated as "no preference", so the reveal still runs there.
 *
 * @returns True only when `prefers-reduced-motion: reduce` currently matches.
 */
function prefersReducedMotion(): boolean {
  return typeof window !== 'undefined' && typeof window.matchMedia === 'function'
    ? window.matchMedia('(prefers-reduced-motion: reduce)').matches
    : false;
}

/**
 * Re-triggers the gadget data-refresh reveal by toggling a `data-reveal` attribute on the element.
 * The CSS in gadget-skin.css animates the renderer content while the attribute is present. It only
 * changes presentation; the element is never remounted and React state is untouched.
 *
 * Behaviour:
 * - A null key does nothing (no data has arrived yet).
 * - The first non-null key is the initial data arrival and does not animate: the gadget just appears.
 * - Any later change of a non-null key restarts the reveal, so only refreshes animate.
 * - Under `prefers-reduced-motion: reduce` nothing is marked, so no animation runs.
 * - The mark is removed after REVEAL_MS, and on cleanup (key change or unmount).
 *
 * @param ref Element that receives the `data-reveal` attribute; read on each effect run, never stored.
 * @param key Freshness marker, typically the renderer-reported collectedAt timestamp, or null while unknown.
 */
export function useGadgetReveal(ref: RefObject<HTMLElement | null>, key: string | null): void {
  // Set once the first non-null key has been seen. It is never reset, so a later null key does not make
  // the next data arrival look like the first one.
  const hasSeenKey = useRef(false);
  useEffect(() => {
    if (key === null) return;
    const isFirstArrival = !hasSeenKey.current;
    hasSeenKey.current = true;
    const el = ref.current;
    if (isFirstArrival || !el || prefersReducedMotion()) return;
    el.removeAttribute('data-reveal');
    // Reading layout forces a style recalc, so removing and re-adding the attribute restarts the CSS
    // animation even when the previous reveal is still running.
    el.getBoundingClientRect();
    el.setAttribute('data-reveal', 'true');
    const timer = window.setTimeout(() => el.removeAttribute('data-reveal'), REVEAL_MS);
    return () => {
      window.clearTimeout(timer);
      el.removeAttribute('data-reveal');
    };
  }, [ref, key]);
}
