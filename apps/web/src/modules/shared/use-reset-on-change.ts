'use client';

import { useState } from 'react';

/**
 * Runs `reset` during render whenever any value in `deps` differs (by `Object.is`) from the previous render,
 * and once on the first render, replacing the `useEffect(() => setState(...), deps)` reset-on-change pattern.
 *
 * React re-renders the component immediately after a render-phase state update, before children render or the
 * browser paints, so consumers never see a frame with stale derived state (the effect version painted it first).
 * `reset` may only call this component's own state setters; it must be pure otherwise (no refs, no network).
 *
 * @param deps Values whose change should trigger the reset; their count must stay stable between renders.
 * @param reset Applies the state updates for the new deps; reads the latest render's closure.
 */
export function useResetOnChange(deps: readonly unknown[], reset: () => void): void {
  const [previous, setPrevious] = useState<readonly unknown[] | null>(null);
  if (previous === null || deps.some((dep, index) => !Object.is(dep, previous[index]))) {
    setPrevious(deps);
    reset();
  }
}
