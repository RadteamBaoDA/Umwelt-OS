'use client';

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { AlertCircle, Check, Minus, X } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import { useTranslations } from 'next-intl';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { Input } from '@/components/ui/input';
import { ApiError, apiRequest, csrfHeaders } from '@/core/api';
import { apiFailureKey } from '@/core/api-failure-key';
import { useWorkspaceSession } from '@/core/app-shell/workspace-shell';
import { AgentSettingsWorkspace } from '@/modules/agents/agent-settings';
import { CheckboxField, SettingsActions, SettingsPageHeader, SettingsSection } from './settings-layout';
import { AutomationSettings } from '@/modules/automations/rule-list';
import { mappingFor, ModelDefaults, ModelsPanel, pruneDefaults, type DefaultSlot, type Defaults, type Mapping } from './models-section';
import { modelTestKey, parseDraftSnapshot, ProviderEditor, testRoleByType, type ModelTestState, type ProviderModel, type ProviderRead, type ProviderSecret } from './provider-editor';

type Privacy = { allow_remote_reasoning: boolean; allow_remote_embeddings: boolean; allow_remote_web_search: boolean; allow_widget_remote_media: boolean; reasoning_destinations: string[]; embedding_destinations: string[]; web_search_destinations: string[] };
type Capability = { role: string; capability: string; result: string; expires_at: string; dimensions?: number };
type AISettings = { configuration_revision: number; providers: ProviderRead[]; web_search_provider: 'none' | 'tavily' | 'brave' | 'searxng'; web_search_endpoint: string | null; web_search_credential_configured: boolean; defaults: Defaults; capabilities: Capability[]; privacy: Privacy; request_timeout_seconds: number };

/**
 * Draft-only default auto-fill: the first Chat model newly present in `next` fills an empty chat and brief
 * default, and a vision-flagged one also fills an empty vision default. Never touches embedding/reranker
 * (embedding needs a dimensions probe), background (it falls back on its own) or defaults already set.
 */
export function autoFillDefaults(prev: ProviderRead[], next: ProviderRead[], current: Defaults): Defaults {
  const out = { ...current };
  for (const provider of next) {
    const before = new Set((prev.find((item) => item.id === provider.id)?.models ?? []).map((model) => model.id));
    for (const model of (provider.models ?? []).filter((item) => item.type === 'llm' && !before.has(item.id))) {
      const mapping = mappingFor(provider, model.id);
      for (const role of model.vision ? (['chat', 'brief', 'vision'] as const) : (['chat', 'brief'] as const)) {
        if (!out[role]?.model) out[role] = mapping;
      }
    }
  }
  return out;
}
const draftStorageKey = 'bbd:settings:ai:draft:v2';
const defaultPrivacy: Privacy = { allow_remote_reasoning: false, allow_remote_embeddings: false, allow_remote_web_search: false, allow_widget_remote_media: false, reasoning_destinations: [], embedding_destinations: [], web_search_destinations: [] };

/** Loads AI, provider, privacy and specialist settings while guarding unsaved gateway drafts. */
export function AISettingsWorkspace() {
  const t = useTranslations('aiSettings');
  const { csrfToken } = useWorkspaceSession();
  const client = useQueryClient();
  const query = useQuery({ queryKey: ['ai-settings'], queryFn: () => apiRequest<AISettings>('/api/v1/settings/ai') });
  const [draft, setDraft] = useState<AISettings | null>(null);
  const [secrets, setSecrets] = useState<Record<string, ProviderSecret>>({});
  const [discoveringId, setDiscoveringId] = useState<string | null>(null);
  const [discoverError, setDiscoverError] = useState<{ id: string; text: string } | null>(null);
  const [discovered, setDiscovered] = useState<Record<string, string[]>>({});
  const [modelTests, setModelTests] = useState<Record<string, ModelTestState>>({});
  const allowNavigation = useRef(false);
  const [restoredDraft, setRestoredDraft] = useState(false);
  const [restoredSecret, setRestoredSecret] = useState(false);
  const base = draft ?? query.data;
  const saved = base ? { ...base, providers: base.providers ?? [] } : undefined;
  const secretList = Object.values(secrets);
  const dirty = Boolean(query.data && (JSON.stringify(draft ?? query.data) !== JSON.stringify(query.data) || secretList.some((item) => item.key || item.action !== 'unchanged')));
  const save = useMutation({ mutationFn: (value: AISettings) => apiRequest<AISettings>('/api/v1/settings/ai', { method: 'PUT', headers: { ...csrfHeaders(csrfToken), 'Content-Type': 'application/json' }, body: JSON.stringify({ expected_revision: value.configuration_revision, providers: value.providers.map((item) => ({ id: item.id, kind: item.kind, label: item.label, base_url: item.base_url || null, models: item.models ?? [], credential_action: secrets[item.id]?.action ?? 'unchanged', api_key: secrets[item.id]?.key || null })), web_search_provider: value.web_search_provider, web_search_endpoint: value.web_search_endpoint || null, web_search_credential_action: 'unchanged', web_search_api_key: null, defaults: value.defaults ?? {}, privacy: value.privacy, request_timeout_seconds: value.request_timeout_seconds }) }), onSuccess: (value) => { setDraft(null); setSecrets({}); client.setQueryData(['ai-settings'], value); } });
  if (query.data && !restoredDraft) {
    // Restores a same-revision draft once, during render, before the persistence effect runs.
    setRestoredDraft(true);
    let raw: string | null = null;
    try { raw = sessionStorage.getItem(draftStorageKey); } catch { /* storage unavailable */ }
    const snapshot = parseDraftSnapshot<AISettings>(raw, query.data.configuration_revision);
    if (snapshot) {
      if (snapshot.draft) setDraft(snapshot.draft);
      setSecrets(Object.fromEntries(Object.entries(snapshot.providerActions).map(([id, action]) => [id, { key: '', action }])));
      setRestoredSecret(snapshot.hadSecret);
    } else {
      try { sessionStorage.removeItem(draftStorageKey); } catch { /* storage unavailable */ }
    }
  }
  useEffect(() => {
    if (!query.data || !restoredDraft) return;
    if (dirty || save.isPending) {
      try {
        sessionStorage.setItem(draftStorageKey, JSON.stringify({
          revision: query.data.configuration_revision,
          draft,
          providerActions: Object.fromEntries(Object.entries(secrets).filter(([, item]) => item.action !== 'unchanged').map(([id, item]) => [id, item.action])),
          hadSecret: secretList.some((item) => item.key),
        }));
      } catch {
        // The in-memory draft remains available for this mount.
      }
    } else {
      sessionStorage.removeItem(draftStorageKey);
    }
  }, [query.data, restoredDraft, dirty, draft, secrets, save.isPending]);
  useEffect(() => {
    if (!dirty && !save.isPending) return;
    /** Prevents browser navigation while unsaved AI settings remain. */
    const warn = (event: BeforeUnloadEvent) => {
      if (allowNavigation.current) return;
      event.preventDefault();
      event.returnValue = '';
    };
    /** Allows the auth-ending navigation and removes the saved AI settings draft snapshot. */
    const authEnding = () => {
      allowNavigation.current = true;
      sessionStorage.removeItem(draftStorageKey);
    };
    const navigationApi = (window as Window & { navigation?: EventTarget }).navigation;
    /** Intercepts guarded route navigation while the settings draft is dirty. */
    const guardNavigation = (event: Event) => {
      if (allowNavigation.current) return;
      const navigateEvent = event as Event & { destination?: { url?: string } };
      let sameOrigin = false;
      try {
        sameOrigin = Boolean(navigateEvent.destination?.url && new URL(navigateEvent.destination.url).origin === window.location.origin);
      } catch {
        sameOrigin = false;
      }
      if (!sameOrigin || !event.cancelable) return;
      if (save.isPending || !window.confirm(t('discardChanges'))) {
        event.preventDefault();
        return;
      }
      sessionStorage.removeItem(draftStorageKey);
      allowNavigation.current = true;
    };
    /** Intercepts eligible links so leaving a dirty settings draft requires confirmation. */
    const guardLinks = (event: MouseEvent) => {
      if (allowNavigation.current) return;
      if (navigationApi) return;
      const anchor = (event.target as HTMLElement | null)?.closest('a[href]');
      if (!(anchor instanceof HTMLAnchorElement) || anchor.target || anchor.origin !== window.location.origin) return;
      event.preventDefault();
      event.stopImmediatePropagation();
      if (save.isPending || !window.confirm(t('discardChanges'))) return;
      sessionStorage.removeItem(draftStorageKey);
      allowNavigation.current = true;
      window.location.assign(anchor.href);
    };
    window.addEventListener('beforeunload', warn);
    window.addEventListener('bbd:auth-ending', authEnding);
    navigationApi?.addEventListener('navigate', guardNavigation);
    document.addEventListener('click', guardLinks, true);
    return () => {
      window.removeEventListener('beforeunload', warn);
      window.removeEventListener('bbd:auth-ending', authEnding);
      navigationApi?.removeEventListener('navigate', guardNavigation);
      document.removeEventListener('click', guardLinks, true);
    };
  }, [dirty, save.isPending, t]);
  const savedIds = (query.data?.providers ?? []).map((item) => item.id);
  const discover = useMutation({
    mutationFn: (provider: ProviderRead) => apiRequest<{ model_ids: string[] }>('/api/v1/settings/ai/discover', { method: 'POST', headers: { ...csrfHeaders(csrfToken), 'Content-Type': 'application/json' }, body: JSON.stringify({ provider_id: savedIds.includes(provider.id) ? provider.id : undefined, base_url: provider.base_url, kind: provider.kind, api_key: secrets[provider.id]?.key ?? '' }) }),
    onMutate: (provider) => { setDiscoveringId(provider.id); setDiscoverError(null); },
    // Discovery only fills the transient picker list; models join the draft via the Add row.
    onSuccess: (data, provider) => setDiscovered((current) => ({ ...current, [provider.id]: data.model_ids })),
    onError: (error, provider) => setDiscoverError({ id: provider.id, text: describeError(error, t, 'discoveryFailed') }),
    onSettled: () => setDiscoveringId(null),
  });
  const draftProbe = useMutation({ mutationFn: ({ role, mapping, capability, provider }: { role: DefaultSlot; mapping: Mapping; capability: string; provider: ProviderRead | undefined }) => apiRequest<Capability>('/api/v1/settings/models/draft-test', { method: 'POST', headers: { ...csrfHeaders(csrfToken), 'Content-Type': 'application/json' }, body: JSON.stringify({ role, provider_id: provider && savedIds.includes(provider.id) ? provider.id : undefined, base_url: provider?.base_url, kind: provider?.kind, api_key: provider ? secrets[provider.id]?.key ?? '' : '', model: mapping.model, version: mapping.version, capability }) }),
    // A supported embeddings probe records the vector size on the embedding default draft.
    onSuccess: (data, { role, mapping }) => { if (data.capability === 'embeddings' && data.result === 'supported' && typeof data.dimensions === 'number') setDraft((current) => { const from = current ?? query.data; // Ignore a late probe for a model the owner has since replaced.
      const picked = from?.defaults?.[role];
      return from && picked?.model === mapping.model ? { ...from, defaults: { ...from.defaults, [role]: { ...picked, dimensions: data.dimensions } } } : current; }); } });
  /** Runs the draft-test request for one provider model using the role that matches its type. */
  const testModel = useMutation({
    mutationFn: ({ provider, model }: { provider: ProviderRead; model: ProviderModel }) => {
      const slot = testRoleByType[model.type];
      if (!slot) return Promise.reject(new Error('untestable'));
      return apiRequest<Capability>('/api/v1/settings/models/draft-test', { method: 'POST', headers: { ...csrfHeaders(csrfToken), 'Content-Type': 'application/json' }, body: JSON.stringify({ role: slot.role, provider_id: savedIds.includes(provider.id) ? provider.id : undefined, base_url: provider.base_url, kind: provider.kind, api_key: secrets[provider.id]?.key ?? '', model: model.id, version: null, capability: slot.capability }) });
    },
    onMutate: ({ provider, model }) => setModelTests((current) => ({ ...current, [modelTestKey(provider.id, model.id)]: 'pending' })),
    onSuccess: (data, { provider, model }) => setModelTests((current) => ({ ...current, [modelTestKey(provider.id, model.id)]: data.result as ModelTestState })),
    onError: (_error, { provider, model }) => setModelTests((current) => ({ ...current, [modelTestKey(provider.id, model.id)]: 'failed' })),
  });
  if (query.isPending) return <section className="rounded-xl border bg-card p-6 skeleton" aria-label={t('loading')} />;
  if (query.isError || !saved) return <section className="rounded-xl border bg-card p-6"><h1>{t('unavailable')}</h1><p className="error">{describeError(query.error, t, 'loadFailed')}</p><Button variant="outline" onClick={() => query.refetch()}>{t('retry')}</Button></section>;
  /** Applies a partial update to the AI settings draft. */
  const set = (patch: Partial<AISettings>) => setDraft({ ...saved, ...patch });
  /** Merges a partial privacy update into the current AI settings draft. */
  const setPrivacy = (patch: Partial<Privacy>) => set({ privacy: { ...defaultPrivacy, ...saved.privacy, ...patch } });
  const defaults: Defaults = saved.defaults ?? {};
  /** Latest recorded probe result for one role and capability, or null when never checked. */
  const capabilityStatus = (role: string, capability: string) => {
    // Results belong to the persisted mapping; a changed draft mapping is unchecked.
    if (JSON.stringify(defaults[role as DefaultSlot] ?? null) !== JSON.stringify(query.data?.defaults?.[role as DefaultSlot] ?? null)) return null;
    const found = (query.data?.capabilities ?? saved.capabilities).filter((item) => item.role === role && item.capability === capability).at(-1);
    return found?.result ?? null;
  };
  const embeddingMapping = defaults.embedding;
  const embeddingsAvailable = Boolean(embeddingMapping?.model && embeddingMapping.destination === 'remote' && saved.privacy.allow_remote_embeddings);
  /** Clears every local change after the owner confirms discarding the draft. */
  const cancelChanges = () => {
    if (!window.confirm(t('discardDraft'))) return;
    try { sessionStorage.removeItem(draftStorageKey); } catch { /* storage unavailable */ } setDraft(null); setSecrets({}); setRestoredSecret(false); save.reset(); draftProbe.reset(); discover.reset(); setDiscoverError(null);
  };
  const capabilityLabel = (kind: string) => t(`capability${kind[0].toUpperCase()}${kind.slice(1)}` as 'capabilityChat');
  // The backend requires embedding dimensions, so a chosen embedding model blocks Save until its probe supplies them.
  const embeddingStatus: 'idle' | 'checking' | 'error' = !embeddingMapping?.model || embeddingMapping.dimensions != null ? 'idle' : draftProbe.isPending ? 'checking' : 'error';
  const savedDimensions = query.data?.defaults?.embedding?.dimensions ?? null;
  const embeddingDimsChanged = embeddingMapping?.dimensions != null && savedDimensions != null && embeddingMapping.dimensions !== savedDimensions;
  /** Applies a models-list edit: fills empty defaults for new Chat models and clears defaults whose model was removed. */
  const changeModels = (providers: ProviderRead[]) => set({ providers, defaults: pruneDefaults(saved.providers, providers, autoFillDefaults(saved.providers, providers, defaults)) });
  /** Writes one role default directly; an embedding pick also starts the dimensions probe and stays unsavable until it reports them. */
  const pickDefault = (role: DefaultSlot, provider: ProviderRead | null, model: string) => {
    const mapping: Mapping | null = provider && model ? mappingFor(provider, model) : null;
    set({ defaults: { ...defaults, [role]: mapping && role === 'embedding' ? { ...mapping, dimensions: null } : mapping } });
    if (role === 'embedding' && mapping && provider) draftProbe.mutate({ role, mapping, capability: 'embeddings', provider });
  };
  const advancedClass = 'rounded-lg border border-border bg-card';
  const summaryClass = 'flex min-h-[52px] cursor-pointer items-center px-5 font-bold';
  return <div className="flex flex-col gap-5">
    <SettingsPageHeader eyebrow={t('eyebrow')} title={t('title')} description={t('description')} status={<p className="m-0 text-sm text-muted-foreground" role="status">{save.isPending ? t('saving') : dirty ? t('unsaved') : t('saved')}</p>} />
    <form className="flex flex-col gap-5" onSubmit={(event) => { event.preventDefault(); save.mutate(saved); }}>
      <SettingsSection title={t('providersSection')}>
        <p className="text-muted-foreground m-0 text-xs">{t('providersHelp')}</p>
        <ProviderEditor providers={saved.providers} savedIds={savedIds} secrets={secrets} disabled={save.isPending} discoveringId={discoveringId} discoverError={discoverError} discovered={discovered} t={t as never}
          onChange={(providers) => set({ providers })} onSecret={(id, secret) => setSecrets({ ...secrets, [id]: secret })} onDiscover={(provider) => discover.mutate(provider)} />
      </SettingsSection>
      <SettingsSection title={t('modelsSection')}>
        <p className="text-muted-foreground m-0 text-xs">{t('modelsHelp')}</p>
        <ModelsPanel providers={saved.providers} discovered={discovered} modelTests={modelTests} disabled={save.isPending} onChange={changeModels} onTestModel={(provider, model) => testModel.mutate({ provider, model })} t={t as never} />
        <ModelDefaults providers={saved.providers} defaults={defaults} disabled={save.isPending} embeddingStatus={embeddingStatus} onPick={pickDefault} t={t as never} />
        {embeddingDimsChanged && <p className="error m-0 text-xs" role="status">{t('dimensionsChangedWarning')}</p>}
      </SettingsSection>
      <details className={advancedClass}><summary className={summaryClass}>{t('embeddingsTitle')}</summary>
        <div className="flex flex-col gap-3 px-5 pb-5">{embeddingsAvailable
          ? <ul className="m-0 flex flex-wrap gap-2 p-0"><li className="list-none"><CapabilityChip status={capabilityStatus('embedding', 'embeddings')} label={capabilityLabel('embeddings')} t={t} /></li></ul>
          : <><p className="label m-0">{t('embeddingsUnavailable')}</p><p className="text-muted-foreground m-0">{t('embeddingsUnavailableHelp')}</p></>}</div>
      </details>
      <details className={advancedClass}><summary className={summaryClass}>{t('budgetsTitle')}</summary>
        <div className="flex flex-col gap-3 px-5 pb-5">
          <label className="field max-w-xs"><span className="label">{t('timeout')}</span><Input type="number" disabled={save.isPending} min={5} max={180} value={saved.request_timeout_seconds} onChange={(event) => set({ request_timeout_seconds: Number(event.target.value) })} /></label>
          <p className="text-muted-foreground m-0">{t('budgetsUnavailable')}</p>
        </div>
      </details>
      <details className={advancedClass}><summary className={summaryClass}>{t('privacyTitle')}</summary>
        <div className="flex flex-col gap-3 px-5 pb-5">
          <p className="text-muted-foreground m-0">{t('endpointGrantHelp')}</p>
          <p className="text-muted-foreground m-0">{t('localProvidersNote')}</p>
          <CheckboxField><Checkbox id="ai-remote-reasoning" disabled={save.isPending} checked={saved.privacy.allow_remote_reasoning} onCheckedChange={(checked) => setPrivacy({ allow_remote_reasoning: checked === true })} /><label htmlFor="ai-remote-reasoning">{t('allowRemoteReasoning')}</label></CheckboxField>
          <CheckboxField><Checkbox id="ai-remote-embeddings" disabled={save.isPending} checked={saved.privacy.allow_remote_embeddings} onCheckedChange={(checked) => setPrivacy({ allow_remote_embeddings: checked === true })} /><label htmlFor="ai-remote-embeddings">{t('allowRemoteEmbeddings')}</label></CheckboxField>
          <CheckboxField><Checkbox id="ai-widget-media" disabled={save.isPending} checked={saved.privacy.allow_widget_remote_media} onCheckedChange={(checked) => setPrivacy({ allow_widget_remote_media: checked === true })} /><label htmlFor="ai-widget-media">{t('allowWidgetRemoteMedia')}</label></CheckboxField>
          <p className="text-muted-foreground m-0">{t('historyStorage')} <a href="/settings/memory">{t('memoryPrivacyLink')}</a></p>
        </div>
      </details>
      {restoredSecret && <p className="error m-0" role="status">{t('restoredDraft')}</p>}
      {save.error && <p className="error m-0" role="alert">{describeError(save.error, t, 'saveFailed')}</p>}
      {/* Success stays visible next to the (now disabled) button until the next edit makes the form dirty again. */}
      {save.isSuccess && !dirty && <p className="m-0 text-sm text-accent" role="status">{t('saveSuccess')}</p>}
      <SettingsActions>
        <Button type="button" variant="outline" disabled={save.isPending || !dirty} onClick={cancelChanges}>{t('cancelChanges')}</Button><Button type="submit" disabled={save.isPending || !dirty || embeddingStatus !== 'idle'}>{t('saveButton')}</Button>
      </SettingsActions>
    </form>
    <details className={advancedClass}><summary className={summaryClass}>{t('agentsAutomations')}</summary><div className="px-5 pb-5"><AgentSettingsWorkspace /><AutomationSettings /></div></details>
  </div>;
}

/** Capability result chip that always pairs an icon with text so state never depends on color. */
function CapabilityChip({ status, label, t }: { status: string | null; label: string; t: (key: 'capPassed' | 'capFailed' | 'capUnsupported' | 'capNotChecked') => string }) {
  const [Icon, key] = status === 'supported' ? [Check, 'capPassed' as const] : status === 'failed' ? [AlertCircle, 'capFailed' as const] : status === 'unsupported' ? [X, 'capUnsupported' as const] : [Minus, 'capNotChecked' as const];
  return <span className="inline-flex items-center gap-1 rounded-full border border-border px-2 py-0.5 text-xs"><Icon className="size-3" aria-hidden="true" />{label ? `${label}: ` : ''}{t(key)}</span>;
}

/** Localized failure text plus the HTTP status and server detail when the API supplied them. */
function describeError(error: unknown, t: (key: never) => string, fallback: string): string {
  const base = (t as (key: string) => string)(apiFailureKey(error) ?? fallback);
  if (!(error instanceof ApiError)) return base;
  const detail = error.message && error.message !== String(error.status) ? `: ${error.message}` : '';
  return `${base} (HTTP ${error.status}${detail})`;
}
