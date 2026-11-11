'use client';

import * as React from 'react';

/**
 * Returns a function with a permanent identity that always calls the latest `fn`, so memoized children keep their props
 * across parent renders. `undefined` stays `undefined`, so optional-handler checks (for example a hidden action) still work.
 *
 * @param fn - The latest handler; it is read at call time (never during render).
 * @returns A stable wrapper, or `undefined` when `fn` is undefined.
 */
export function useStableCallback<A extends unknown[], R>(fn: ((...args: A) => R) | undefined): ((...args: A) => R) | undefined {
  const ref = React.useRef(fn);
  // Layout phase so the ref is current before any event handler or child effect can fire after this commit.
  React.useLayoutEffect(() => {
    ref.current = fn;
  });
  const stable = React.useCallback((...args: A) => (ref.current as (...a: A) => R)(...args), []);
  return fn ? stable : undefined;
}
