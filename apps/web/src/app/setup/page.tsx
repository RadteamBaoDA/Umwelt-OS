'use client';

import { zodResolver } from '@hookform/resolvers/zod';
import { useMutation } from '@tanstack/react-query';
import { useRouter } from 'next/navigation';
import { useTranslations } from 'next-intl';
import { useMemo } from 'react';
import { useForm } from 'react-hook-form';
import { z } from 'zod';
import { apiRequest, csrfHeaders } from '@/core/api';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { PasswordInput } from '@/components/ui/password-input';
import { Label } from '@/components/ui/label';
import { UmweltLogo } from '@/components/brand/umwelt-mark';
import { AppearanceControl } from '@/modules/account/appearance-control';

/** Build localized validation for first-run owner creation without changing its API contract. */
function createSetupSchema(passwordMismatch: string) {
  return z.object({
    setupToken: z.string().min(1),
    password: z.string().min(12).max(128),
    confirmPassword: z.string(),
  }).refine((value) => value.password === value.confirmPassword, {
    path: ['confirmPassword'],
    message: passwordMismatch,
  });
}

type SetupForm = z.infer<ReturnType<typeof createSetupSchema>>;

/** Renders localized first-run account bootstrap; resumable onboarding starts after authentication. */
export default function SetupPage() {
  const router = useRouter();
  const t = useTranslations('setup');
  const login = useTranslations('login');
  const setupSchema = useMemo(() => createSetupSchema(t('passwordMismatch')), [t]);
  const form = useForm<SetupForm>({ resolver: zodResolver(setupSchema) });
  const setup = useMutation({
    mutationFn: async (values: SetupForm) => {
      const csrf = await apiRequest<{ csrfToken: string }>('/api/v1/auth/csrf');
      return apiRequest<{ created: boolean }>('/api/v1/auth/setup', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...csrfHeaders(csrf.csrfToken), 'X-Setup-Token': values.setupToken },
        body: JSON.stringify({ password: values.password }),
      });
    },
    onSuccess: () => router.replace('/login'),
  });

  return <div className="flex min-h-dvh flex-col bg-secondary text-foreground">
    <header className="flex h-16 items-center gap-4 border-b border-border bg-background px-4 sm:px-6">
      <UmweltLogo />
      <span className="flex-1" />
      <AppearanceControl />
    </header>
    <main id="main-content" className="flex flex-1 items-center justify-center px-4 py-10 sm:py-12"><section className="auth-panel flex flex-col gap-6" aria-labelledby="setup-title">
    <div className="grid gap-1"><span className="brand">{t('brand')}</span><h1 id="setup-title" className="m-0 text-[30px] font-bold leading-[1.15] tracking-[-0.04em]">{t('title')}</h1><p className="text-muted-foreground m-0">{t('description')}</p></div>
    <form className="grid gap-4" onSubmit={form.handleSubmit((values) => setup.mutate(values))}>
      <div className="field"><Label htmlFor="setupToken">{t('setupToken')}</Label><Input id="setupToken" autoComplete="off" aria-invalid={Boolean(form.formState.errors.setupToken)} aria-describedby={form.formState.errors.setupToken ? 'setup-token-error' : undefined} {...form.register('setupToken')} />{form.formState.errors.setupToken && <span id="setup-token-error" className="error">{t('enterSetupToken')}</span>}</div>
      <div className="field"><Label htmlFor="password">{t('password')}</Label><PasswordInput id="password" autoComplete="new-password" aria-invalid={Boolean(form.formState.errors.password)} aria-describedby={form.formState.errors.password ? 'setup-password-help' : undefined} {...form.register('password')} />{form.formState.errors.password && <span id="setup-password-help" className="error">{t('passwordHelp')}</span>}</div>
      <div className="field"><Label htmlFor="confirmPassword">{t('confirmPassword')}</Label><PasswordInput id="confirmPassword" autoComplete="new-password" aria-invalid={Boolean(form.formState.errors.confirmPassword)} aria-describedby={form.formState.errors.confirmPassword ? 'setup-confirm-error' : undefined} {...form.register('confirmPassword')} />{form.formState.errors.confirmPassword && <span id="setup-confirm-error" className="error">{form.formState.errors.confirmPassword.message}</span>}</div>
      {setup.error && <p className="error" role="alert">{t('failure')}</p>}
      <Button type="submit" disabled={setup.isPending}>{setup.isPending ? t('creating') : t('createOwner')}</Button>
    </form>
  </section></main>
    <footer className="flex flex-wrap items-center gap-4 border-t border-border bg-background px-4 py-2.5 text-xs text-muted-foreground sm:px-6">
      <span>{login('noDataBeforeSignIn')}</span>
    </footer>
  </div>;
}
