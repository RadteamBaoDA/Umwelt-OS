'use client';

import { useQuery } from '@tanstack/react-query';
import { SmartphoneIcon } from 'lucide-react';
import { useTranslations } from 'next-intl';
import { QRCodeSVG } from 'qrcode.react';
import { useEffect, useRef, useState } from 'react';
import { Button } from '@/components/ui/button';
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { createPickSession, pickEnabled, pollPickSession, type PickCreated } from './api';

const POLL_MS = 1500;

/**
 * Builds the URL the phone opens. A loopback host is useless on a phone, so it is swapped for the server LAN IPv4.
 * @param origin The desktop page origin.
 * @param sessionId 128-bit session id (travels only in the query string).
 * @param lanIp Host LAN IPv4 from the authenticated create call, if any.
 */
export function buildPickUrl(origin: string, sessionId: string, lanIp: string | null): string {
  const url = new URL('/pick', origin);
  if (lanIp && ['localhost', '127.0.0.1', '[::1]'].includes(url.hostname)) url.hostname = lanIp;
  url.searchParams.set('session', sessionId);
  return url.toString();
}

/**
 * Session body, mounted only while the dialog is open so each opening starts a fresh session with fresh state:
 * creates a session, polls until the phone picks, hands the prompt to `onPrompt` once. Polling stops on pick,
 * expiry/404, close, or unmount.
 */
function PickQrSession({ csrfToken, onPrompt, onClose }: { csrfToken: string; onPrompt: (prompt: string) => void; onClose: () => void }) {
  const t = useTranslations('pick');
  const [created, setCreated] = useState<PickCreated | null>(null);
  const [state, setState] = useState<'waiting' | 'scanned' | 'picked' | 'expired' | 'error'>('waiting');
  // Callbacks live in refs so a parent re-render never restarts the session (that would burn the QR code).
  const promptRef = useRef(onPrompt);
  const closeRef = useRef(onClose);
  useEffect(() => { promptRef.current = onPrompt; closeRef.current = onClose; });

  useEffect(() => {
    const abort = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    /** Polls once, then reschedules itself unless the session reached a terminal state. */
    const tick = async (id: string) => {
      try {
        const result = await pollPickSession(id, abort.signal);
        if (abort.signal.aborted) return;
        setState(result.state);
        if (result.state === 'picked' && result.prompt) {
          promptRef.current(result.prompt);
          closeRef.current();
          return;
        }
      } catch {
        if (!abort.signal.aborted) setState('expired');
        return;
      }
      timer = setTimeout(() => void tick(id), POLL_MS);
    };
    createPickSession(csrfToken).then((session) => {
      if (abort.signal.aborted) return;
      setCreated(session);
      void tick(session.session_id);
    }).catch(() => { if (!abort.signal.aborted) setState('error'); });
    return () => { abort.abort(); if (timer) clearTimeout(timer); };
  }, [csrfToken]);

  const statusText = { waiting: t('waiting'), scanned: t('scanned'), picked: t('picked'), expired: t('expired'), error: t('startError') }[state];
  return (
    <div className="flex flex-col items-center gap-3">
      {created && state !== 'expired' ? (
        // QR codes need a white quiet zone to scan reliably in both themes; this is the one fixed-color surface.
        <div className="rounded-lg bg-white p-3" data-testid="pick-qr">
          <QRCodeSVG value={buildPickUrl(window.location.origin, created.session_id, created.lan_ip)} size={200} />
        </div>
      ) : null}
      {created && !created.lan_ip ? <p className="text-xs text-muted-foreground">{t('noLan')}</p> : null}
      <p role="status" className="text-sm text-muted-foreground">{statusText}</p>
    </div>
  );
}

/** Modal shell around {@link PickQrSession}; Radix unmounts the content on close, ending the poll. */
function PickQrDialog({ open, onOpenChange, csrfToken, onPrompt }: {
  open: boolean; onOpenChange: (open: boolean) => void; csrfToken: string; onPrompt: (prompt: string) => void;
}) {
  const t = useTranslations('pick');
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-sm">
        <DialogHeader>
          <DialogTitle>{t('title')}</DialogTitle>
          <DialogDescription>{t('description')}</DialogDescription>
        </DialogHeader>
        <PickQrSession csrfToken={csrfToken} onPrompt={onPrompt} onClose={() => onOpenChange(false)} />
      </DialogContent>
    </Dialog>
  );
}

/**
 * Header button that opens the phone-picker QR dialog. Renders nothing unless the backend reports the feature
 * enabled (PICK_ENABLED, off by default), so the control is invisible on a stock install.
 * @param csrfToken Session CSRF token for creating a pick session.
 * @param onPrompt Receives the picked prompt text once (the chat send entry).
 */
export function PickQrButton({ csrfToken, onPrompt }: { csrfToken: string; onPrompt: (prompt: string) => void }) {
  const t = useTranslations('pick');
  const [open, setOpen] = useState(false);
  const status = useQuery({ queryKey: ['pick', 'status'], queryFn: ({ signal }) => pickEnabled(signal), retry: false, staleTime: Infinity });
  if (!status.data?.enabled) return null;
  return (
    <>
      <Button type="button" variant="ghost" size="icon" aria-label={t('open')} title={t('open')} onClick={() => setOpen(true)}>
        <SmartphoneIcon className="size-4" aria-hidden />
      </Button>
      <PickQrDialog open={open} onOpenChange={setOpen} csrfToken={csrfToken} onPrompt={onPrompt} />
    </>
  );
}
