'use client';

import { useTranslations } from 'next-intl';
import { useEffect, useRef, useState } from 'react';
import { Button } from '@/components/ui/button';
import { ApiError } from '@/core/api';
import { scanPickSession, submitPick, type PickOption } from './api';

type Phase = 'loading' | 'options' | 'done' | 'invalid' | 'error' | 'tooMany';

/** Maps a failed phone call to a neutral phase; server text is never shown. */
function phaseFor(error: unknown): Phase {
  if (error instanceof ApiError) {
    if (error.status === 404) return 'invalid';
    if (error.status === 429) return 'tooMany';
  }
  return 'error';
}

/**
 * Phone page body: reads the session id from the URL, marks it scanned, shows the 3 server-chosen options and
 * submits only the chosen index. Anonymous by design; the unguessable id is the sole credential.
 */
export function PickPhone() {
  const t = useTranslations('pick');
  const [phase, setPhase] = useState<Phase>('loading');
  const [options, setOptions] = useState<PickOption[]>([]);
  const [sessionId, setSessionId] = useState('');

  // The scan burns the session server-side (GETDEL), so it must run once per id: StrictMode re-runs the
  // effect, and the ref (kept across that simulated remount) lets run 2 reuse run 1's request.
  const scanRef = useRef<{ id: string; request: ReturnType<typeof scanPickSession> } | null>(null);

  useEffect(() => {
    const id = new URLSearchParams(window.location.search).get('session') ?? '';
    let cancelled = false;
    // A missing id is treated like an unknown session; no request is sent for it.
    if (!scanRef.current || scanRef.current.id !== id) {
      scanRef.current = { id, request: id ? scanPickSession(id) : Promise.reject(new ApiError(404, 'missing')) };
    }
    scanRef.current.request.then((result) => {
      if (cancelled) return;
      setSessionId(id);
      setOptions(result.options);
      setPhase('options');
    }).catch((error: unknown) => { if (!cancelled) setPhase(phaseFor(error)); });
    return () => { cancelled = true; };
  }, []);

  /** Sends the chosen option pool index. */
  const choose = (index: number) => {
    submitPick(sessionId, index).then(() => setPhase('done')).catch((error: unknown) => setPhase(phaseFor(error)));
  };

  const message = { loading: t('phoneLoading'), done: t('phoneDone'), invalid: t('phoneInvalid'), error: t('phoneError'), tooMany: t('phoneTooMany'), options: '' }[phase];
  return (
    <main className="mx-auto flex min-h-dvh max-w-sm flex-col justify-center gap-4 p-6">
      <h1 className="text-xl font-semibold">{t('phoneTitle')}</h1>
      {phase === 'options' ? options.map((option) => (
        <Button key={option.index} type="button" variant="outline" className="h-14 text-base" onClick={() => choose(option.index)}>
          {option.label}
        </Button>
      )) : <p role="status" className="text-sm text-muted-foreground">{message}</p>}
    </main>
  );
}
