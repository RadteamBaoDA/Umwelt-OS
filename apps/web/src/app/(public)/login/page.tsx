'use client';

import { zodResolver } from '@hookform/resolvers/zod';
import { useMutation, useQuery } from '@tanstack/react-query';
import { AlertTriangleIcon, UserIcon } from 'lucide-react';
import { useRouter } from 'next/navigation';
import { useTranslations } from 'next-intl';
import { useEffect, useState, useSyncExternalStore } from 'react';
import { useForm } from 'react-hook-form';
import { z } from 'zod';
import { ApiError, apiRequest, csrfHeaders } from '@/core/api';
import { apiFailureKey } from '@/core/api-failure-key';
import { assignAuthorizationUrl } from '@/core/safe-url';
import { UmweltLogo } from '@/components/brand/umwelt-mark';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { PasswordInput } from '@/components/ui/password-input';
import { Label } from '@/components/ui/label';
import { LegalLinks } from '@/modules/legal';
import { AppearanceControl } from '@/modules/account';
import { useDisplayPreferences } from '@/core/query-provider';

const loginSchema = z.object({ password: z.string().min(1).max(128), identifier: z.string().max(320).optional() });
type LoginForm = z.infer<typeof loginSchema>;

/** Reads the current query string without a state-in-effect round trip; empty on the server. */
function useSearch(): string {
  return useSyncExternalStore(
    () => () => undefined,
    () => window.location.search,
    () => '',
  );
}

/** True when a failure means the local service did not answer (network error or 5xx). */
function isUnreachable(error: unknown): boolean {
  return error !== null && error !== undefined && (!(error instanceof ApiError) || error.status >= 500);
}

/** Renders sign-in controls and reports authentication state to the user. */
export default function LoginPage() {
  const s0 = useLoginState();
  const { t, form } = s0;
  const s1 = useLoginRequests(s0);
  const { login } = s1;

  const s2 = deriveLoginFlags(s1);
  const { unavailable } = s2;

  const vm: LoginPageVm = s2;
  return (
    <div className="flex min-h-dvh flex-col bg-secondary text-foreground">
      <header className="flex h-16 items-center gap-4 border-b border-border bg-background px-4 sm:px-6">
        <UmweltLogo />
        <span className="flex-1" />
        <AppearanceControl />
      </header>
      <main id="main-content" className="flex flex-1 items-center justify-center px-4 py-10 sm:py-12">
        <section aria-labelledby="login-title" className="auth-panel flex flex-col gap-6">
          <div className="grid gap-1">
            <h1 id="login-title" className="m-0 text-2xl font-semibold leading-tight">
              {t('welcome')}
            </h1>
            <p className="text-muted-foreground">{t('privateWorkspace')}</p>
          </div>
          <LoginBanners vm={vm} />
          <form className="grid gap-4" onSubmit={form.handleSubmit((values) => login.mutate(values))} noValidate>
            <LoginIdentity vm={vm} />
            <LoginPasswordField vm={vm} />
            <Button type="submit" className="w-full" disabled={login.isPending || unavailable}>
              {login.isPending ? t('signingIn') : t('signIn')}
            </Button>
          </form>
          {/* Hidden entirely unless the server reports Google sign-in as configured. */}
          <GoogleSignIn vm={vm} />
        </section>
      </main>
      <footer className="flex flex-wrap items-center gap-4 border-t border-border bg-background px-4 py-2.5 text-xs text-muted-foreground sm:px-6">
        {/* Status is shown only when the API is unreachable; a healthy connection needs no label. */}
        {unavailable && (
          <span role="status" className="inline-flex items-center gap-1.5">
            <span aria-hidden="true" className="size-2 rounded-full bg-destructive" />
            {t('apiUnreachable')}
          </span>
        )}
        <LegalLinks />
      </footer>
    </div>
  );
}

type LoginPageVm = ReturnType<typeof deriveLoginFlags>;

/** Session-expired, service-unavailable and sign-in failure notices. */
function LoginBanners({ vm }: { vm: LoginPageVm }) {
  const { t, expired, googleStatus, login, unavailable, invalid } = vm;
  return (
    <>
      {expired && !invalid && !unavailable && (
        <div role="status" className="grid gap-0.5 rounded-lg border border-border bg-secondary px-4 py-3 text-sm">
          <strong>{t('sessionExpired')}</strong>
          <span className="text-muted-foreground">{t('sessionExpiredHelp')}</span>
        </div>
      )}
      {unavailable && (
        <div role="alert" className="grid gap-0.5 rounded-lg border border-destructive px-4 py-3 text-sm">
          <strong className="flex items-center gap-2 text-destructive">
            <AlertTriangleIcon aria-hidden="true" className="size-4" />
            {t('serviceUnavailable')}
          </strong>
          <span className="text-muted-foreground">{t('checkApi')}</span>
          <Button
            type="button"
            variant="outline"
            size="sm"
            className="mt-2 w-fit"
            onClick={() => {
              void googleStatus.refetch();
              login.reset();
            }}
          >
            {t('retry')}
          </Button>
        </div>
      )}
      {invalid && !unavailable && (
        <div
          role="alert"
          id="login-error"
          className="flex items-start gap-3 rounded-lg border border-destructive px-4 py-3 text-sm text-destructive"
        >
          <AlertTriangleIcon aria-hidden="true" className="mt-0.5 size-4 shrink-0" />
          <span>{t(invalid)}</span>
        </div>
      )}
      {login.error && !invalid && !unavailable && (
        <p className="error" role="alert" id="login-error">
          {t('signInFailed')}
        </p>
      )}
    </>
  );
}

/** Email identifier field or the fixed local owner account with its toggle. */
function LoginIdentity({ vm }: { vm: LoginPageVm }) {
  const { t, presetIdentifier, emailMode, form, setEmailToggled } = vm;
  return emailMode ? (
    <div className="field">
      <Label htmlFor="identifier">{t('identifierLabel')}</Label>
      <Input
        id="identifier"
        type="email"
        autoComplete="username"
        autoFocus
        defaultValue={presetIdentifier ?? ''}
        placeholder={t('identifierPlaceholder')}
        {...form.register('identifier')}
      />
      <Button
        type="button"
        variant="link"
        size="sm"
        className="w-fit px-0"
        onClick={() => {
          setEmailToggled(false);
          form.setValue('identifier', '');
        }}
      >
        {t('useOwner')}
      </Button>
    </div>
  ) : (
    <div className="grid gap-1.5">
      <span className="text-[13px] font-semibold">{t('localAccount')}</span>
      <div className="flex min-h-[52px] items-center gap-3 rounded-[9px] border border-border px-3">
        <span aria-hidden="true" className="inline-flex size-8 items-center justify-center rounded-full bg-secondary text-primary">
          <UserIcon className="size-4" />
        </span>
        <strong className="text-sm font-semibold leading-tight">{t('ownerAccount')}</strong>
      </div>
      <Button type="button" variant="link" size="sm" className="w-fit px-0 text-foreground" onClick={() => setEmailToggled(true)}>
        {t('useEmail')}
      </Button>
    </div>
  );
}

/** Password field with inline required-error. */
function LoginPasswordField({ vm }: { vm: LoginPageVm }) {
  const { t, emailMode, form, login, passwordError } = vm;
  return (
    <div className="field">
      <Label htmlFor="password">{t('password')}</Label>
      <PasswordInput
        id="password"
        autoComplete="current-password"
        autoFocus={!emailMode}
        placeholder={t('passwordPlaceholder')}
        aria-invalid={passwordError || undefined}
        aria-describedby={login.error ? 'login-error' : undefined}
        {...form.register('password')}
      />
      {form.formState.errors.password && (
        <span className="error" role="alert">
          {t('enterPassword')}
        </span>
      )}
    </div>
  );
}

/** Google sign-in divider, button and failure notices. */
function GoogleSignIn({ vm }: { vm: LoginPageVm }) {
  const { t, googleError, googleLogin, showGoogle, googleOff } = vm;
  return (
    showGoogle && (
      <>
        <div className="text-muted-foreground flex items-center gap-3 text-[13px]" aria-hidden="true">
          <span className="h-px flex-1 bg-border" />
          <span>{t('orUseGoogle')}</span>
          <span className="h-px flex-1 bg-border" />
        </div>
        <div className="grid gap-2">
          {googleError && (
            <p className="error" role="alert">
              {t('googleFailed')}
            </p>
          )}
          <Button
            type="button"
            variant="outline"
            className="w-full"
            aria-describedby="google-help"
            disabled={googleOff || googleLogin.isPending}
            onClick={() => googleLogin.mutate()}
          >
            {googleLogin.isPending ? t('openingGoogle') : t('continueGoogle')}
          </Button>
          <p id="google-help" className="text-muted-foreground text-xs leading-snug">
            {t('googleHelp')}
          </p>
          {googleLogin.error && (
            <p className="error" role="alert">
              {t(apiFailureKey(googleLogin.error) ?? 'googleStartFailed')}
            </p>
          )}
        </div>
      </>
    )
  );
}

/** Reads the query string flags and the email-mode toggle and creates the sign-in form. */
function useLoginState() {
  const router = useRouter();
  const { setPreview, timezone, authGeneration } = useDisplayPreferences();
  const t = useTranslations('login');
  const search = new URLSearchParams(useSearch());
  const requestedLocale = search.get('lang');
  const googleError = search.get('google') === 'error';
  const expired = search.get('reason') === 'expired';
  const presetIdentifier = search.get('identifier');
  const setup = useQuery({
    queryKey: ['setup-status'],
    queryFn: () => apiRequest<{ setupRequired: boolean }>('/api/v1/auth/setup-status'),
  });
  const [emailToggled, setEmailToggled] = useState<boolean | null>(null);
  // The query string is empty during hydration, so the preset is derived on every render, not stored.
  const emailMode = emailToggled ?? presetIdentifier !== null;
  const form = useForm<LoginForm>({ resolver: zodResolver(loginSchema) });
  const googleStatus = useQuery({
    queryKey: ['auth-google-status'],
    queryFn: () => apiRequest<{ configured: boolean; linked: boolean }>('/api/v1/auth/google/status'),
  });

  useEffect(() => {
    if (setup.data?.setupRequired) router.replace('/setup');
  }, [router, setup.data?.setupRequired]);

  useEffect(() => {
    const locale = requestedLocale;
    if (locale !== 'en-us' && locale !== 'vi-vi') return;
    setPreview({ theme: 'system', locale, timezone }, authGeneration);
  }, [authGeneration, setPreview, timezone, requestedLocale]);

  return { router, t, search, googleError, expired, presetIdentifier, emailMode, form, googleStatus, emailToggled, setEmailToggled };
}

/** Declares the Google-status query and the Google-start and password-login mutations (one retry on 503). */
function useLoginRequests(p: ReturnType<typeof useLoginState>) {
  const { router, emailMode } = p;
  const googleLogin = useMutation({
    mutationFn: async () => {
      const csrf = await apiRequest<{ csrfToken: string }>('/api/v1/auth/csrf');
      return apiRequest<{ authorization_url: string }>('/api/v1/auth/google/start', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...csrfHeaders(csrf.csrfToken) },
        body: JSON.stringify({ purpose: 'login' }),
      });
    },
    onSuccess: ({ authorization_url }) => assignAuthorizationUrl(authorization_url),
  });
  const login = useMutation({
    mutationFn: async (values: LoginForm) => {
      const csrf = await apiRequest<{ csrfToken: string }>('/api/v1/auth/csrf');
      return apiRequest<{ authenticated: boolean }>('/api/v1/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...csrfHeaders(csrf.csrfToken) },
        // Omitting identifier keeps the bootstrap owner login; email mode sends the exact identifier.
        body: JSON.stringify({
          password: values.password,
          ...(emailMode && values.identifier?.trim() ? { identifier: values.identifier.trim() } : {}),
        }),
      });
    },
    onSuccess: () => router.replace('/dashboard'),
    // 503 + Retry-After means the owner row was briefly busy with a correct password: retry once, shortly.
    retry: (failureCount, error) => failureCount < 1 && error instanceof ApiError && error.status === 503,
    retryDelay: 1000,
  });

  return { ...p, googleLogin, login };
}

/** Derives service-unavailable, error-key and Google visibility flags from the request states. */
function deriveLoginFlags(p: ReturnType<typeof useLoginRequests>) {
  const { form, googleStatus, login } = p;
  const unavailable = (googleStatus.error !== null && !(googleStatus.error instanceof ApiError)) || isUnreachable(login.error);
  const invalid = login.error ? apiFailureKey(login.error) : null;
  const passwordError = Boolean(form.formState.errors.password) || invalid === 'unauthorized';
  const showGoogle = googleStatus.isSuccess && googleStatus.data.configured;
  const googleOff = unavailable;

  return { ...p, unavailable, invalid, passwordError, showGoogle, googleOff };
}
