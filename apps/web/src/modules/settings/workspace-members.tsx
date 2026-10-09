'use client';

import { useInfiniteQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { useFormatter, useTranslations } from 'next-intl';
import { useState, type FormEvent } from 'react';
import {
  AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent, AlertDialogDescription,
  AlertDialogFooter, AlertDialogHeader, AlertDialogTitle,
} from '@/components/ui/alert-dialog';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { ApiError, apiRequest, csrfHeaders } from '@/core/api';
import { apiFailureKey } from '@/core/api-failure-key';
import { useWorkspaceSession } from '@/core/app-shell/workspace-shell';
import { workspaceKeys } from '@/core/query-keys';
import { useWorkspace } from '@/core/workspace-context';

export type InvitationRead = {
  id: string; email: string; created_at: string; expires_at: string;
  accepted_at: string | null; accepted_by_user_id: number | null; revoked_at: string | null;
};
export type MemberRead = { user_id: number; email: string | null; role: 'owner' | 'member'; membership_revision: number };
type Page<T, C> = { items: T[]; next_cursor: C | null };
type Created = { invitation_id: string; invitation_url: string; expires_at: string };
type Pending = { kind: 'revoke'; id: string } | { kind: 'remove'; id: number };

const EMAIL_PATTERN = /^[^\s@]+@[^\s@]+$/;

/** Derives the display status of an invitation from its timestamps. */
export function invitationStatus(row: InvitationRead, now = Date.now()): 'accepted' | 'revoked' | 'expired' | 'pending' {
  if (row.accepted_at) return 'accepted';
  if (row.revoked_at) return 'revoked';
  return Date.parse(row.expires_at) <= now ? 'expired' : 'pending';
}

/** Owner-only invitation and member management for the selected workspace. */
export function WorkspaceMembers() {
  const t = useTranslations('workspaceMembers');
  const format = useFormatter();
  const { csrfToken } = useWorkspaceSession();
  const { selection, isOwner } = useWorkspace();
  const client = useQueryClient();
  const workspaceId = selection?.id ?? null;
  const revision = selection?.revision ?? null;
  const base = `/api/v1/workspaces/${workspaceId}`;

  const [email, setEmail] = useState('');
  const [emailError, setEmailError] = useState(false);
  const [link, setLink] = useState<string | null>(null);
  const [copied, setCopied] = useState<'ok' | 'failed' | null>(null);
  const [pending, setPending] = useState<Pending | null>(null);

  const enabled = Boolean(workspaceId) && isOwner;
  const invitations = useInfiniteQuery({
    queryKey: [...workspaceKeys.all, workspaceId, 'invitations'],
    queryFn: ({ pageParam }) => apiRequest<Page<InvitationRead, string>>(`${base}/invitations?limit=50${pageParam ? `&cursor=${pageParam}` : ''}`),
    initialPageParam: '', getNextPageParam: (last) => last.next_cursor ?? undefined, enabled,
  });
  const members = useInfiniteQuery({
    queryKey: [...workspaceKeys.all, workspaceId, 'members'],
    queryFn: ({ pageParam }) => apiRequest<Page<MemberRead, number>>(`${base}/members?limit=50${pageParam ? `&cursor=${pageParam}` : ''}`),
    initialPageParam: 0, getNextPageParam: (last) => last.next_cursor ?? undefined, enabled,
  });

  // Every owner mutation bumps the workspace revision, which is also the next If-Match / expected_revision.
  const refresh = () => Promise.all([
    client.invalidateQueries({ queryKey: [...workspaceKeys.all, workspaceId] }),
    client.invalidateQueries({ queryKey: workspaceKeys.list() }),
  ]);
  const writeHeaders = (extra: Record<string, string> = {}) => ({ ...csrfHeaders(csrfToken), ...extra });

  const create = useMutation({
    mutationFn: () => apiRequest<Created>(`${base}/invitations`, {
      method: 'POST', headers: writeHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({ email: email.trim(), expected_revision: revision }),
    }),
    onSuccess: (created) => { setLink(created.invitation_url); setCopied(null); setEmail(''); },
    onSettled: refresh,
  });
  const revoke = useMutation({
    mutationFn: (id: string) => apiRequest<void>(`${base}/invitations/${id}`, { method: 'DELETE', headers: writeHeaders({ 'If-Match': `"${revision}"` }) }),
    onSettled: refresh,
  });
  const remove = useMutation({
    mutationFn: (userId: number) => apiRequest<void>(`${base}/members/${userId}`, { method: 'DELETE', headers: writeHeaders({ 'If-Match': `"${revision}"` }) }),
    onSettled: refresh,
  });

  if (!selection) return <section className="content-panel skeleton" aria-label={t('loading')} />;
  if (!isOwner) return <section className="content-panel"><h1>{t('title')}</h1><p className="muted">{t('ownerOnly')}</p></section>;

  const error = create.error ?? revoke.error ?? remove.error;
  const errorKey = error ? failureText(error) : null;
  function failureText(e: unknown): 'forbidden' | 'conflict' | 'invalidEmail' | 'notEnabled' | 'requestFailed' {
    if (e instanceof ApiError && e.status === 422) return 'invalidEmail';
    const key = apiFailureKey(e);
    return key === 'forbidden' || key === 'conflict' ? key : 'requestFailed';
  }

  function submit(event: FormEvent) {
    event.preventDefault();
    const valid = EMAIL_PATTERN.test(email.trim());
    setEmailError(!valid);
    if (valid) create.mutate();
  }
  async function copy() {
    try { await navigator.clipboard.writeText(link ?? ''); setCopied('ok'); } catch { setCopied('failed'); }
  }
  function confirmPending() {
    if (pending?.kind === 'revoke') revoke.mutate(pending.id);
    if (pending?.kind === 'remove') remove.mutate(pending.id);
    setPending(null);
  }

  const invitationRows = invitations.data?.pages.flatMap((p) => p.items) ?? [];
  const memberRows = members.data?.pages.flatMap((p) => p.items) ?? [];
  const statusLabel = { pending: t('statusPending'), accepted: t('statusAccepted'), revoked: t('statusRevoked'), expired: t('statusExpired') };

  return <section className="content-panel grid gap-8">
    <div className="grid gap-1">
      <h1>{t('title')}</h1>
      <p className="muted">{t('intro')}</p>
    </div>

    <form className="grid gap-3" onSubmit={submit} noValidate>
      <h2 className="text-lg font-semibold">{t('inviteHeading')}</h2>
      <div className="field">
        <Label htmlFor="invite-email">{t('emailLabel')}</Label>
        <div className="flex flex-wrap gap-2">
          <Input id="invite-email" type="email" className="min-w-0 flex-1" autoComplete="off" value={email} placeholder={t('emailPlaceholder')}
            onChange={(e) => setEmail(e.target.value)} aria-invalid={emailError || undefined} />
          <Button type="submit" disabled={create.isPending}>{create.isPending ? t('creating') : t('createInvite')}</Button>
        </div>
        {emailError && <span className="error" role="alert">{t('invalidEmail')}</span>}
      </div>
    </form>

    {link && <div className="grid gap-2 rounded-xl border border-border bg-secondary p-4" role="status">
      <h3 className="text-sm font-semibold">{t('linkHeading')}</h3>
      <p className="muted text-xs">{t('linkHelp')}</p>
      <div className="flex flex-wrap gap-2">
        <Input readOnly value={link} aria-label={t('linkHeading')} className="min-w-0 flex-1" onFocus={(e) => e.currentTarget.select()} />
        <Button type="button" variant="outline" onClick={() => void copy()}>{copied === 'ok' ? t('copied') : t('copy')}</Button>
        <Button type="button" variant="ghost" onClick={() => setLink(null)}>{t('dismissLink')}</Button>
      </div>
      {copied === 'failed' && <span className="error" role="alert">{t('copyFailed')}</span>}
    </div>}

    {errorKey && <p className="error" role="alert">{t(errorKey)}</p>}

    <div className="grid gap-3">
      <h2 className="text-lg font-semibold">{t('invitationsHeading')}</h2>
      {invitations.isError ? listError(() => void invitations.refetch()) : invitations.isPending ? <p className="muted">{t('loading')}</p>
        : invitationRows.length === 0 ? <p className="muted">{t('noInvitations')}</p>
          : <ul className="grid gap-2">{invitationRows.map((row) => {
            const status = invitationStatus(row);
            return <li key={row.id} className="flex flex-wrap items-center gap-3 rounded-xl border border-border px-4 py-3">
              <span className="min-w-0 flex-1 break-all text-sm font-medium">{row.email}</span>
              <span className="muted text-xs">{statusLabel[status]}{status === 'pending' ? ` · ${t('expiresOn', { date: format.dateTime(new Date(row.expires_at), { dateStyle: 'medium' }) })}` : ''}</span>
              {status === 'pending' && <Button type="button" variant="outline" size="sm" onClick={() => setPending({ kind: 'revoke', id: row.id })}>{t('revoke')}</Button>}
            </li>;
          })}</ul>}
      {invitations.hasNextPage && <Button type="button" variant="outline" className="w-fit" disabled={invitations.isFetchingNextPage} onClick={() => void invitations.fetchNextPage()}>{invitations.isFetchingNextPage ? t('loading') : t('loadMore')}</Button>}
    </div>

    <div className="grid gap-3">
      <h2 className="text-lg font-semibold">{t('membersHeading')}</h2>
      {members.isError ? listError(() => void members.refetch()) : members.isPending ? <p className="muted">{t('loading')}</p>
        : memberRows.length === 0 ? <p className="muted">{t('noMembers')}</p>
          : <ul className="grid gap-2">{memberRows.map((row) => <li key={row.user_id} className="flex flex-wrap items-center gap-3 rounded-xl border border-border px-4 py-3">
            <span className="min-w-0 flex-1 break-all text-sm font-medium">{row.email ?? t('unknownEmail')}</span>
            <span className="muted text-xs">{row.role === 'owner' ? t('roleOwner') : t('roleMember')}</span>
            {row.role === 'member' && <Button type="button" variant="outline" size="sm" onClick={() => setPending({ kind: 'remove', id: row.user_id })}>{t('remove')}</Button>}
          </li>)}</ul>}
      {members.hasNextPage && <Button type="button" variant="outline" className="w-fit" disabled={members.isFetchingNextPage} onClick={() => void members.fetchNextPage()}>{members.isFetchingNextPage ? t('loading') : t('loadMore')}</Button>}
    </div>

    <AlertDialog open={pending !== null} onOpenChange={(open) => { if (!open) setPending(null); }}>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>{pending?.kind === 'remove' ? t('removeTitle') : t('revokeTitle')}</AlertDialogTitle>
          <AlertDialogDescription>{pending?.kind === 'remove' ? t('removeBody') : t('revokeBody')}</AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel>{t('cancel')}</AlertDialogCancel>
          <AlertDialogAction variant="destructive" onClick={confirmPending}>{t('confirm')}</AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  </section>;

  function listError(onRetry: () => void) {
    return <div role="alert" className="flex items-center gap-3"><span className="error">{t('loadFailed')}</span><Button type="button" variant="outline" size="sm" onClick={onRetry}>{t('retry')}</Button></div>;
  }
}
