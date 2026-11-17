'use client';

import dynamic from 'next/dynamic';
import { useQuery } from '@tanstack/react-query';
import { SmartphoneIcon } from 'lucide-react';
import { useTranslations } from 'next-intl';
import { useState } from 'react';
import { Button } from '@/components/ui/button';
import { pickEnabled } from './api';

// The dialog (and the QR encoder it pulls in) loads on first open; the header button itself stays tiny.
const PickQrDialog = dynamic(() => import('./pick-qr-dialog').then((m) => m.PickQrDialog), { ssr: false });

/**
 * Header button that opens the phone-picker QR dialog. Renders nothing unless the backend reports the feature
 * enabled (PICK_ENABLED, off by default), so the control is invisible on a stock install.
 * @param csrfToken Session CSRF token for creating a pick session.
 * @param onPrompt Receives the picked prompt text once (the chat send entry).
 */
export function PickQrButton({ csrfToken, onPrompt }: { csrfToken: string; onPrompt: (prompt: string) => void }) {
  const t = useTranslations('pick');
  const [open, setOpen] = useState(false);
  // Latch: the dynamic chunk is requested on first open, and stays mounted afterwards so the close animation can play.
  const [everOpened, setEverOpened] = useState(false);
  const status = useQuery({
    queryKey: ['pick', 'status'],
    queryFn: ({ signal }) => pickEnabled(signal),
    retry: false,
    staleTime: Infinity,
  });
  if (!status.data?.enabled) return null;
  return (
    <>
      <Button
        type="button"
        variant="ghost"
        size="icon"
        aria-label={t('open')}
        title={t('open')}
        onClick={() => {
          setEverOpened(true);
          setOpen(true);
        }}
      >
        <SmartphoneIcon className="size-4" aria-hidden />
      </Button>
      {everOpened && <PickQrDialog open={open} onOpenChange={setOpen} csrfToken={csrfToken} onPrompt={onPrompt} />}
    </>
  );
}
