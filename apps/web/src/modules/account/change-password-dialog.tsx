'use client';

import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useState, type FormEvent } from 'react';
import { useTranslations } from 'next-intl';
import { Button } from '@/components/ui/button';
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { PasswordInput } from '@/components/ui/password-input';
import { Label } from '@/components/ui/label';
import { ApiError, apiRequest, csrfHeaders } from '@/core/api';
import { apiFailureKey } from '@/core/api-failure-key';
import { SESSION_KEY } from '@/core/session';

type ChangePasswordDialogProps = {
  open: boolean;
  csrfToken: string;
  closeLabel: string;
  onOpenChange: (open: boolean) => void;
  onCloseAutoFocus?: (event: Event) => void;
};

/**
 * Password form state and the change request. Clears the fields after success; `reset` also clears the mutation.
 * @param csrfToken CSRF token for the POST; on success the rotated token replaces the cached session's token.
 * @returns Field values/setters, the mutation, validation flags and the failure message key.
 */
function useChangePassword(csrfToken: string) {
  const queryClient = useQueryClient();
  const [current, setCurrent] = useState('');
  const [next, setNext] = useState('');
  const [confirm, setConfirm] = useState('');
  const clear = () => {
    setCurrent('');
    setNext('');
    setConfirm('');
  };
  const change = useMutation({
    mutationFn: () =>
      apiRequest<{ csrfToken: string }>('/api/v1/auth/password', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...csrfHeaders(csrfToken) },
        body: JSON.stringify({ currentPassword: current, newPassword: next }),
      }),
    onSuccess: (state) => {
      queryClient.setQueryData<Record<string, unknown>>(SESSION_KEY, (old) => (old ? { ...old, csrfToken: state.csrfToken } : old));
      clear();
    },
  });
  const failure =
    change.error instanceof ApiError && change.error.status === 429
      ? 'passwordTooMany'
      : change.error instanceof ApiError && change.error.code === 'password_incorrect'
        ? 'passwordIncorrect'
        : change.error
          ? (apiFailureKey(change.error) ?? 'requestFailed')
          : null;
  const reset = () => {
    change.reset();
    clear();
  };
  return {
    current,
    setCurrent,
    next,
    setNext,
    confirm,
    setConfirm,
    change,
    failure,
    reset,
    tooShort: next.length > 0 && next.length < 12,
    mismatch: confirm.length > 0 && confirm !== next,
    ready: current !== '' && next.length >= 12 && confirm === next,
  };
}

/** Changes the owner password; the server revokes every other session and rotates this one. */
export function ChangePasswordDialog({ open, csrfToken, closeLabel, onOpenChange, onCloseAutoFocus }: ChangePasswordDialogProps) {
  const t = useTranslations('account');
  const { current, setCurrent, next, setNext, confirm, setConfirm, change, failure, reset, tooShort, mismatch, ready } =
    useChangePassword(csrfToken);
  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (ready && !change.isPending) change.mutate();
  };
  const handleOpenChange = (value: boolean) => {
    if (!value) reset();
    onOpenChange(value);
  };

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent closeLabel={closeLabel} onCloseAutoFocus={onCloseAutoFocus}>
        <DialogHeader>
          <DialogTitle>{t('changePassword')}</DialogTitle>
          <DialogDescription>{t('changePasswordHelp')}</DialogDescription>
        </DialogHeader>
        <form onSubmit={submit} className="grid gap-3">
          <div className="field">
            <Label htmlFor="cp-current">{t('currentPassword')}</Label>
            <PasswordInput
              id="cp-current"
              autoComplete="current-password"
              value={current}
              onChange={(event) => setCurrent(event.target.value)}
            />
          </div>
          <div className="field">
            <Label htmlFor="cp-new">{t('newPassword')}</Label>
            <PasswordInput
              id="cp-new"
              autoComplete="new-password"
              aria-invalid={tooShort}
              aria-describedby="cp-new-help"
              value={next}
              onChange={(event) => setNext(event.target.value)}
            />
            <span id="cp-new-help" aria-live="polite" className={tooShort ? 'error' : 'muted'}>
              {t('newPasswordHelp')}
            </span>
          </div>
          <div className="field">
            <Label htmlFor="cp-confirm">{t('confirmNewPassword')}</Label>
            <PasswordInput
              id="cp-confirm"
              autoComplete="new-password"
              aria-invalid={mismatch}
              aria-describedby={mismatch ? 'cp-confirm-error' : undefined}
              value={confirm}
              onChange={(event) => setConfirm(event.target.value)}
            />
            {mismatch && (
              <span id="cp-confirm-error" className="error" role="alert">
                {t('passwordMismatch')}
              </span>
            )}
          </div>
          {failure && (
            <p className="error" role="alert">
              {t(failure)}
            </p>
          )}
          <p className="text-muted-foreground" role="status">
            {change.isSuccess ? t('passwordChanged') : ''}
          </p>
          <DialogFooter>
            <Button type="submit" disabled={!ready || change.isPending}>
              {change.isPending ? t('changingPassword') : t('changePassword')}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
