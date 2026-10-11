'use client';

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import { useTranslations } from 'next-intl';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { Input } from '@/components/ui/input';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { apiRequest, csrfHeaders } from '@/core/api';
import { apiFailureKey } from '@/core/api-failure-key';
import { useWorkspaceSession } from '@/core/app-shell/workspace-shell';
import { CheckboxField, FieldGrid, SecretField, SettingsActions, SettingsPageHeader, SettingsSection } from './settings-layout';

type Provider = 'none' | 'tavily' | 'brave' | 'searxng';
type WebSearch = { configuration_revision: number; provider: Provider; endpoint: string | null; credential_configured: boolean; allow_remote_web_search: boolean; destination_id: string | null };
type TestResult = { ok: boolean; error: 'credential_required' | 'network_denied' | 'provider_error' | 'timeout' | null; detail: number | null };
type Draft = Partial<Pick<WebSearch, 'provider' | 'endpoint' | 'allow_remote_web_search'>>;

/** Owner web-search provider settings (Tavily, Brave or SearXNG) with a write-only API key and a connection test. */
export function WebSearchSettingsWorkspace() {
  const t = useTranslations('webSearch');
  const { csrfToken } = useWorkspaceSession();
  const client = useQueryClient();
  const [draft, setDraft] = useState<Draft>({});
  const [key, setKey] = useState('');
  const [removed, setRemoved] = useState(false);
  const query = useQuery({ queryKey: ['web-search-settings'], queryFn: () => apiRequest<WebSearch>('/api/v1/settings/web-search') });
  const reset = () => { setDraft({}); setKey(''); setRemoved(false); };
  const post = { method: 'POST', headers: { ...csrfHeaders(csrfToken), 'Content-Type': 'application/json' } } as const;
  const saved = query.data;
  const view = saved ? { ...saved, ...draft } : undefined;
  const needsKey = view?.provider === 'tavily' || view?.provider === 'brave';
  const save = useMutation({
    mutationFn: (value: WebSearch & Draft) => apiRequest<WebSearch>('/api/v1/settings/web-search', {
      method: 'PUT', headers: { ...csrfHeaders(csrfToken), 'Content-Type': 'application/json' },
      body: JSON.stringify({
        expected_revision: value.configuration_revision, provider: value.provider, endpoint: value.provider === 'none' ? null : value.endpoint || null,
        credential_action: key ? 'replaced' : removed ? 'removed' : 'unchanged', api_key: key || null, allow_remote_web_search: value.allow_remote_web_search,
      }),
    }),
    onSuccess: (value) => { reset(); test.reset(); client.setQueryData(['web-search-settings'], value); },
  });
  const test = useMutation({
    mutationFn: (value: WebSearch & Draft) => apiRequest<TestResult>('/api/v1/settings/web-search/test', { ...post, body: JSON.stringify({ provider: value.provider, endpoint: value.endpoint || null, api_key: key }) }),
  });
  if (query.isPending) return <section className="content-panel skeleton" aria-label={t('loading')} />;
  if (query.isError || !saved || !view) return <section className="content-panel"><h1>{t('unavailable')}</h1><p className="error">{t(apiFailureKey(query.error) ?? 'loadFailed')}</p><Button className="secondary" onClick={() => query.refetch()}>{t('retry')}</Button></section>;
  const dirty = Object.keys(draft).length > 0 || Boolean(key) || removed;
  const keyReady = !needsKey || key !== '' || (saved.credential_configured && !removed);
  const valid = view.provider === 'none' || (Boolean(view.endpoint) && keyReady);
  const set = (patch: Draft) => setDraft((current) => ({ ...current, ...patch }));
  const result = test.data;
  return <div className="flex flex-col gap-5">
    <SettingsPageHeader eyebrow={t('eyebrow')} title={t('title')} description={t('description')} status={<p className="m-0 text-sm text-muted-foreground" role="status">{save.isPending ? t('saving') : dirty ? t('unsaved') : t('saved')}</p>} />
    <form className="flex flex-col gap-5" onSubmit={(event) => { event.preventDefault(); save.mutate(view); }}>
      <SettingsSection title={t('provider')}>
        <FieldGrid>
          <label className="field"><span className="label">{t('provider')}</span><Select disabled={save.isPending} value={view.provider} onValueChange={(provider) => { set({ provider: provider as Provider }); setKey(''); setRemoved(false); }}><SelectTrigger aria-label={t('provider')}><SelectValue /></SelectTrigger><SelectContent><SelectItem value="none">{t('none')}</SelectItem><SelectItem value="tavily">Tavily</SelectItem><SelectItem value="brave">Brave</SelectItem><SelectItem value="searxng">SearXNG</SelectItem></SelectContent></Select></label>
          {view.provider !== 'none' ? <label className="field"><span className="label">{t('endpoint')}</span><Input type="url" disabled={save.isPending} value={view.endpoint ?? ''} onChange={(event) => set({ endpoint: event.target.value || null })} /></label> : null}
        </FieldGrid>
        {needsKey ? <SecretField id="web-search-key" label={t('apiKey')} value={key} onValue={(value) => { setKey(value); if (value) setRemoved(false); }} removed={removed} onRemoved={(next) => { setRemoved(next); if (next) setKey(''); }} configured={saved.credential_configured} disabled={save.isPending} placeholder={t('keepSaved')} removeLabel={t('removeKey')} status={<>{t('keyStatus')} {saved.credential_configured ? t('configured') : t('notConfigured')}. {t('keyNeverShown')} {t('keyRequired')}</>} /> : null}
        <CheckboxField><Checkbox id="web-search-allow" disabled={save.isPending} checked={view.allow_remote_web_search} onCheckedChange={(checked) => set({ allow_remote_web_search: checked === true })} /><label htmlFor="web-search-allow">{t('allowRemote')}</label></CheckboxField>
        <p className="m-0 text-xs text-muted-foreground">{t('envNote')}</p>
        {view.provider !== 'none' ? <div className="flex flex-wrap items-center gap-3">
          <Button type="button" className="secondary" disabled={test.isPending || !view.endpoint} onClick={() => test.mutate(view)}>{test.isPending ? t('testing') : t('test')}</Button>
          {result ? <p className={result.ok ? 'm-0 text-sm text-muted-foreground' : 'error m-0'} role="status">{result.ok ? (result.detail == null ? t('testOkNoCount') : t('testOk', { count: result.detail })) : t(`testError_${result.error ?? 'unknown'}` as 'testError_unknown')}</p> : null}
          {test.isError ? <p className="error m-0" role="alert">{t(apiFailureKey(test.error) ?? 'requestFailed')}</p> : null}
        </div> : null}
      </SettingsSection>
      {save.isError ? <p className="error" role="alert">{t(apiFailureKey(save.error) ?? 'saveFailed')}</p> : null}
      <SettingsActions>
        <Button type="button" className="secondary" disabled={save.isPending || !dirty} onClick={() => { reset(); save.reset(); test.reset(); }}>{t('cancelChanges')}</Button>
        <Button type="submit" disabled={save.isPending || !dirty || !valid}>{t('saveButton')}</Button>
      </SettingsActions>
    </form>
  </div>;
}
