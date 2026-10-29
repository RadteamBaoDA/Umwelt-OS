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

type Provider = 'tavily' | 'brave' | 'searxng' | 'omniroute';
type Entry = { provider: Provider; endpoint: string | null; credential_configured: boolean; destination_id: string | null };
type WebSearch = { configuration_revision: number; providers: Entry[]; allow_remote_web_search: boolean };
type DraftEntry = Entry & { key: string; removed: boolean };
type Draft = { configuration_revision: number; providers: DraftEntry[]; allow_remote_web_search: boolean };
type TestResult = { ok: boolean; error: 'credential_required' | 'network_denied' | 'provider_error' | 'timeout' | null; detail: number | null };

/** Edit ordered web-search providers and their write-only credentials under one remote-search consent. */
export function WebSearchSettingsWorkspace() {
  const t = useTranslations('webSearch');
  const { csrfToken } = useWorkspaceSession();
  const client = useQueryClient();
  const [draft, setDraft] = useState<Draft | null>(null);
  const [testedIndex, setTestedIndex] = useState<number | null>(null);
  const query = useQuery({ queryKey: ['web-search-settings'], queryFn: () => apiRequest<WebSearch>('/api/v1/settings/web-search') });
  const saved = query.data;
  // Keep the revision observed at edit start even if the query refreshes in the background.
  const view = draft ?? (saved ? { configuration_revision: saved.configuration_revision, providers: saved.providers.map((entry) => ({ ...entry, key: '', removed: false })), allow_remote_web_search: saved.allow_remote_web_search } : null);
  const save = useMutation({
    mutationFn: (value: Draft) => apiRequest<WebSearch>('/api/v1/settings/web-search', {
      method: 'PUT', headers: { ...csrfHeaders(csrfToken), 'Content-Type': 'application/json' },
      body: JSON.stringify({ expected_revision: value.configuration_revision, providers: value.providers.map((entry) => ({
        provider: entry.provider, endpoint: entry.endpoint || null, credential_action: entry.key ? 'replaced' : entry.removed ? 'removed' : 'unchanged', api_key: entry.key || null,
      })), allow_remote_web_search: value.allow_remote_web_search }),
    }),
    onSuccess: (value) => { setDraft(null); setTestedIndex(null); test.reset(); client.setQueryData(['web-search-settings'], value); void client.invalidateQueries({ queryKey: ['ai-settings'] }); },
  });
  const test = useMutation({
    mutationFn: (entry: DraftEntry) => apiRequest<TestResult>('/api/v1/settings/web-search/test', {
      method: 'POST', headers: { ...csrfHeaders(csrfToken), 'Content-Type': 'application/json' },
      body: JSON.stringify({ provider: entry.provider, endpoint: entry.endpoint || null, api_key: entry.key }),
    }),
  });
  if (query.isPending) return <section className="rounded-xl border bg-card p-6 skeleton" aria-label={t('loading')} />;
  if (query.isError || !saved || !view) return <section className="rounded-xl border bg-card p-6"><h1>{t('unavailable')}</h1><p className="error">{t(apiFailureKey(query.error) ?? 'loadFailed')}</p><Button variant="outline" onClick={() => query.refetch()}>{t('retry')}</Button></section>;
  /** Resolve saved credential status by endpoint so reordering does not move secrets between targets. */
  const configured = (entry: DraftEntry) => saved.providers.some((item) => item.endpoint === entry.endpoint && item.credential_configured);
  /** Check whether this draft can use a new or retained credential, or is keyless. */
  const keyReady = (entry: DraftEntry) => entry.provider === 'searxng' || Boolean(entry.key) || (configured(entry) && !entry.removed);
  const valid = view.providers.every((entry) => Boolean(entry.endpoint) && keyReady(entry));
  /** Replace the form draft and discard test results for the previous configuration. */
  const change = (next: Draft) => { setDraft(next); setTestedIndex(null); test.reset(); };
  /** Update one provider while keeping the other ordered entries intact. */
  const changeEntry = (index: number, patch: Partial<DraftEntry>) => change({ ...view, providers: view.providers.map((entry, i) => i === index ? { ...entry, ...patch } : entry) });
  /** Swap adjacent providers together with their unsaved secret state. */
  const move = (index: number, offset: number) => {
    const providers = [...view.providers];
    [providers[index], providers[index + offset]] = [providers[index + offset], providers[index]];
    change({ ...view, providers });
  };
  return <div className="flex flex-col gap-5">
    <SettingsPageHeader eyebrow={t('eyebrow')} title={t('title')} description={t('description')} status={<p className="m-0 text-sm text-muted-foreground" role="status">{save.isPending ? t('saving') : draft ? t('unsaved') : t('saved')}</p>} />
    <form className="flex flex-col gap-5" onSubmit={(event) => { event.preventDefault(); save.mutate(view); }}>
      <SettingsSection title={t('provider')}>
        <p className="m-0 text-sm text-muted-foreground">{t('fallbackNote')}</p>
        {view.providers.map((entry, index) => <div key={index} className="flex flex-col gap-3 rounded-lg border p-4">
          <FieldGrid>
            <label className="field"><span className="label">{t('provider')}</span><Select disabled={save.isPending} value={entry.provider} onValueChange={(provider) => changeEntry(index, { provider: provider as Provider })}><SelectTrigger aria-label={t('providerAt', { number: index + 1 })}><SelectValue /></SelectTrigger><SelectContent><SelectItem value="tavily">Tavily</SelectItem><SelectItem value="brave">Brave</SelectItem><SelectItem value="searxng">SearXNG</SelectItem><SelectItem value="omniroute">OmniRoute</SelectItem></SelectContent></Select></label>
            <label className="field"><span className="label">{t('endpoint')}</span><Input type="url" disabled={save.isPending} value={entry.endpoint ?? ''} placeholder={entry.provider === 'omniroute' ? 'https://<host>/v1' : undefined} onChange={(event) => changeEntry(index, { endpoint: event.target.value || null })} /></label>
          </FieldGrid>
          {entry.provider !== 'searxng' ? <SecretField id={`web-search-key-${index}`} label={t('apiKey')} value={entry.key} onValue={(key) => changeEntry(index, { key, ...(key ? { removed: false } : {}) })} removed={entry.removed} onRemoved={(removed) => changeEntry(index, { removed, ...(removed ? { key: '' } : {}) })} configured={configured(entry)} disabled={save.isPending} placeholder={t('keepSaved')} removeLabel={t('removeKey')} status={<>{t('keyStatus')} {configured(entry) ? t('configured') : t('notConfigured')}. {t('keyNeverShown')} {t('keyRequired')}</>} /> : null}
          <div className="flex flex-wrap items-center gap-3">
            <Button type="button" variant="outline" disabled={test.isPending || save.isPending || !entry.endpoint || !keyReady(entry)} onClick={() => { setTestedIndex(index); test.mutate(entry); }}>{test.isPending && testedIndex === index ? t('testing') : t('test')}</Button>
            <Button type="button" variant="outline" disabled={save.isPending || index === 0} onClick={() => move(index, -1)} aria-label={t('moveUpAt', { number: index + 1 })}>{t('moveUp')}</Button>
            <Button type="button" variant="outline" disabled={save.isPending || index === view.providers.length - 1} onClick={() => move(index, 1)} aria-label={t('moveDownAt', { number: index + 1 })}>{t('moveDown')}</Button>
            <Button type="button" variant="outline" disabled={save.isPending} onClick={() => change({ ...view, providers: view.providers.filter((_, i) => i !== index) })} aria-label={t('removeAt', { number: index + 1 })}>{t('removeProvider')}</Button>
            {testedIndex === index && test.data ? <p className={test.data.ok ? 'm-0 text-sm text-muted-foreground' : 'error m-0'} role="status">{test.data.ok ? (test.data.detail == null ? t('testOkNoCount') : t('testOk', { count: test.data.detail })) : t(`testError_${test.data.error ?? 'unknown'}` as 'testError_unknown')}</p> : null}
            {testedIndex === index && test.isError ? <p className="error m-0" role="alert">{t(apiFailureKey(test.error) ?? 'requestFailed')}</p> : null}
          </div>
        </div>)}
        <Button type="button" variant="outline" disabled={save.isPending || view.providers.length >= 4} onClick={() => change({ ...view, providers: [...view.providers, { provider: 'tavily', endpoint: null, credential_configured: false, destination_id: null, key: '', removed: false }] })}>{t('addFallback')}</Button>
        <CheckboxField><Checkbox id="web-search-allow" disabled={save.isPending} checked={view.allow_remote_web_search} onCheckedChange={(checked) => change({ ...view, allow_remote_web_search: checked === true })} /><label htmlFor="web-search-allow">{t('allowRemote')}</label></CheckboxField>
        <p className="m-0 text-xs text-muted-foreground">{t('envNote')}</p>
      </SettingsSection>
      {save.isError ? <p className="error" role="alert">{t(apiFailureKey(save.error) ?? 'saveFailed')}</p> : null}
      <SettingsActions>
        <Button type="button" variant="outline" disabled={save.isPending || !draft} onClick={() => { setDraft(null); setTestedIndex(null); save.reset(); test.reset(); }}>{t('cancelChanges')}</Button>
        <Button type="submit" disabled={save.isPending || !draft || !valid}>{t('saveButton')}</Button>
      </SettingsActions>
    </form>
  </div>;
}
