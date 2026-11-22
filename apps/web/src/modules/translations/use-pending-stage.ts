'use client';

import { useEffect, useState } from 'react';

/** Milliseconds a pending translation shows the plain "Translating…" label before the first reassurance. */
export const STILL_TRANSLATING_MS = 8_000;
/** Milliseconds after which a still-pending translation is described as waiting in the background queue. */
export const WAITING_IN_QUEUE_MS = 30_000;

/** 0 = translating, 1 = still translating, 2 = waiting in queue. */
export type PendingStage = 0 | 1 | 2;

/**
 * Staged reassurance for one pending translation request. Two one-shot timers advance the stage at 8 s and
 * 30 s; they are created only while `pending` is true and cleared when it flips, which also resets the stage
 * to 0. No interval runs, so the button re-renders at most twice per pending run.
 *
 * @param pending True while this single translation request is in flight.
 * @returns The current stage index; always 0 while not pending.
 */
export function usePendingStage(pending: boolean): PendingStage {
  const [stage, setStage] = useState<PendingStage>(0);
  useEffect(() => {
    if (!pending) return;
    const first = setTimeout(() => setStage(1), STILL_TRANSLATING_MS);
    const second = setTimeout(() => setStage(2), WAITING_IN_QUEUE_MS);
    return () => {
      clearTimeout(first);
      clearTimeout(second);
      // Reset when the run ends (cleanup, not the effect body) so the next pending run starts at stage 0.
      setStage(0);
    };
  }, [pending]);
  return pending ? stage : 0;
}
