'use client';

import { useMutation, useQuery } from '@tanstack/react-query';
import { useState, type FormEvent } from 'react';
import { useTranslations } from 'next-intl';
import { AlertDialog, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle, AlertDialogTrigger } from '@/components/ui/alert-dialog';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { PasswordInput } from '@/components/ui/password-input';
import { Label } from '@/components/ui/label';
import { ApiError, apiRequest } from '@/core/api';
import { announceAccountErasure } from '@/core/account-erasure';
import { acceptAccountDeletion, type DeletionPreview } from './deletion-api';

/** Confirm permanent account erasure after server-owned blockers and a fresh password check.
 * The preview preissues an HttpOnly status receipt. Neither the password nor receipt is
 * stored in browser persistence, mutation variables, URLs or query-cache keys.
 */
export function AccountDeletionSection({ csrfToken }: { csrfToken: string }) {
  const t = useTranslations('accountDeletion');
  const [open, setOpen] = useState(false);
  const [password, setPassword] = useState('');
  const [confirmation, setConfirmation] = useState('');
  const preview = useQuery({
    queryKey: ['account-deletion-preview'],
    queryFn: ({ signal }) => apiRequest<DeletionPreview>('/api/v1/auth/account/deletion-preview', { signal }),
    enabled: open,
    staleTime: 0,
  });
  const deletion = useMutation({
    mutationFn: () => acceptAccountDeletion(password, csrfToken),
    onSuccess: () => announceAccountErasure(),
    onError: () => { void preview.refetch(); },
    onSettled: () => setPassword(''),
  });
  const ready = Boolean(preview.data?.can_delete && !preview.isFetching && !preview.isError && password && confirmation === 'DELETE');
  const failure = deletion.error instanceof ApiError && deletion.error.code === 'password_incorrect' ? 'passwordIncorrect'
    : deletion.error instanceof ApiError && deletion.error.status === 429 ? 'tooMany' : 'failed';

  /** Closing drops the transient password; an accepted/ambiguous in-flight request must settle first. */
  const changeOpen = (value: boolean) => {
    if (deletion.isPending) return;
    setOpen(value);
    setPassword('');
    setConfirmation('');
    deletion.reset();
  };
  /** Submit only after typed confirmation and the latest successful blocker check. */
  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (ready && !deletion.isPending) deletion.mutate();
  };

  return <section className="grid gap-3 border-t border-border pt-4" aria-labelledby="account-deletion-title">
    <h2 id="account-deletion-title" className="font-semibold">{t('title')}</h2>
    <p className="text-sm text-muted-foreground">{t('help')}</p>
    <Button asChild variant="outline"><a href="/api/v1/exports/json" download="umwelt-os-export.json">{t('export')}</a></Button>
    <AlertDialog open={open} onOpenChange={changeOpen}>
      <AlertDialogTrigger asChild><Button variant="destructive">{t('title')}</Button></AlertDialogTrigger>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle className="text-lg font-semibold">{t('confirmTitle')}</AlertDialogTitle>
          <AlertDialogDescription className="text-sm text-muted-foreground">{t('help')}</AlertDialogDescription>
        </AlertDialogHeader>
        {preview.isPending && <p role="status">{t('checking')}</p>}
        {preview.isError && <p role="alert">{t('failed')}</p>}
        {preview.data && <div className="grid gap-2 text-sm">
          <p>{t('scope', { count: preview.data.workspace_count })}</p>
          {preview.data.blockers.map((blocker) => <p key={blocker} className="text-destructive" role="alert">{t(blocker)}</p>)}
          {preview.data.instance_operator && <p>{t('operator')}</p>}
        </div>}
        <p className="text-sm text-muted-foreground">{t('limits')}</p>
        <Button variant="outline" type="button" disabled={preview.isFetching || deletion.isPending} onClick={() => void preview.refetch()}>{t('retry')}</Button>
        <form onSubmit={submit} className="grid gap-3">
          <div className="grid gap-1"><Label htmlFor="delete-account-password">{t('password')}</Label><PasswordInput id="delete-account-password" autoComplete="current-password" maxLength={128} value={password} disabled={deletion.isPending} onChange={(event) => setPassword(event.target.value)} /></div>
          <div className="grid gap-1"><Label htmlFor="delete-account-confirmation">{t('confirmation')}</Label><Input id="delete-account-confirmation" autoComplete="off" spellCheck={false} value={confirmation} disabled={deletion.isPending} onChange={(event) => setConfirmation(event.target.value)} /></div>
          {deletion.isError && <p className="text-destructive text-sm" role="alert">{t(failure)}</p>}
          <AlertDialogFooter>
            <AlertDialogCancel disabled={deletion.isPending}>{t('cancel')}</AlertDialogCancel>
            <Button type="submit" variant="destructive" disabled={!ready || deletion.isPending}>{deletion.isPending ? t('deleting') : t('title')}</Button>
          </AlertDialogFooter>
        </form>
      </AlertDialogContent>
    </AlertDialog>
  </section>;
}
