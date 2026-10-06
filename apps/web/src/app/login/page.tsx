'use client';

import { zodResolver } from '@hookform/resolvers/zod';
import { useMutation, useQuery } from '@tanstack/react-query';
import { useRouter } from 'next/navigation';
import { useTranslations } from 'next-intl';
import { useEffect, useState } from 'react';
import { useForm } from 'react-hook-form';
import { z } from 'zod';
import { apiRequest, csrfHeaders } from '@/core/api';
import { apiFailureKey } from '@/core/api-failure-key';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';

const loginSchema = z.object({ password: z.string().min(1).max(128) });
type LoginForm = z.infer<typeof loginSchema>;

/** Renders sign-in controls and reports authentication state to the user. */
export default function LoginPage() {
  const router = useRouter();
  const t = useTranslations('login');
  const [googleError, setGoogleError] = useState(false);
  useEffect(() => setGoogleError(new URLSearchParams(window.location.search).get('google') === 'error'), []);
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

  return <main className="page"><section className="auth-panel"><span className="brand">Umwelt-OS</span><h1>{t('welcome')}</h1><p className="muted">{t('privateWorkspace')}</p><form className="form" onSubmit={form.handleSubmit((values) => login.mutate(values))}>
    <div className="field"><Label htmlFor="password">{t('password')}</Label><Input id="password" type="password" autoComplete="current-password" autoFocus {...form.register('password')} />{form.formState.errors.password && <span className="error">{t('enterPassword')}</span>}</div>
    {login.error && <p className="error" role="alert">{t(apiFailureKey(login.error) ?? 'signInFailed')}</p>}
    <Button type="submit" disabled={login.isPending}>{login.isPending ? t('signingIn') : t('signIn')}</Button>
  </form>
  {googleStatus.isPending && <p className="muted" role="status">{t('checkingGoogle')}</p>}
  {googleStatus.isError && <p className="error" role="alert">{t('googleUnknown')}</p>}
  {googleStatus.isSuccess && !googleStatus.data.configured && <p className="muted">{t('googleUnconfigured')}</p>}
  {googleStatus.isSuccess && googleStatus.data.configured && <div className="form"><p className="muted">{t('orGoogle')}</p>
    {googleError && <p className="error" role="alert">{t('googleFailed')}</p>}
    <Button className="secondary" type="button" disabled={googleLogin.isPending} onClick={() => googleLogin.mutate()}>{googleLogin.isPending ? t('openingGoogle') : t('continueGoogle')}</Button>
    {googleLogin.error && <p className="error" role="alert">{t(apiFailureKey(googleLogin.error) ?? 'googleStartFailed')}</p>}
  </div>}
  </section></main>;
}
