'use client';

import { useMutation, useQuery } from '@tanstack/react-query';
import { AlertTriangleIcon } from 'lucide-react';
import { useRouter } from 'next/navigation';
import { useTranslations } from 'next-intl';
import { useEffect, useState, type FormEvent } from 'react';
import { UmweltLogo } from '@/components/brand/umwelt-mark';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { ApiError, apiRequest, csrfHeaders } from '@/core/api';

const GOOGLE_RETURN_KEY = 'umwelt:invite-google';
const MIN_PASSWORD = 12;

// The bearer token lives only in this module variable: never in React state, props, storage (except the
// Google round trip below), query keys or rendered output.
let heldToken: string | null = null;
let googleEnrolled = false;

/**
 * Reads the invitation token from the URL exactly once, strips the query at once, and keeps it in memory.
 * The Google redirect cannot carry the token, so it is parked in sessionStorage for that one hop only.
 */
export function takeInviteToken(): { present: boolean; google: boolean } {
  if (typeof window === 'undefined') return { present: false, google: false };
  const url = new URL(window.location.href);
  if (url.search) {
    const fromUrl = url.searchParams.get('token');
    const googleReturn = url.searchParams.get('google') === 'enrollment';
    if (fromUrl) heldToken = fromUrl;
    else if (googleReturn) {
      try { heldToken = window.sessionStorage.getItem(GOOGLE_RETURN_KEY); } catch { heldToken = null; }
      googleEnrolled = heldToken !== null;
    }
    window.history.replaceState(null, '', url.pathname);
  }
  try { window.sessionStorage.removeItem(GOOGLE_RETURN_KEY); } catch { /* storage may be blocked */ }
  return { present: heldToken !== null, google: googleEnrolled };
}

/** Drops the in-memory token (success or cancel). */
export function clearInviteToken(): void { heldToken = null; googleEnrolled = false; }

type Accepted = { membership: { email: string | null } };
type FailureKey = 'requestFailed' | 'inviteTooMany' | 'inviteAuthRequired' | 'inviteWrongAccount' | 'notEnabled' | 'inviteUnavailable' | 'inviteGoogleFailed';

/** Maps an accept failure to a neutral message key; server text is never shown. */
function failureKey(error: unknown, signedIn: boolean): FailureKey {
  if (!(error instanceof ApiError)) return 'requestFailed';
  if (error.status === 429) return 'inviteTooMany';
  if (error.status === 401) return 'inviteAuthRequired';
  if (error.status === 403) return signedIn ? 'inviteWrongAccount' : 'notEnabled';
  if (error.status === 404 || error.status === 410) return 'inviteUnavailable';
  return 'requestFailed';
}

/** Collects a real password (or a Google proof) and accepts the invitation without signing the user in. */
export function InviteAccept() {
  const t = useTranslations('workspaceMembers');
  const router = useRouter();
  const [state, setState] = useState({ checked: false, present: false, google: false });
  const [password, setPassword] = useState('');
  const [confirm, setConfirm] = useState('');
  const [touched, setTouched] = useState(false);

  useEffect(() => {
    const read = takeInviteToken();
    // eslint-disable-next-line react-hooks/set-state-in-effect -- the URL can only be read after mount.
    setState({ checked: true, ...read });
  }, []);

  const session = useQuery({
    queryKey: ['invite-session'],
    queryFn: () => apiRequest<{ csrfToken: string }>('/api/v1/auth/session'),
    enabled: state.present, retry: false,
  });
  const signedIn = session.isSuccess;
  const googleStatus = useQuery({
    queryKey: ['invite-google-status'],
    queryFn: () => apiRequest<{ configured: boolean }>('/api/v1/auth/google/status'),
    enabled: state.present && session.isError, retry: false,
  });

  const accept = useMutation({
    mutationFn: async () => {
      const csrf = signedIn && session.data ? session.data : await apiRequest<{ csrfToken: string }>('/api/v1/auth/csrf');
      return apiRequest<Accepted>('/api/v1/workspaces/invitations/accept', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...csrfHeaders(csrf.csrfToken) },
        body: JSON.stringify({
          token: heldToken,
          ...(signedIn || state.google ? {} : { password }),
          google_enrollment: state.google,
        }),
      });
    },
    onSuccess: (result) => {
      clearInviteToken();
      setPassword(''); setConfirm('');
      // Acceptance issues no session: continue at the identifier login.
      const email = result.membership.email;
      router.replace(email ? `/login?identifier=${encodeURIComponent(email)}` : '/login?identifier=');
    },
  });
  const google = useMutation({
    mutationFn: async () => {
      const csrf = await apiRequest<{ csrfToken: string }>('/api/v1/auth/csrf');
      const started = await apiRequest<{ authorization_url: string }>('/api/v1/auth/google/start', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...csrfHeaders(csrf.csrfToken) },
        body: JSON.stringify({ purpose: 'invitation', invitation_token: heldToken }),
      });
      try { if (heldToken) window.sessionStorage.setItem(GOOGLE_RETURN_KEY, heldToken); } catch { /* no storage: Google enrollment cannot resume */ }
      return started;
    },
    onSuccess: ({ authorization_url }) => window.location.assign(authorization_url),
  });

  const needsPassword = !signedIn && !state.google;
  const tooShort = password.length < MIN_PASSWORD;
  const mismatch = password !== confirm;
  const invalid = needsPassword && (tooShort || mismatch);
  const probing = state.present && session.isPending;

  function submit(event: FormEvent) {
    event.preventDefault();
    setTouched(true);
    if (!invalid) accept.mutate();
  }
  function cancel() { clearInviteToken(); setPassword(''); setConfirm(''); router.replace('/login'); }

  const errorKey: FailureKey | null = accept.error ? failureKey(accept.error, signedIn) : google.error ? 'inviteGoogleFailed' : null;

  return <div className="flex min-h-dvh flex-col bg-secondary text-foreground">
    <header className="flex h-16 items-center border-b border-border bg-background px-4 sm:px-6"><UmweltLogo /></header>
    <main id="main-content" className="flex flex-1 items-center justify-center px-4 py-10 sm:py-12">
      <section aria-labelledby="invite-title" className="auth-panel flex flex-col gap-6">
        <div className="grid gap-1">
          <h1 id="invite-title" className="text-3xl font-bold leading-tight tracking-tight">{t('inviteTitle')}</h1>
          <p className="muted">{t('inviteIntro')}</p>
        </div>
        {state.checked && !state.present && <p role="alert" className="rounded-xl border border-destructive px-4 py-3 text-sm text-destructive">{t('inviteMissing')}</p>}
        {state.present && !probing && <form className="grid gap-4" onSubmit={submit} noValidate>
          {signedIn && <p role="status" className="muted text-sm">{t('inviteSignedIn')}</p>}
          {state.google && <p role="status" className="muted text-sm">{t('inviteGoogleReady')}</p>}
          {needsPassword && <>
            <div className="field">
              <Label htmlFor="invite-password">{t('invitePassword')}</Label>
              <Input id="invite-password" type="password" autoComplete="new-password" value={password} onChange={(e) => setPassword(e.target.value)}
                aria-invalid={touched && tooShort ? true : undefined} aria-describedby="invite-password-help" />
              <p id="invite-password-help" className="muted text-xs">{t('invitePasswordHelp')}</p>
              {touched && tooShort && <span className="error" role="alert">{t('invitePasswordShort')}</span>}
            </div>
            <div className="field">
              <Label htmlFor="invite-confirm">{t('invitePasswordConfirm')}</Label>
              <Input id="invite-confirm" type="password" autoComplete="new-password" value={confirm} onChange={(e) => setConfirm(e.target.value)}
                aria-invalid={touched && mismatch ? true : undefined} />
              {touched && mismatch && <span className="error" role="alert">{t('invitePasswordMismatch')}</span>}
            </div>
          </>}
          {errorKey && <div role="alert" className="flex items-start gap-3 rounded-xl border border-destructive px-4 py-3 text-sm text-destructive">
            <AlertTriangleIcon aria-hidden="true" className="mt-0.5 size-4 shrink-0" /><span>{t(errorKey)}</span>
          </div>}
          <Button type="submit" className="w-full" disabled={accept.isPending}>{accept.isPending ? t('inviteAccepting') : t('inviteAccept')}</Button>
          {!signedIn && !state.google && googleStatus.data?.configured && <Button type="button" variant="outline" className="w-full" disabled={google.isPending} onClick={() => google.mutate()}>{t('inviteGoogle')}</Button>}
          <Button type="button" variant="ghost" className="w-full" onClick={cancel}>{t('inviteCancel')}</Button>
        </form>}
      </section>
    </main>
  </div>;
}
