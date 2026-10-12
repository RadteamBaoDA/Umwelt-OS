'use client';

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import Link from 'next/link';
import { useState } from 'react';
import { useTranslations } from 'next-intl';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Switch } from '@/components/ui/switch';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import { apiRequest, csrfHeaders } from '@/core/api';
import { apiFailureKey } from '@/core/api-failure-key';
import { useWorkspaceSession } from '@/core/app-shell/workspace-shell';
import { formatDateTime } from '@/core/i18n';
import { useDisplayPreferences } from '@/core/query-provider';
import { deactivateConnector, triggerCollection } from '@/modules/sources/api';
import { SecretField, SettingsActions, SettingsPageHeader, SettingsSection } from './settings-layout';

type Source = 'database' | 'environment' | 'none';
type N8n = { enabled: boolean; callback_base_url: string; configuration_revision: number; service_url: string | null; api_key_configured: boolean; webhook_token_configured: boolean; service_url_source: Source; api_key_source: Source; webhook_token_source: Source };
type TestResult = { ok: boolean; error: 'credential_required' | 'unreachable' | 'unauthorized' | 'http_error' | null; detail: number | null };
type Workflow = { source_id: string; source_name: string; source_type: string; provider: string | null; workflow_id: string | null; state: string; error_code: string | null; transition_phase: string | null; last_success_at: string | null; last_error_at: string | null; last_error_code: string | null; collected_at: string | null; next_due_at: string | null };
type Secret = { value: string; removed: boolean };
const empty: Secret = { value: '', removed: false };
/** Provisioning states the backend can report (ck_connector_provisioning_state). */
const STATES = new Set(['queued', 'provisioning', 'active', 'saved_not_active', 'reconciliation_required', 'disabled']);
const action = (secret: Secret) => (secret.value ? 'replaced' : secret.removed ? 'removed' : 'unchanged');

/** Lists provisioned n8n workflows with per-source deactivate and collect-now actions; actions are disabled while the integration is off. */
function WorkflowsSection({ enabled }: { enabled: boolean }) {
  const t = useTranslations('n8n');
  const { csrfToken } = useWorkspaceSession();
  const display = useDisplayPreferences();
  const client = useQueryClient();
  const query = useQuery({ queryKey: ['n8n-workflows'], queryFn: () => apiRequest<Workflow[]>('/api/v1/connectors/n8n/workflows') });
  const act = useMutation({
    mutationFn: async ({ id, kind }: { id: string; kind: 'deactivate' | 'collect' }): Promise<unknown> => kind === 'deactivate' ? deactivateConnector(id, csrfToken) : triggerCollection(id, csrfToken),
    // Refetch even on failure so the table reflects whatever state the backend actually reached.
    onSettled: () => client.invalidateQueries({ queryKey: ['n8n-workflows'] }),
  });
  const rows = query.data;
  /** Translates a backend provisioning state, falling back to the raw value for unknown states. */
  const stateLabel = (state: string) => (STATES.has(state) ? t(`state_${state}` as 'state_active') : state);
  return <SettingsSection title={t('workflows')}>
    {query.isPending ? <div className="skeleton h-16" role="status" aria-label={t('workflowsLoading')} />
      : query.isError || !rows ? <div className="flex flex-wrap items-center gap-3"><p className="error m-0" role="alert">{t(apiFailureKey(query.error) ?? 'workflowsLoadFailed')}</p><Button type="button" className="secondary" onClick={() => query.refetch()}>{t('retry')}</Button></div>
      : rows.length === 0 ? <p className="m-0 text-sm text-muted-foreground">{t('workflowsEmpty')}</p>
      : <Table>
        <TableHeader><TableRow><TableHead>{t('colName')}</TableHead><TableHead>{t('colType')}</TableHead><TableHead>{t('colState')}</TableHead><TableHead>{t('colLastSuccess')}</TableHead><TableHead>{t('colLastError')}</TableHead><TableHead>{t('colActions')}</TableHead></TableRow></TableHeader>
        <TableBody>{rows.map((row) => <TableRow key={row.source_id}>
          <TableCell>{row.source_name}</TableCell>
          <TableCell>{row.source_type}{row.provider ? ` / ${row.provider}` : ''}</TableCell>
          <TableCell><Badge variant="outline">{stateLabel(row.state)}</Badge></TableCell>
          <TableCell>{row.last_success_at ? formatDateTime(row.last_success_at, display.locale, display.timezone) : t('never')}</TableCell>
          <TableCell>{row.last_error_code ?? '-'}</TableCell>
          <TableCell><div className="flex flex-wrap gap-2">
            <Button type="button" className="secondary" disabled={!enabled || act.isPending} onClick={() => act.mutate({ id: row.source_id, kind: 'collect' })}>{t('collectNow')}</Button>
            <Button type="button" className="secondary" disabled={!enabled || act.isPending} onClick={() => act.mutate({ id: row.source_id, kind: 'deactivate' })}>{t('deactivate')}</Button>
            <Button asChild className="secondary"><Link href={`/settings/sources?source=${encodeURIComponent(row.source_id)}`}>{t('openSource')}</Link></Button>
          </div></TableCell>
        </TableRow>)}</TableBody>
      </Table>}
    {act.isError ? <p className="error m-0" role="alert">{t(apiFailureKey(act.error) ?? 'actionFailed')}</p> : null}
  </SettingsSection>;
}

/** Owner n8n connection settings: service URL, API key and webhook token with source badges and a connection test. */
export function N8nSettingsWorkspace() {
  const t = useTranslations('n8n');
  const { csrfToken } = useWorkspaceSession();
  const client = useQueryClient();
  const [url, setUrl] = useState<string | null>(null);
  const [enabled, setEnabled] = useState<boolean | null>(null);
  const [copied, setCopied] = useState<'idle' | 'ok' | 'failed'>('idle');
  const [apiKey, setApiKey] = useState<Secret>(empty);
  const [token, setToken] = useState<Secret>(empty);
  const query = useQuery({ queryKey: ['n8n-settings'], queryFn: () => apiRequest<N8n>('/api/v1/settings/n8n') });
  const saved = query.data;
  const currentUrl = url ?? saved?.service_url ?? '';
  const currentEnabled = enabled ?? saved?.enabled ?? false;
  const reset = () => { setUrl(null); setEnabled(null); setApiKey(empty); setToken(empty); };
  const headers = { ...csrfHeaders(csrfToken), 'Content-Type': 'application/json' };
  const save = useMutation({
    mutationFn: (value: N8n) => apiRequest<N8n>('/api/v1/settings/n8n', {
      method: 'PUT', headers,
      body: JSON.stringify({
        expected_revision: value.configuration_revision, enabled: currentEnabled, service_url: currentUrl.trim() || null,
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
  const dirty = enabled !== null && enabled !== saved.enabled || url !== null && url !== (saved.service_url ?? '') || action(apiKey) !== 'unchanged' || action(token) !== 'unchanged';
  const result = test.data;
  /** Copies the callback base URL; clipboard may be unavailable or denied, so failure is surfaced instead of thrown. */
  const copy = async () => { try { await navigator.clipboard.writeText(saved.callback_base_url); setCopied('ok'); } catch { setCopied('failed'); } };
  const source = (value: Source) => <Badge variant="outline">{t(`source_${value}` as 'source_none')}</Badge>;
  const state = (configured: boolean) => `${t('status')} ${configured ? t('configured') : t('notConfigured')}. ${t('neverShown')}`;
  return <div className="flex flex-col gap-5">
    <SettingsPageHeader eyebrow={t('eyebrow')} title={t('title')} description={t('description')} status={<p className="m-0 text-sm text-muted-foreground" role="status">{save.isPending ? t('saving') : dirty ? t('unsaved') : t('saved')}</p>} />
    <form className="flex flex-col gap-5" onSubmit={(event) => { event.preventDefault(); save.mutate(saved); }}>
      <SettingsSection title={t('integration')}>
        <label className="flex items-center gap-3"><Switch checked={currentEnabled} disabled={save.isPending} onCheckedChange={setEnabled} aria-label={t('enabledLabel')} /><span className="label">{t('enabledLabel')}</span></label>
        {currentEnabled ? null : <p className="m-0 text-sm text-muted-foreground" role="note">{t('disabledWarning')}</p>}
      </SettingsSection>
      <SettingsSection title={t('connectionTitle')}>
        <div className="flex flex-wrap items-center gap-2">
          <Input readOnly aria-label={t('callbackBaseUrl')} value={saved.callback_base_url} className="max-w-md" />
          <Button type="button" className="secondary" onClick={copy}>{t('copy')}</Button>
          {copied === 'idle' ? null : <span className="text-sm text-muted-foreground" role="status">{copied === 'ok' ? t('copied') : t('copyFailed')}</span>}
        </div>
        <p className="m-0 text-xs text-muted-foreground">{t('webhookToken')}: {state(saved.webhook_token_configured)}</p>
        <p className="m-0 text-xs text-muted-foreground" role="note">{t('collectorNote')}</p>
      </SettingsSection>
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
    <WorkflowsSection enabled={saved.enabled} />
  </div>;
}
