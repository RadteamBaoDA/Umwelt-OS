'use client';

import * as React from 'react';

/**
 * Smooths bursty streamed text: the returned string trails `target` and catches up one animation frame at a time, so
 * large network chunks render as a steady flow instead of jumps. The catch-up rate grows with the backlog so the display
 * never lags far behind, and reduced motion (or a target that is not an extension of the shown text) renders immediately.
 *
 * @param target - Latest full streamed text.
 * @returns The portion of `target` to display now.
 */
export function useSmoothedText(target: string): string {
  const [shown, setShown] = React.useState(target);
  const shownRef = React.useRef(target);

  React.useEffect(() => {
    if (shownRef.current === target) return;
    const reduced = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
    // A reset or edit (not an append) must never animate stale text.
    if (reduced || !target.startsWith(shownRef.current)) {
      shownRef.current = target;
      setShown(target);
      return;
    }
    let frame = 0;
    /** Reveals a backlog-proportional slice (at least 2 chars) per frame, snapping to the end on word boundaries. */
    const tick = () => {
      const backlog = target.length - shownRef.current.length;
      if (backlog <= 0) return;
      let next = shownRef.current.length + Math.max(2, Math.ceil(backlog / 8));
      const space = target.indexOf(' ', next);
      if (space !== -1 && space - next < 6) next = space;
      shownRef.current = target.slice(0, Math.min(next, target.length));
      setShown(shownRef.current);
      if (shownRef.current.length < target.length) frame = requestAnimationFrame(tick);
    };
    frame = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frame);
  }, [target]);

  return shown;
}
