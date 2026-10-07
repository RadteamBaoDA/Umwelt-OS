'use client';

import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useState, type FormEvent } from 'react';
import { useTranslations } from 'next-intl';
import { Button } from '@/components/ui/button';
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { ApiError, apiRequest, csrfHeaders } from '@/core/api';
import { apiFailureKey } from '@/core/api-failure-key';

type ChangePasswordDialogProps = {
  open: boolean;
  csrfToken: string;
  closeLabel: string;
  onOpenChange: (open: boolean) => void;
  onCloseAutoFocus?: (event: Event) => void;
};

/** Changes the owner password; the server revokes every other session and rotates this one. */
export function ChangePasswordDialog({ open, csrfToken, closeLabel, onOpenChange, onCloseAutoFocus }: ChangePasswordDialogProps) {
  const t = useTranslations('account');
  const queryClient = useQueryClient();
  const [current, setCurrent] = useState('');
  const [next, setNext] = useState('');
  const [confirm, setConfirm] = useState('');
  const change = useMutation({
    mutationFn: () => apiRequest<{ csrfToken: string }>('/api/v1/auth/password', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...csrfHeaders(csrfToken) },
      body: JSON.stringify({ currentPassword: current, newPassword: next }),
    }),
    onSuccess: (state) => {
      queryClient.setQueryData<Record<string, unknown>>(['session'], (old) => (old ? { ...old, csrfToken: state.csrfToken } : old));
      setCurrent(''); setNext(''); setConfirm('');
    },
  });
  const tooShort = next.length > 0 && next.length < 12;
  const mismatch = confirm.length > 0 && confirm !== next;
  const ready = current !== '' && next.length >= 12 && confirm === next;
  const failure = change.error instanceof ApiError && change.error.status === 429
    ? 'passwordTooMany'
    : change.error instanceof ApiError && change.error.status === 403 && change.error.message === 'Password is incorrect'
      ? 'passwordIncorrect'
      : change.error ? (apiFailureKey(change.error) ?? 'requestFailed') : null;
  const submit = (event: FormEvent) => { event.preventDefault(); if (ready && !change.isPending) change.mutate(); };
  const handleOpenChange = (value: boolean) => { if (!value) { change.reset(); setCurrent(''); setNext(''); setConfirm(''); } onOpenChange(value); };

  return <Dialog open={open} onOpenChange={handleOpenChange}>
    <DialogContent closeLabel={closeLabel} onCloseAutoFocus={onCloseAutoFocus}>
      <DialogHeader><DialogTitle>{t('changePassword')}</DialogTitle><DialogDescription>{t('changePasswordHelp')}</DialogDescription></DialogHeader>
      <form onSubmit={submit} className="grid gap-3">
        <div className="field"><Label htmlFor="cp-current">{t('currentPassword')}</Label><Input id="cp-current" type="password" autoComplete="current-password" value={current} onChange={(event) => setCurrent(event.target.value)} /></div>
        <div className="field"><Label htmlFor="cp-new">{t('newPassword')}</Label><Input id="cp-new" type="password" autoComplete="new-password" aria-invalid={tooShort} aria-describedby="cp-new-help" value={next} onChange={(event) => setNext(event.target.value)} /><span id="cp-new-help" aria-live="polite" className={tooShort ? 'error' : 'muted'}>{t('newPasswordHelp')}</span></div>
        <div className="field"><Label htmlFor="cp-confirm">{t('confirmNewPassword')}</Label><Input id="cp-confirm" type="password" autoComplete="new-password" aria-invalid={mismatch} aria-describedby={mismatch ? 'cp-confirm-error' : undefined} value={confirm} onChange={(event) => setConfirm(event.target.value)} />{mismatch && <span id="cp-confirm-error" className="error" role="alert">{t('passwordMismatch')}</span>}</div>
        {failure && <p className="error" role="alert">{t(failure)}</p>}
        {change.isSuccess && <p className="muted" role="status">{t('passwordChanged')}</p>}
        <DialogFooter><Button type="submit" disabled={!ready || change.isPending}>{change.isPending ? t('changingPassword') : t('changePassword')}</Button></DialogFooter>
      </form>
    </DialogContent>
  </Dialog>;
}
