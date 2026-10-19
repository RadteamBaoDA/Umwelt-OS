'use client';

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import { useTranslations } from 'next-intl';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { apiRequest, csrfHeaders } from '@/core/api';
import { apiFailureKey } from '@/core/api-failure-key';
import { useWorkspaceSession } from '@/core/app-shell/workspace-shell';
import { CheckboxField, FieldGrid, SecretField, SettingsActions, SettingsSection } from './settings-layout';

type Kind = 'internal' | 'llm' | 'typesafe';
type Mode = 'json_schema' | 'forced_tool';
type VizRouter = { configuration_revision: number; kind: Kind; provider_id: string | null; model: string; mode: Mode; typesafe_key_configured: boolean; allow_remote_visualization_routing: boolean; destination_id: string | null };
type Provider = { id: string; label: string; local: boolean; models: { id: string; type: string }[] };
type TestResult = { ok: boolean; category: string | null; mode: Mode | null; error: string | null };
type Draft = Partial<Pick<VizRouter, 'kind' | 'provider_id' | 'model' | 'mode' | 'allow_remote_visualization_routing'>>;

/** Opt-in external visualization router selection (internal default, LLM classifier or TypeSafe System One) with consent and a Test routing probe. */
export function VizRouterCard() {
  const t = useTranslations('vizRouter');
  const { csrfToken } = useWorkspaceSession();
  const client = useQueryClient();
  const [draft, setDraft] = useState<Draft>({});
  const [key, setKey] = useState('');
  const [removed, setRemoved] = useState(false);
  const query = useQuery({ queryKey: ['viz-router'], queryFn: () => apiRequest<VizRouter>('/api/v1/settings/viz-router') });
  const providers = useQuery({ queryKey: ['ai-settings'], queryFn: () => apiRequest<{ providers: Provider[] }>('/api/v1/settings/ai') }).data?.providers ?? [];
  const headers = { ...csrfHeaders(csrfToken), 'Content-Type': 'application/json' };
  const saved = query.data;
  const view = saved ? { ...saved, ...draft } : undefined;
  const reset = () => { setDraft({}); setKey(''); setRemoved(false); };
  const save = useMutation({
    mutationFn: (value: VizRouter) => apiRequest<VizRouter>('/api/v1/settings/viz-router', { method: 'PUT', headers, body: JSON.stringify({
      expected_revision: value.configuration_revision, kind: value.kind, provider_id: value.kind === 'llm' ? value.provider_id : null, model: value.kind === 'llm' ? value.model : '', mode: value.mode,
      typesafe_key_action: key ? 'replaced' : removed ? 'removed' : 'unchanged', typesafe_key: key || null, allow_remote_visualization_routing: value.kind !== 'internal' && value.allow_remote_visualization_routing,
    }) }),
    onSuccess: (value) => { reset(); test.reset(); client.setQueryData(['viz-router'], value); },
  });
  const test = useMutation({ mutationFn: () => apiRequest<TestResult>('/api/v1/settings/viz-router/test', { method: 'POST', headers }) });
  if (query.isPending) return <section className="rounded-xl border bg-card p-6 skeleton" aria-label={t('loading')} />;
  if (query.isError || !saved || !view) return <section className="rounded-xl border bg-card p-6"><p className="error">{t(apiFailureKey(query.error) ?? 'loadFailed')}</p><Button variant="outline" onClick={() => query.refetch()}>{t('retry')}</Button></section>;
  const dirty = Object.keys(draft).length > 0 || Boolean(key) || removed;
  const provider = providers.find((item) => item.id === (view.provider_id ?? 'default')) ?? providers[0];
  const models = (provider?.models ?? []).filter((item) => item.type === 'llm');
  const needsConsent = view.kind === 'typesafe' || (view.kind === 'llm' && !provider?.local);
  const valid = view.kind === 'internal' || (view.kind === 'llm' ? Boolean(provider && view.model) : key !== '' || (saved.typesafe_key_configured && !removed));
  const set = (patch: Draft) => setDraft((current) => ({ ...current, ...patch }));
  // Consent is bound to the destination it was given for: any kind/provider change starts unchecked.
  const choose = (patch: Draft) => set({ ...patch, allow_remote_visualization_routing: false });
  const result = test.data;
  const errorText = (code: string | null) => t(`testError_${code ?? 'unknown'}` as 'testError_unknown');
  return <form className="flex flex-col gap-5" aria-label={t('title')} onSubmit={(event) => { event.preventDefault(); save.mutate({ ...view, provider_id: view.provider_id ?? provider?.id ?? null }); }}>
    <SettingsSection title={t('title')}>
      <p className="m-0 text-sm text-muted-foreground">{t('description')}</p>
      <FieldGrid>
        <label className="field"><span className="label">{t('kind')}</span><Select disabled={save.isPending} value={view.kind} onValueChange={(kind) => choose({ kind: kind as Kind })}><SelectTrigger aria-label={t('kind')}><SelectValue /></SelectTrigger><SelectContent><SelectItem value="internal">{t('internal')}</SelectItem><SelectItem value="llm">{t('llm')}</SelectItem><SelectItem value="typesafe">{t('typesafe')}</SelectItem></SelectContent></Select></label>
      </FieldGrid>
      {view.kind === 'llm' ? (providers.length === 0 ? <p className="m-0 text-xs text-muted-foreground">{t('noProvider')}</p> : <FieldGrid>
        <label className="field"><span className="label">{t('provider')}</span><Select disabled={save.isPending} value={provider?.id} onValueChange={(id) => choose({ provider_id: id, model: '' })}><SelectTrigger aria-label={t('provider')}><SelectValue /></SelectTrigger><SelectContent>{providers.map((item) => <SelectItem key={item.id} value={item.id}>{item.label || item.id}</SelectItem>)}</SelectContent></Select></label>
        <label className="field"><span className="label">{t('model')}</span><Select disabled={save.isPending || models.length === 0} value={view.model || undefined} onValueChange={(model) => set({ model })}><SelectTrigger aria-label={t('model')}><SelectValue placeholder={models.length === 0 ? t('noModel') : undefined} /></SelectTrigger><SelectContent>{models.map((item) => <SelectItem key={item.id} value={item.id}>{item.id}</SelectItem>)}</SelectContent></Select></label>
        <label className="field"><span className="label">{t('mode')}</span><Select disabled={save.isPending} value={view.mode} onValueChange={(mode) => set({ mode: mode as Mode })}><SelectTrigger aria-label={t('mode')}><SelectValue /></SelectTrigger><SelectContent><SelectItem value="json_schema">{t('modeJsonSchema')}</SelectItem><SelectItem value="forced_tool">{t('modeForcedTool')}</SelectItem></SelectContent></Select></label>
      </FieldGrid>) : null}
      {view.kind === 'typesafe' ? <>
        <SecretField id="viz-router-key" label={t('typesafeKey')} value={key} onValue={(value) => { setKey(value); if (value) setRemoved(false); }} removed={removed} onRemoved={(next) => { setRemoved(next); if (next) setKey(''); }} configured={saved.typesafe_key_configured} disabled={save.isPending} placeholder={t('keepSaved')} removeLabel={t('removeKey')} status={<>{t('keyStatus')} {saved.typesafe_key_configured ? t('configured') : t('notConfigured')}. {t('keyNeverShown')}</>} />
        <p className="m-0 text-xs text-muted-foreground">{t('typesafeNote')}</p>
      </> : null}
      {view.kind !== 'internal' ? <>
        {needsConsent ? <CheckboxField><Checkbox id="viz-router-allow" disabled={save.isPending} checked={view.allow_remote_visualization_routing} onCheckedChange={(checked) => set({ allow_remote_visualization_routing: checked === true })} /><label htmlFor="viz-router-allow">{t('allowRemote')}</label></CheckboxField> : null}
        <p className="m-0 text-xs text-muted-foreground">{t('privacyNote')}</p>
        <div className="flex flex-wrap items-center gap-3">
          <Button type="button" variant="outline" disabled={test.isPending || dirty || saved.kind === 'internal'} onClick={() => test.mutate()}>{test.isPending ? t('testing') : t('test')}</Button>
          {dirty ? <p className="m-0 text-xs text-muted-foreground">{t('testSaveFirst')}</p> : null}
          {result ? <p className={result.ok ? 'm-0 text-sm text-muted-foreground' : 'error m-0'} role="status">{result.ok ? (result.mode ? t('testOkMode', { mode: result.mode === 'json_schema' ? t('modeJsonSchema') : t('modeForcedTool'), category: result.category ?? '' }) : t('testOk', { category: result.category ?? '' })) : errorText(result.error)}</p> : null}
          {test.isError ? <p className="error m-0" role="alert">{t(apiFailureKey(test.error) ?? 'requestFailed')}</p> : null}
        </div>
      </> : null}
    </SettingsSection>
    {save.isError ? <p className="error" role="alert">{t(apiFailureKey(save.error) ?? 'saveFailed')}</p> : null}
    <SettingsActions>
      <p className="m-0 mr-auto self-center text-sm text-muted-foreground" role="status">{save.isPending ? t('saving') : dirty ? t('unsaved') : t('saved')}</p>
      <Button type="button" variant="outline" disabled={save.isPending || !dirty} onClick={() => { reset(); save.reset(); }}>{t('cancelChanges')}</Button>
      <Button type="submit" disabled={save.isPending || !dirty || !valid}>{t('saveButton')}</Button>
    </SettingsActions>
  </form>;
}
