'use client';

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import { useTranslations } from 'next-intl';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { apiRequest, csrfHeaders } from '@/core/api';
import { apiFailureKey } from '@/core/api-failure-key';
import { useWorkspaceSession } from '@/core/app-shell/workspace-shell';
import { SecretField, SettingsActions, SettingsPageHeader, SettingsSection } from './settings-layout';

type Source = 'database' | 'environment' | 'none';
type N8n = { configuration_revision: number; service_url: string | null; api_key_configured: boolean; webhook_token_configured: boolean; service_url_source: Source; api_key_source: Source; webhook_token_source: Source };
type TestResult = { ok: boolean; error: 'credential_required' | 'unreachable' | 'unauthorized' | 'http_error' | null; detail: number | null };
type Secret = { value: string; removed: boolean };
const empty: Secret = { value: '', removed: false };
const action = (secret: Secret) => (secret.value ? 'replaced' : secret.removed ? 'removed' : 'unchanged');

/** Owner n8n connection settings: service URL, API key and webhook token with source badges and a connection test. */
export function N8nSettingsWorkspace() {
  const t = useTranslations('n8n');
  const { csrfToken } = useWorkspaceSession();
  const client = useQueryClient();
  const [url, setUrl] = useState<string | null>(null);
  const [apiKey, setApiKey] = useState<Secret>(empty);
  const [token, setToken] = useState<Secret>(empty);
  const query = useQuery({ queryKey: ['n8n-settings'], queryFn: () => apiRequest<N8n>('/api/v1/settings/n8n') });
  const saved = query.data;
  const currentUrl = url ?? saved?.service_url ?? '';
  const reset = () => { setUrl(null); setApiKey(empty); setToken(empty); };
  const headers = { ...csrfHeaders(csrfToken), 'Content-Type': 'application/json' };
  const save = useMutation({
    mutationFn: (value: N8n) => apiRequest<N8n>('/api/v1/settings/n8n', {
      method: 'PUT', headers,
      body: JSON.stringify({
        expected_revision: value.configuration_revision, service_url: currentUrl.trim() || null,
        api_key_action: action(apiKey), api_key: apiKey.value || null, webhook_token_action: action(token), webhook_token: token.value || null,
      }),
    }),
    onSuccess: (value) => { reset(); test.reset(); client.setQueryData(['n8n-settings'], value); },
  });
  const test = useMutation({
    mutationFn: () => apiRequest<TestResult>('/api/v1/settings/n8n/test', { method: 'POST', headers, body: JSON.stringify({ service_url: currentUrl.trim() || null, api_key: apiKey.value }) }),
  });
  if (query.isPending) return <section className="content-panel skeleton" aria-label={t('loading')} />;
  if (query.isError || !saved) return <section className="content-panel"><h1>{t('unavailable')}</h1><p className="error">{t(apiFailureKey(query.error) ?? 'loadFailed')}</p><Button className="secondary" onClick={() => query.refetch()}>{t('retry')}</Button></section>;
  const dirty = url !== null && url !== (saved.service_url ?? '') || action(apiKey) !== 'unchanged' || action(token) !== 'unchanged';
  const result = test.data;
  const source = (value: Source) => <Badge variant="outline">{t(`source_${value}` as 'source_none')}</Badge>;
  const state = (configured: boolean) => `${t('status')} ${configured ? t('configured') : t('notConfigured')}. ${t('neverShown')}`;
  return <div className="flex flex-col gap-5">
    <SettingsPageHeader eyebrow={t('eyebrow')} title={t('title')} description={t('description')} status={<p className="m-0 text-sm text-muted-foreground" role="status">{save.isPending ? t('saving') : dirty ? t('unsaved') : t('saved')}</p>} />
    <form className="flex flex-col gap-5" onSubmit={(event) => { event.preventDefault(); save.mutate(saved); }}>
      <SettingsSection title={<span className="flex flex-wrap items-center gap-2">{t('serviceUrl')}{source(saved.service_url_source)}</span>}>
        <label className="field"><span className="label">{t('serviceUrl')}</span><Input type="url" disabled={save.isPending} value={currentUrl} onChange={(event) => setUrl(event.target.value)} /></label>
        <p className="m-0 text-xs text-muted-foreground">{t('serviceUrlHelp')}</p>
      </SettingsSection>
      <SettingsSection title={<span className="flex flex-wrap items-center gap-2">{t('apiKey')}{source(saved.api_key_source)}</span>}>
        <SecretField id="n8n-api-key" label={t('apiKey')} value={apiKey.value} onValue={(value) => setApiKey({ value, removed: value ? false : apiKey.removed })} removed={apiKey.removed} onRemoved={(removed) => setApiKey({ value: removed ? '' : apiKey.value, removed })} configured={saved.api_key_configured} disabled={save.isPending} placeholder={t('keepSaved')} removeLabel={t('removeApiKey')} status={state(saved.api_key_configured)} />
        <div className="flex flex-wrap items-center gap-3">
          <Button type="button" className="secondary" disabled={test.isPending} onClick={() => test.mutate()}>{test.isPending ? t('testing') : t('test')}</Button>
          {result ? <p className={result.ok ? 'm-0 text-sm text-muted-foreground' : 'error m-0'} role="status">{result.ok ? t('testOk') : t(`testError_${result.error ?? 'unknown'}` as 'testError_unknown', { code: result.detail ?? '' })}</p> : null}
          {test.isError ? <p className="error m-0" role="alert">{t(apiFailureKey(test.error) ?? 'requestFailed')}</p> : null}
        </div>
      </SettingsSection>
      <SettingsSection title={<span className="flex flex-wrap items-center gap-2">{t('webhookToken')}{source(saved.webhook_token_source)}</span>}>
        <SecretField id="n8n-webhook-token" label={t('webhookToken')} value={token.value} onValue={(value) => setToken({ value, removed: value ? false : token.removed })} removed={token.removed} onRemoved={(removed) => setToken({ value: removed ? '' : token.value, removed })} configured={saved.webhook_token_configured} disabled={save.isPending} placeholder={t('keepSaved')} removeLabel={t('removeWebhookToken')} status={state(saved.webhook_token_configured)} />
        <p className="m-0 text-xs text-muted-foreground" role="note">{t('tokenWarning')}</p>
      </SettingsSection>
      {save.isError ? <p className="error" role="alert">{t(apiFailureKey(save.error) ?? 'saveFailed')}</p> : null}
      <SettingsActions>
        <Button type="button" className="secondary" disabled={save.isPending || !dirty} onClick={() => { reset(); save.reset(); test.reset(); }}>{t('cancelChanges')}</Button>
        <Button type="submit" disabled={save.isPending || !dirty}>{t('saveButton')}</Button>
      </SettingsActions>
    </form>
  </div>;
}
