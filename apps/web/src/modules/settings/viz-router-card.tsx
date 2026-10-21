'use client';

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import { useTranslations } from 'next-intl';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { Input } from '@/components/ui/input';
import { SearchableSelect } from '@/components/ui/searchable-select';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { apiRequest, csrfHeaders } from '@/core/api';
import { apiFailureKey } from '@/core/api-failure-key';
import { useWorkspaceSession } from '@/core/app-shell/workspace-shell';
import { CheckboxField, FieldGrid, SecretField, SettingsActions, SettingsSection } from './settings-layout';

type Kind = 'internal' | 'llm' | 'typesafe' | 'openrouter' | 'cloudflare';
type Mode = 'json_schema' | 'forced_tool';
type Api = 'chat' | 'decisions';
type VizRouter = { configuration_revision: number; kind: Kind; provider_id: string | null; model: string; mode: Mode; api: Api; typesafe_key_configured: boolean; openrouter_key_configured: boolean; cloudflare_key_configured: boolean; account_id: string | null; gateway_url: string | null; allow_remote_visualization_routing: boolean; destination_id: string | null };
type Provider = { id: string; label: string; local: boolean; models: { id: string; type: string }[] };
type DecisionModels = { ok: boolean; models: { id: string; input_modalities: string[] }[]; error: string | null };
type TestResult = { ok: boolean; category: string | null; mode: Mode | null; error: string | null };
type Draft = Partial<Pick<VizRouter, 'kind' | 'provider_id' | 'model' | 'mode' | 'api' | 'account_id' | 'gateway_url' | 'allow_remote_visualization_routing'>>;
const OWN_ENDPOINT_KINDS: Kind[] = ['openrouter', 'cloudflare'];
// OpenRouter Decisions API (alpha) presets; the chat presets below apply when the API selector is on chat completions.
const DECISION_PRESETS = ['cloudflare/clef-flash', 'cloudflare/clef-omni'];
const MODEL_PRESETS: Record<'openrouter' | 'cloudflare', string[]> = {
  openrouter: ['openrouter/auto', 'openai/gpt-4o-mini', 'google/gemini-2.0-flash-001'],
  cloudflare: ['@cf/meta/llama-3.1-8b-instruct', '@cf/meta/llama-3.3-70b-instruct-fp8-fast'],
};
const CLOUDFLARE_ACCOUNT = /^[0-9a-fA-F]{32}$/;

/** Opt-in external visualization router selection (internal default, LLM classifier, OpenRouter, Cloudflare Workers AI or TypeSafe System One) with consent and a Test routing probe. */
export function VizRouterCard() {
  const t = useTranslations('vizRouter');
  const { csrfToken } = useWorkspaceSession();
  const client = useQueryClient();
  const [draft, setDraft] = useState<Draft>({});
  const [key, setKey] = useState('');
  const [removed, setRemoved] = useState(false);
  const [decisionModels, setDecisionModels] = useState<string[]>([]);
  const query = useQuery({ queryKey: ['viz-router'], queryFn: () => apiRequest<VizRouter>('/api/v1/settings/viz-router') });
  const providers = useQuery({ queryKey: ['ai-settings'], queryFn: () => apiRequest<{ providers: Provider[] }>('/api/v1/settings/ai') }).data?.providers ?? [];
  const headers = { ...csrfHeaders(csrfToken), 'Content-Type': 'application/json' };
  const saved = query.data;
  const view = saved ? { ...saved, ...draft } : undefined;
  const keyAction = key ? 'replaced' : removed ? 'removed' : 'unchanged';
  const reset = () => { setDraft({}); setKey(''); setRemoved(false); };
  const save = useMutation({
    mutationFn: (value: VizRouter) => apiRequest<VizRouter>('/api/v1/settings/viz-router', { method: 'PUT', headers, body: JSON.stringify({
      expected_revision: value.configuration_revision, kind: value.kind, provider_id: value.kind === 'llm' ? value.provider_id : null, model: value.kind === 'llm' || value.kind === 'openrouter' || value.kind === 'cloudflare' ? value.model : '', mode: value.mode, api: value.kind === 'openrouter' ? value.api : 'chat',
      typesafe_key_action: value.kind === 'typesafe' ? keyAction : 'unchanged', typesafe_key: value.kind === 'typesafe' ? key || null : null,
      api_key_action: value.kind === 'openrouter' || value.kind === 'cloudflare' ? keyAction : 'unchanged', api_key: value.kind === 'openrouter' || value.kind === 'cloudflare' ? key || null : null,
      account_id: value.kind === 'cloudflare' ? value.account_id?.trim() || null : null, gateway_url: value.kind === 'cloudflare' ? value.gateway_url?.trim() || null : null, allow_remote_visualization_routing: value.kind !== 'internal' && value.allow_remote_visualization_routing,
    }) }),
    onSuccess: (value) => { reset(); test.reset(); client.setQueryData(['viz-router'], value); },
  });
  // Discovery sends only the key (typed, else the saved OpenRouter key) to the server, which asks openrouter.ai for Decision models.
  const loadModels = useMutation({
    mutationFn: () => apiRequest<DecisionModels>('/api/v1/settings/viz-router/decision-models', { method: 'POST', headers, body: JSON.stringify({ api_key: key || null }) }),
    onSuccess: (value) => { if (value.ok) setDecisionModels(value.models.map((item) => item.id)); },
  });
  const test = useMutation({ mutationFn: () => apiRequest<TestResult>('/api/v1/settings/viz-router/test', { method: 'POST', headers }) });
  if (query.isPending) return <section className="rounded-xl border bg-card p-6 skeleton" aria-label={t('loading')} />;
  if (query.isError || !saved || !view) return <section className="rounded-xl border bg-card p-6"><p className="error">{t(apiFailureKey(query.error) ?? 'loadFailed')}</p><Button variant="outline" onClick={() => query.refetch()}>{t('retry')}</Button></section>;
  const dirty = Object.keys(draft).length > 0 || Boolean(key) || removed;
  const provider = providers.find((item) => item.id === (view.provider_id ?? 'default')) ?? providers[0];
  // Decision models first (the intended type); legacy chat models stay selectable for back-compat.
  const models = [...(provider?.models ?? []).filter((item) => item.type === 'decision'), ...(provider?.models ?? []).filter((item) => item.type === 'llm')].map((item) => item.id);
  const ownEndpoint = view.kind === 'openrouter' || view.kind === 'cloudflare';
  // Decisions API (alpha) is OpenRouter-only and has no constraint modes; discovery needs a typed key or the saved OpenRouter key.
  const decisions = view.kind === 'openrouter' && view.api === 'decisions';
  const modelOptions = view.kind === 'cloudflare' ? MODEL_PRESETS.cloudflare : decisions ? [...new Set([...DECISION_PRESETS, ...decisionModels])] : MODEL_PRESETS.openrouter;
  const canLoadModels = key !== '' || (saved.kind === 'openrouter' && saved.openrouter_key_configured && !removed);
  const needsConsent = view.kind === 'typesafe' || ownEndpoint || (view.kind === 'llm' && !provider?.local);
  const keyConfigured = view.kind === 'typesafe' ? saved.typesafe_key_configured : view.kind === 'openrouter' ? saved.openrouter_key_configured : saved.cloudflare_key_configured;
  const keyOk = key !== '' || (keyConfigured && !removed);
  const accountId = view.account_id ?? '';
  const gatewayUrl = view.gateway_url ?? '';
  const cloudflareOk = view.kind !== 'cloudflare' || (gatewayUrl.trim() !== '' ? gatewayUrl.trim().toLowerCase().startsWith('https://') : CLOUDFLARE_ACCOUNT.test(accountId.trim()));
  const valid = view.kind === 'internal' || (view.kind === 'llm' ? Boolean(provider && view.model) : ownEndpoint ? keyOk && view.model.trim() !== '' && cloudflareOk : keyOk);
  const set = (patch: Draft) => setDraft((current) => ({ ...current, ...patch }));
  // Consent is bound to the destination it was given for: any kind/provider change starts unchecked.
  const choose = (patch: Draft) => { setKey(''); setRemoved(false); setDecisionModels([]); loadModels.reset(); set({ ...patch, allow_remote_visualization_routing: false }); };
  const result = test.data;
  const errorText = (code: string | null) => t(`testError_${code ?? 'unknown'}` as 'testError_unknown');
  return <form className="flex flex-col gap-5" aria-label={t('title')} onSubmit={(event) => { event.preventDefault(); save.mutate({ ...view, provider_id: view.provider_id ?? provider?.id ?? null }); }}>
    <SettingsSection title={t('title')}>
      <p className="m-0 text-sm text-muted-foreground">{t('description')}</p>
      <FieldGrid>
        <label className="field"><span className="label">{t('kind')}</span><Select disabled={save.isPending} value={view.kind} onValueChange={(kind) => choose({ kind: kind as Kind, model: '', api: kind === 'openrouter' ? 'decisions' : 'chat' })}><SelectTrigger aria-label={t('kind')}><SelectValue /></SelectTrigger><SelectContent><SelectItem value="internal">{t('internal')}</SelectItem><SelectItem value="llm">{t('llm')}</SelectItem><SelectItem value="openrouter">{t('openrouter')}</SelectItem><SelectItem value="cloudflare">{t('cloudflare')}</SelectItem><SelectItem value="typesafe">{t('typesafe')}</SelectItem></SelectContent></Select></label>
      </FieldGrid>
      {view.kind === 'llm' ? (providers.length === 0 ? <p className="m-0 text-xs text-muted-foreground">{t('noProvider')}</p> : <FieldGrid>
        <label className="field"><span className="label">{t('provider')}</span><Select disabled={save.isPending} value={provider?.id} onValueChange={(id) => choose({ provider_id: id, model: '' })}><SelectTrigger aria-label={t('provider')}><SelectValue /></SelectTrigger><SelectContent>{providers.map((item) => <SelectItem key={item.id} value={item.id}>{item.label || item.id}</SelectItem>)}</SelectContent></Select></label>
        <div className="field"><span className="label">{t('model')}</span><SearchableSelect aria-label={t('model')} disabled={save.isPending || models.length === 0} value={view.model} options={models} placeholder={models.length === 0 ? t('noModel') : t('modelPlaceholder')} searchLabel={t('modelSearch')} emptyLabel={t('modelEmpty')} onValueChange={(model) => set({ model })} /></div>
        <label className="field"><span className="label">{t('mode')}</span><Select disabled={save.isPending} value={view.mode} onValueChange={(mode) => set({ mode: mode as Mode })}><SelectTrigger aria-label={t('mode')}><SelectValue /></SelectTrigger><SelectContent><SelectItem value="json_schema">{t('modeJsonSchema')}</SelectItem><SelectItem value="forced_tool">{t('modeForcedTool')}</SelectItem></SelectContent></Select></label>
      </FieldGrid>) : null}
      {view.kind === 'openrouter' || view.kind === 'cloudflare' ? <>
        <FieldGrid>
          {view.kind === 'cloudflare' ? <>
            <label className="field"><span className="label">{t('accountId')}</span><Input disabled={save.isPending} value={accountId} placeholder="0123456789abcdef0123456789abcdef" aria-invalid={accountId.trim() !== '' && !CLOUDFLARE_ACCOUNT.test(accountId.trim())} onChange={(event) => set({ account_id: event.target.value })} /></label>
            <label className="field"><span className="label">{t('gatewayUrl')}</span><Input disabled={save.isPending} value={gatewayUrl} placeholder="https://gateway.ai.cloudflare.com/v1/…" onChange={(event) => set({ gateway_url: event.target.value })} /></label>
          </> : null}
          <label className="field"><span className="label">{t('model')}</span><Input disabled={save.isPending} list={`viz-router-models-${view.kind}`} value={view.model} placeholder={t('modelFreeText')} onChange={(event) => set({ model: event.target.value })} /><datalist id={`viz-router-models-${view.kind}`}>{modelOptions.map((id) => <option key={id} value={id} />)}</datalist></label>
          {view.kind === 'openrouter' ? <label className="field"><span className="label">{t('api')}</span><Select disabled={save.isPending} value={view.api} onValueChange={(api) => set({ api: api as Api })}><SelectTrigger aria-label={t('api')}><SelectValue /></SelectTrigger><SelectContent><SelectItem value="decisions">{t('apiDecisions')}</SelectItem><SelectItem value="chat">{t('apiChat')}</SelectItem></SelectContent></Select></label> : null}
          {decisions ? null : <label className="field"><span className="label">{t('mode')}</span><Select disabled={save.isPending} value={view.mode} onValueChange={(mode) => set({ mode: mode as Mode })}><SelectTrigger aria-label={t('mode')}><SelectValue /></SelectTrigger><SelectContent><SelectItem value="json_schema">{t('modeJsonSchema')}</SelectItem><SelectItem value="forced_tool">{t('modeForcedTool')}</SelectItem></SelectContent></Select></label>}
        </FieldGrid>
        {decisions ? <div className="flex flex-wrap items-center gap-3">
          <Button type="button" variant="outline" disabled={loadModels.isPending || !canLoadModels} onClick={() => loadModels.mutate()}>{loadModels.isPending ? t('loadingModels') : t('loadModels')}</Button>
          {loadModels.data?.ok ? <p className="m-0 text-xs text-muted-foreground" role="status">{t('modelsLoaded', { count: loadModels.data.models.length })}</p> : null}
          {loadModels.data && !loadModels.data.ok ? <p className="error m-0" role="status">{errorText(loadModels.data.error)}</p> : null}
          {loadModels.isError ? <p className="error m-0" role="alert">{t(apiFailureKey(loadModels.error) ?? 'requestFailed')}</p> : null}
        </div> : null}
        <SecretField id="viz-router-own-key" label={view.kind === 'openrouter' ? t('openrouterKey') : t('cloudflareToken')} value={key} onValue={(value) => { setKey(value); if (value) setRemoved(false); }} removed={removed} onRemoved={(next) => { setRemoved(next); if (next) setKey(''); }} configured={keyConfigured} disabled={save.isPending} placeholder={t('keepSaved')} removeLabel={t('removeKey')} status={<>{t('keyStatus')} {keyConfigured ? t('configured') : t('notConfigured')}. {t('keyNeverShown')}</>} />
        <p className="m-0 text-xs text-muted-foreground">{decisions ? t('decisionsNote') : view.kind === 'openrouter' ? t('openrouterNote') : t('cloudflareNote')}</p>
      </> : null}
      {view.kind === 'typesafe' ? <>
        <SecretField id="viz-router-key" label={t('typesafeKey')} value={key} onValue={(value) => { setKey(value); if (value) setRemoved(false); }} removed={removed} onRemoved={(next) => { setRemoved(next); if (next) setKey(''); }} configured={keyConfigured} disabled={save.isPending} placeholder={t('keepSaved')} removeLabel={t('removeKey')} status={<>{t('keyStatus')} {keyConfigured ? t('configured') : t('notConfigured')}. {t('keyNeverShown')}</>} />
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
