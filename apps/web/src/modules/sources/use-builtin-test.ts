'use client';

import { useEffect, useRef, useState } from 'react';
import { ApiError } from '@/core/api';
import { collectAndWait, waitForActivation } from './use-source-actions';

/** Phase of the post-enable verification fetch for a built-in source. */
export type TestState = { phase: 'idle' | 'activating' | 'collecting' | 'succeeded' | 'no_changes' | 'failed'; code?: string | null; runId?: string | null };

/**
 * State machine for the built-in wizard's "prove a fetch" step.
 * `runTest(sourceId)` aborts any previous run, waits for activation, runs one collection and records the outcome;
 * a superseded or aborted run never writes state. `onSettled` is called after a completed (non-failed-activation)
 * collection so the caller can refresh caches. The in-flight run is aborted on unmount; `abort()` cancels it manually.
 */
export function useBuiltinTest(csrfToken: string, onSettled: () => void) {
  const [test, setTest] = useState<TestState>({ phase: 'idle' });
  const controller = useRef<AbortController | null>(null);
  useEffect(() => () => controller.current?.abort(), []);

  /** Waits for activation, runs one real collection and records the outcome; failures keep the dialog open for retry or discard. */
  async function runTest(sourceId: string) {
    controller.current?.abort();
    const ctl = new AbortController();
    controller.current = ctl;
    /** True while this test run is still the active one (not superseded by a newer run and not aborted). */
    const current = () => controller.current === ctl && !ctl.signal.aborted;
    setTest({ phase: 'activating' });
    try {
      const activation = await waitForActivation(sourceId, ctl.signal);
      if (!activation || !current()) return;
      if (!activation.ok) { setTest({ phase: 'failed', code: activation.code }); return; }
      setTest({ phase: 'collecting' });
      const final = await collectAndWait(sourceId, csrfToken, { signal: ctl.signal });
      if (!final || !current()) return;
      if (final.status === 'succeeded' || final.status === 'no_changes') setTest({ phase: final.status, runId: final.ingestion_run_id });
      else setTest({ phase: 'failed', code: final.error_code ?? final.status });
      onSettled();
    } catch (cause) {
      if (current()) setTest({ phase: 'failed', code: cause instanceof ApiError ? cause.message || `http_${cause.status}` : 'request_failed' });
    }
  }

  /** Cancels the in-flight verification run, if any. */
  const abort = () => controller.current?.abort();
  return { test, runTest, abort, verified: test.phase === 'succeeded' || test.phase === 'no_changes', testing: test.phase === 'activating' || test.phase === 'collecting' };
}
