'use client';

import { zodResolver } from '@hookform/resolvers/zod';
import { useMutation, useQuery } from '@tanstack/react-query';
import { AlertTriangleIcon, UserIcon } from 'lucide-react';
import { useRouter } from 'next/navigation';
import { useTranslations } from 'next-intl';
import { useSyncExternalStore } from 'react';
import { useForm } from 'react-hook-form';
import { z } from 'zod';
import { ApiError, apiRequest, csrfHeaders } from '@/core/api';
import { apiFailureKey } from '@/core/api-failure-key';
import { UmweltLogo } from '@/components/brand/umwelt-mark';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { AppearanceControl } from '@/modules/account/appearance-control';

const loginSchema = z.object({ password: z.string().min(1).max(128) });
type LoginForm = z.infer<typeof loginSchema>;

/** Reads the current query string without a state-in-effect round trip; empty on the server. */
function useSearch(): string {
  return useSyncExternalStore(() => () => undefined, () => window.location.search, () => '');
}

/** True when a failure means the local service did not answer (network error or 5xx). */
function isUnreachable(error: unknown): boolean {
  return error !== null && error !== undefined && (!(error instanceof ApiError) || error.status >= 500);
}

/** Renders sign-in controls and reports authentication state to the user. */
export default function LoginPage() {
  const router = useRouter();
  const t = useTranslations('login');
  const search = new URLSearchParams(useSearch());
  const googleError = search.get('google') === 'error';
  const expired = search.get('reason') === 'expired';
  const form = useForm<LoginForm>({ resolver: zodResolver(loginSchema) });
  const googleStatus = useQuery({ queryKey: ['auth-google-status'], queryFn: () => apiRequest<{ configured: boolean; linked: boolean }>('/api/v1/auth/google/status') });
  const googleLogin = useMutation({
    mutationFn: async () => {
      const csrf = await apiRequest<{ csrfToken: string }>('/api/v1/auth/csrf');
      return apiRequest<{ authorization_url: string }>('/api/v1/auth/google/start', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...csrfHeaders(csrf.csrfToken) },
        body: JSON.stringify({ purpose: 'login' }),
      });
    },
    onSuccess: ({ authorization_url }) => window.location.assign(authorization_url),
  });
  const login = useMutation({
    mutationFn: async (values: LoginForm) => {
      const csrf = await apiRequest<{ csrfToken: string }>('/api/v1/auth/csrf');
      return apiRequest<{ authenticated: boolean }>('/api/v1/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...csrfHeaders(csrf.csrfToken) },
        body: JSON.stringify(values),
      });
    },
    onSuccess: () => router.replace('/app'),
  });

  const unavailable = (googleStatus.error !== null && !(googleStatus.error instanceof ApiError)) || isUnreachable(login.error);
  const invalid = login.error ? apiFailureKey(login.error) : null;
  const passwordError = Boolean(form.formState.errors.password) || invalid === 'unauthorized';
  const showGoogle = googleStatus.isSuccess && googleStatus.data.configured;
  const googleOff = !showGoogle || unavailable;
  const googleHelp = googleStatus.isPending ? t('checkingGoogle')
    : googleStatus.isError ? t('googleUnknown')
      : googleStatus.data.configured ? t('googleHelp') : t('googleUnconfigured');
  const apiText = unavailable ? t('apiUnreachable') : googleStatus.isPending ? t('apiChecking') : t('apiConnected');

  return <div className="flex min-h-dvh flex-col bg-secondary text-foreground">
    <header className="flex h-16 items-center gap-4 border-b border-border bg-background px-4 sm:px-6">
      <UmweltLogo />
      <span className="flex-1" />
      <AppearanceControl />
    </header>
    <main id="main-content" className="flex flex-1 items-center justify-center px-4 py-10 sm:py-12">
      <section aria-labelledby="login-title" className="auth-panel flex flex-col gap-6">
        <div className="grid gap-1">
          <span className="brand">{t('eyebrow')}</span>
          <h1 id="login-title" className="text-3xl font-bold leading-tight tracking-tight">{t('welcome')}</h1>
          <p className="muted">{t('privateWorkspace')}</p>
        </div>
        {expired && !invalid && !unavailable && <div role="status" className="grid gap-0.5 rounded-xl border border-border bg-secondary px-4 py-3 text-sm">
          <strong>{t('sessionExpired')}</strong><span className="muted">{t('sessionExpiredHelp')}</span>
        </div>}
        {unavailable && <div role="alert" className="grid gap-0.5 rounded-xl border border-destructive px-4 py-3 text-sm">
          <strong className="flex items-center gap-2 text-destructive"><AlertTriangleIcon aria-hidden="true" className="size-4" />{t('serviceUnavailable')}</strong>
          <span className="muted">{t('checkApi')}</span>
          <Button type="button" variant="outline" size="sm" className="mt-2 w-fit" onClick={() => { void googleStatus.refetch(); login.reset(); }}>{t('retry')}</Button>
        </div>}
        {invalid && !unavailable && <div role="alert" id="login-error" className="flex items-start gap-3 rounded-xl border border-destructive px-4 py-3 text-sm text-destructive">
          <AlertTriangleIcon aria-hidden="true" className="mt-0.5 size-4 shrink-0" /><span>{t(invalid)}</span>
        </div>}
        {login.error && !invalid && !unavailable && <p className="error" role="alert" id="login-error">{t('signInFailed')}</p>}
        <form className="grid gap-4" onSubmit={form.handleSubmit((values) => login.mutate(values))} noValidate>
          <div className="grid gap-1.5">
            <span className="text-[13px] font-semibold">{t('localAccount')}</span>
            <div className="flex min-h-[52px] items-center gap-3 rounded-[9px] border border-border px-3">
              <span aria-hidden="true" className="inline-flex size-8 items-center justify-center rounded-full bg-secondary text-primary"><UserIcon className="size-4" /></span>
              <span className="grid"><strong className="text-sm font-semibold leading-tight">{t('ownerAccount')}</strong><span className="muted text-xs">{t('passwordOnly')}</span></span>
            </div>
          </div>
          <div className="field">
            <Label htmlFor="password">{t('password')}</Label>
            <Input id="password" type="password" autoComplete="current-password" autoFocus placeholder={t('passwordPlaceholder')}
              aria-invalid={passwordError || undefined} aria-describedby={`password-help${login.error ? ' login-error' : ''}`} {...form.register('password')} />
            {form.formState.errors.password && <span className="error" role="alert">{t('enterPassword')}</span>}
            <p id="password-help" className="muted text-xs">{t('passwordHelp')}</p>
          </div>
          <Button type="submit" className="w-full" disabled={login.isPending || unavailable}>{login.isPending ? t('signingIn') : t('signIn')}</Button>
        </form>
        <div className="muted flex items-center gap-3 text-[13px]" aria-hidden="true"><span className="h-px flex-1 bg-border" /><span>{t('orUseGoogle')}</span><span className="h-px flex-1 bg-border" /></div>
        <div className="grid gap-2">
          {googleError && <p className="error" role="alert">{t('googleFailed')}</p>}
          <Button type="button" variant="outline" className="w-full" aria-describedby="google-help" disabled={googleOff || googleLogin.isPending} onClick={() => googleLogin.mutate()}>{googleLogin.isPending ? t('openingGoogle') : t('continueGoogle')}</Button>
          <p id="google-help" className="muted text-xs leading-snug" role="status">{googleHelp}</p>
          {googleLogin.error && <p className="error" role="alert">{t(apiFailureKey(googleLogin.error) ?? 'googleStartFailed')}</p>}
        </div>
      </section>
    </main>
    <footer className="flex flex-wrap items-center gap-4 border-t border-border bg-background px-4 py-2.5 text-xs text-muted-foreground sm:px-6">
      <span role="status" className="inline-flex items-center gap-1.5"><span aria-hidden="true" className={`size-2 rounded-full ${unavailable ? 'bg-destructive' : 'bg-primary'}`} />{apiText}</span>
      <span>{t('noDataBeforeSignIn')}</span>
    </footer>
  </div>;
}
