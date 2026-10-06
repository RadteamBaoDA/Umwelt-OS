'use client';

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useState, useSyncExternalStore } from 'react';
import { useTranslations } from 'next-intl';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { apiRequest, csrfHeaders } from '@/core/api';
import { apiFailureKey } from '@/core/api-failure-key';
import { useWorkspaceSession } from '@/core/app-shell/workspace-shell';

type GoogleStatus = { configured: boolean; linked: boolean };

/** Renders the Google account connection state and its supported linking action. */
export function GoogleLink() {
  const t = useTranslations('account');
  const { csrfToken } = useWorkspaceSession();
  const queryClient = useQueryClient();
  const [password, setPassword] = useState('');
  const providerError = useSyncExternalStore(() => () => undefined, () => new URLSearchParams(window.location.search).get('google') === 'error', () => false);
  const status = useQuery({ queryKey: ['auth-google-status'], queryFn: () => apiRequest<GoogleStatus>('/api/v1/auth/google/status') });
  const link = useMutation({
    mutationFn: async () => {
      await apiRequest<void>('/api/v1/auth/reauthenticate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...csrfHeaders(csrfToken) },
        body: JSON.stringify({ password }),
      });
      return apiRequest<{ authorization_url: string }>('/api/v1/auth/google/start', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...csrfHeaders(csrfToken) },
        body: JSON.stringify({ purpose: 'link' }),
      });
    },
    onSuccess: ({ authorization_url }) => window.location.assign(authorization_url),
  });
  const unlink = useMutation({
    mutationFn: async () => {
      await apiRequest<void>('/api/v1/auth/reauthenticate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...csrfHeaders(csrfToken) },
        body: JSON.stringify({ password }),
      });
      return apiRequest<void>('/api/v1/auth/google/unlink', { method: 'POST', headers: csrfHeaders(csrfToken) });
    },
    onSuccess: () => {
      setPassword('');
      queryClient.invalidateQueries({ queryKey: ['auth-google-status'] });
    },
  });
  const error = link.error ?? unlink.error;

  return <section className="status-panel">
    <h2>{t('googleSignIn')}</h2>
    {status.isPending && <p className="muted" role="status">{t('checkingGoogle')}</p>}
    {status.isError && <p className="error" role="alert">{t('unavailable')}</p>}
    {status.isSuccess && !status.data.configured && <p className="muted">{t('unconfigured')}</p>}
    {status.isSuccess && status.data.configured && <>
      <p className="muted">{status.data.linked ? t('linked') : t('unlinked')}</p>
      {providerError && <p className="error" role="alert">{t('linkFailed')}</p>}
      <p className="muted">{t('scopeHelp')}</p>
      <div className="field"><Label htmlFor="google-password">{t('confirmPassword')}</Label><Input id="google-password" type="password" autoComplete="current-password" value={password} onChange={(event) => setPassword(event.target.value)} /></div>
      {status.data.linked
        ? <Button type="button" className="secondary" disabled={!password || unlink.isPending} onClick={() => unlink.mutate()}>{unlink.isPending ? t('unlinking') : t('unlinkGoogle')}</Button>
        : <Button type="button" disabled={!password || link.isPending} onClick={() => link.mutate()}>{link.isPending ? t('linking') : t('linkGoogle')}</Button>}
      {error && <p className="error" role="alert">{t(apiFailureKey(error) ?? 'changeFailed')}</p>}
    </>}
  </section>;
}
