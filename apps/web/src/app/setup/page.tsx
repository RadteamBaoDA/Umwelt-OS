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
import { Label } from '@/components/ui/label';

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

  return <main className="page"><section className="auth-panel" aria-labelledby="setup-title">
    <span className="brand">{t('brand')}</span><h1 id="setup-title">{t('title')}</h1><p className="muted">{t('description')}</p>
    <form className="form" onSubmit={form.handleSubmit((values) => setup.mutate(values))}>
      <div className="field"><Label htmlFor="setupToken">{t('setupToken')}</Label><Input id="setupToken" autoComplete="off" aria-invalid={Boolean(form.formState.errors.setupToken)} aria-describedby={form.formState.errors.setupToken ? 'setup-token-error' : undefined} {...form.register('setupToken')} />{form.formState.errors.setupToken && <span id="setup-token-error" className="error">{t('enterSetupToken')}</span>}</div>
      <div className="field"><Label htmlFor="password">{t('password')}</Label><Input id="password" type="password" autoComplete="new-password" aria-invalid={Boolean(form.formState.errors.password)} aria-describedby={form.formState.errors.password ? 'setup-password-help' : undefined} {...form.register('password')} />{form.formState.errors.password && <span id="setup-password-help" className="error">{t('passwordHelp')}</span>}</div>
      <div className="field"><Label htmlFor="confirmPassword">{t('confirmPassword')}</Label><Input id="confirmPassword" type="password" autoComplete="new-password" aria-invalid={Boolean(form.formState.errors.confirmPassword)} aria-describedby={form.formState.errors.confirmPassword ? 'setup-confirm-error' : undefined} {...form.register('confirmPassword')} />{form.formState.errors.confirmPassword && <span id="setup-confirm-error" className="error">{form.formState.errors.confirmPassword.message}</span>}</div>
      {setup.error && <p className="error" role="alert">{t('failure')}</p>}
      <Button type="submit" disabled={setup.isPending}>{setup.isPending ? t('creating') : t('createOwner')}</Button>
    </form>
  </section></main>;
}
