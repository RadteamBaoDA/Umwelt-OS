'use client';

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useRef, useState } from 'react';
import { useTranslations } from 'next-intl';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { Input } from '@/components/ui/input';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { apiRequest, csrfHeaders } from '@/core/api';
import { apiFailureKey } from '@/core/api-failure-key';
import { useWorkspaceSession } from '@/core/app-shell/workspace-shell';
import { AgentSettingsWorkspace } from '@/modules/agents/agent-settings';

type Mapping = { model: string; version: string | null; destination: 'unknown' | 'remote' };
type Privacy = { allow_remote_reasoning: boolean; allow_remote_embeddings: boolean; allow_remote_web_search: boolean; reasoning_destinations: string[]; embedding_destinations: string[]; web_search_destinations: string[] };
type Capability = { alias: string; capability: string; result: string; expires_at: string };
type AISettings = { configuration_revision: number; omniroute_base_url: string | null; endpoint_destination_id: string | null; endpoint_policy_denied: boolean; omniroute_credential_configured: boolean; web_search_provider: 'none' | 'tavily' | 'brave'; web_search_endpoint: string | null; web_search_credential_configured: boolean; chat_alias: string; brief_alias: string; aliases: Record<string, Mapping>; capabilities: Capability[]; privacy: Privacy; request_timeout_seconds: number };
const aliases = ['reasoning-large', 'reasoning-small', 'fast', 'embedding', 'reranker', 'vision', 'local-private'];
const capabilityTypes = ['chat', 'streaming', 'embeddings', 'structured', 'tools', 'reranking'];
const draftStorageKey = 'bbd:settings:ai:draft:v1';
const defaultPrivacy: Privacy = { allow_remote_reasoning: false, allow_remote_embeddings: false, allow_remote_web_search: false, reasoning_destinations: [], embedding_destinations: [], web_search_destinations: [] };

/** Loads AI, provider, privacy and specialist settings while guarding unsaved gateway drafts. */
export function AISettingsWorkspace() {
  const t = useTranslations('aiSettings');
  const { csrfToken } = useWorkspaceSession();
  const client = useQueryClient();
  const query = useQuery({ queryKey: ['ai-settings'], queryFn: () => apiRequest<AISettings>('/api/v1/settings/ai') });
  const [draft, setDraft] = useState<AISettings | null>(null);
  const [gatewayKey, setGatewayKey] = useState('');
  const [gatewayAction, setGatewayAction] = useState<'unchanged' | 'replaced' | 'removed'>('unchanged');
  const [searchKey, setSearchKey] = useState('');
  const [searchAction, setSearchAction] = useState<'unchanged' | 'replaced' | 'removed'>('unchanged');
  const [probeTypes, setProbeTypes] = useState<Record<string, string>>({});
  const allowNavigation = useRef(false);
  const restoredDraft = useRef(false);
  const [restoredSecret, setRestoredSecret] = useState(false);
  const saved = draft ?? query.data;
  const dirty = Boolean(query.data && (JSON.stringify(draft ?? query.data) !== JSON.stringify(query.data) || gatewayKey || searchKey || gatewayAction !== 'unchanged' || searchAction !== 'unchanged'));
  const save = useMutation({ mutationFn: (value: AISettings) => apiRequest<AISettings>('/api/v1/settings/ai', { method: 'PUT', headers: { ...csrfHeaders(csrfToken), 'Content-Type': 'application/json' }, body: JSON.stringify({ expected_revision: value.configuration_revision, omniroute_base_url: value.omniroute_base_url || null, omniroute_credential_action: gatewayAction, omniroute_api_key: gatewayKey || null, web_search_provider: value.web_search_provider, web_search_endpoint: value.web_search_endpoint || null, web_search_credential_action: searchAction, web_search_api_key: searchKey || null, chat_alias: value.chat_alias, brief_alias: value.brief_alias, aliases: value.aliases, privacy: value.privacy, request_timeout_seconds: value.request_timeout_seconds }) }), onSuccess: (value) => { setDraft(null); setGatewayAction('unchanged'); setSearchAction('unchanged'); setGatewayKey(''); setSearchKey(''); client.setQueryData(['ai-settings'], value); } });
  useEffect(() => {
    if (!query.data) return;
    if (!restoredDraft.current) {
      restoredDraft.current = true;
      try {
        const raw = sessionStorage.getItem(draftStorageKey);
        const snapshot = raw ? JSON.parse(raw) as { revision?: number; draft?: AISettings | null; gatewayAction?: 'unchanged' | 'replaced' | 'removed'; searchAction?: 'unchanged' | 'replaced' | 'removed'; hadGatewayKey?: boolean; hadSearchKey?: boolean } : null;
        if (snapshot?.revision === query.data.configuration_revision) {
          if (snapshot.draft) setDraft(snapshot.draft);
          if (snapshot.gatewayAction) setGatewayAction(snapshot.gatewayAction);
          if (snapshot.searchAction) setSearchAction(snapshot.searchAction);
          setRestoredSecret(Boolean(snapshot.hadGatewayKey || snapshot.hadSearchKey));
          return;
        }
      } catch {
        // Ignore an invalid/stale local draft and use the server settings.
      }
      sessionStorage.removeItem(draftStorageKey);
      return;
    }
    if (dirty || save.isPending) {
      try {
        sessionStorage.setItem(draftStorageKey, JSON.stringify({
          revision: query.data.configuration_revision,
          draft,
          gatewayAction,
          searchAction,
          hadGatewayKey: Boolean(gatewayKey),
          hadSearchKey: Boolean(searchKey),
        }));
      } catch {
        // The in-memory draft remains available for this mount.
      }
    } else {
      sessionStorage.removeItem(draftStorageKey);
    }
  }, [query.data, dirty, draft, gatewayAction, searchAction, gatewayKey, searchKey, save.isPending]);
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
  const discover = useMutation({ mutationFn: () => apiRequest<{ model_ids: string[] }>('/api/v1/settings/ai/discover', { method: 'POST', headers: { ...csrfHeaders(csrfToken), 'Content-Type': 'application/json' }, body: JSON.stringify({ base_url: saved?.omniroute_base_url, api_key: gatewayKey }) }) });
  const draftProbe = useMutation({ mutationFn: ({ alias, mapping, capability }: { alias: string; mapping: Mapping; capability: string }) => apiRequest<Capability>(`/api/v1/settings/models/${alias}/draft-test`, { method: 'POST', headers: { ...csrfHeaders(csrfToken), 'Content-Type': 'application/json' }, body: JSON.stringify({ base_url: saved?.omniroute_base_url, api_key: gatewayKey, model: mapping.model, version: mapping.version, capability }) }) });
  const probe = useMutation({ mutationFn: ({ alias, capability }: { alias: string; capability: string }) => apiRequest<Capability>(`/api/v1/settings/models/${alias}/test`, { method: 'POST', headers: { ...csrfHeaders(csrfToken), 'Content-Type': 'application/json' }, body: JSON.stringify({ capability }) }), onSuccess: () => client.invalidateQueries({ queryKey: ['ai-settings'] }) });
  if (query.isPending) return <section className="content-panel skeleton" aria-label={t('loading')} />;
  if (query.isError || !saved) return <section className="content-panel"><h1>{t('unavailable')}</h1><p className="error">{t(apiFailureKey(query.error) ?? 'loadFailed')}</p><Button className="secondary" onClick={() => query.refetch()}>{t('retry')}</Button></section>;
  /** Applies a partial update to the AI settings draft. */
  const set = (patch: Partial<AISettings>) => setDraft({ ...saved, ...patch });
  /** Merges a partial privacy update into the current AI settings draft. */
  const setPrivacy = (patch: Partial<Privacy>) => set({ privacy: { ...defaultPrivacy, ...saved.privacy, ...patch } });
  return <><section className="content-panel"><span className="brand">{t('gateway')}</span><h1>{t('title')}</h1><p className="muted">{t('description')}</p>
    <p className="muted" role="status">{save.isPending ? t('saving') : dirty ? t('unsaved') : t('saved')}</p>
    <form className="form" onSubmit={(event) => { event.preventDefault(); save.mutate(saved); }}>
      <fieldset disabled={save.isPending} className="form-fields">
      <fieldset className="sub-panel"><legend>{t('gateway')}</legend>
        <label className="field"><span className="label">{t('gatewayEndpoint')}</span><Input type="url" value={saved.omniroute_base_url ?? ''} onChange={(event) => set({ omniroute_base_url: event.target.value || null })} placeholder="https://gateway.example/v1" /></label>
        <Button type="button" className="secondary" disabled={discover.isPending || !saved.omniroute_base_url || !gatewayKey} onClick={() => discover.mutate()}>{t('discover')}</Button>{discover.data && <p className="muted">{t('availableIds')} {discover.data.model_ids.join(', ') || t('noneReturned')}</p>}
        {discover.error && <p className="error" role="alert">{t(apiFailureKey(discover.error) ?? 'discoveryFailed')}</p>}
        {saved.endpoint_policy_denied && <p className="error" role="alert">{t('blockedEndpoint')}</p>}
        <p className="muted">{t('credential')} {saved.omniroute_credential_configured ? t('configured') : t('notConfigured')}{saved.endpoint_destination_id ? ` · ${saved.endpoint_destination_id}` : ''}</p>
        <label className="field"><span className="label">{t('gatewayCredential')}</span><Input type="password" autoComplete="new-password" value={gatewayKey} onChange={(event) => { setGatewayKey(event.target.value); setGatewayAction(event.target.value ? 'replaced' : 'unchanged'); }} placeholder={t('gatewayCredentialHelp')} /></label>
        <label className="field"><Checkbox checked={gatewayAction === 'removed'} onCheckedChange={(checked) => setGatewayAction(checked ? 'removed' : 'unchanged')} /> {t('removeGatewayCredential')}</label>
      </fieldset>
      <fieldset className="sub-panel"><legend>{t('aliases')}</legend>
        <label className="field"><span className="label">{t('chatAlias')}</span><Select value={saved.chat_alias} onValueChange={(value) => set({ chat_alias: value as AISettings['chat_alias'] })}><SelectTrigger><SelectValue /></SelectTrigger><SelectContent>{['reasoning-large', 'reasoning-small', 'fast'].map((item) => <SelectItem key={item} value={item}>{item}</SelectItem>)}</SelectContent></Select></label>
        <label className="field"><span className="label">{t('briefAlias')}</span><Select value={saved.brief_alias} onValueChange={(value) => set({ brief_alias: value as AISettings['brief_alias'] })}><SelectTrigger><SelectValue /></SelectTrigger><SelectContent>{['reasoning-large', 'reasoning-small', 'fast'].map((item) => <SelectItem key={item} value={item}>{item}</SelectItem>)}</SelectContent></Select></label>
        {aliases.map((alias) => { const value = saved.aliases[alias] ?? { model: '', version: null, destination: 'unknown' as const }; return <div className="sub-panel" key={alias}><h2>{alias}</h2>
          <label className="field"><span className="label">{t('modelId')}</span><Input value={value.model} maxLength={200} onChange={(event) => set({ aliases: { ...saved.aliases, [alias]: { ...value, model: event.target.value } } })} /></label>
          <label className="field"><span className="label">{t('modelVersion')}</span><Input value={value.version ?? ''} maxLength={200} onChange={(event) => set({ aliases: { ...saved.aliases, [alias]: { ...value, version: event.target.value || null } } })} /></label>
          <label className="field"><span className="label">{t('destination')}</span><Select value={value.destination} onValueChange={(destination) => set({ aliases: { ...saved.aliases, [alias]: { ...value, destination: destination as Mapping['destination'] } } })}><SelectTrigger><SelectValue /></SelectTrigger><SelectContent><SelectItem value="unknown">{t('unknownDenied')}</SelectItem><SelectItem value="remote">{t('remote')}</SelectItem></SelectContent></Select></label>
          {(query.data?.capabilities ?? saved.capabilities).filter((item) => item.alias === alias).map((item) => <p className="muted" key={`${item.capability}-${item.expires_at}`}>{item.capability}: {item.result}</p>)}
          <div className="form-actions"><Select value={probeTypes[alias] ?? 'chat'} onValueChange={(value) => setProbeTypes({ ...probeTypes, [alias]: value })}><SelectTrigger aria-label={t('probeCapability', { alias })}><SelectValue /></SelectTrigger><SelectContent>{capabilityTypes.map((kind) => <SelectItem key={kind} value={kind}>{t(`capability${kind[0].toUpperCase()}${kind.slice(1)}` as 'capabilityChat')}</SelectItem>)}</SelectContent></Select><Button type="button" className="secondary" disabled={!value.model || value.destination !== 'remote' || probe.isPending || JSON.stringify(value) !== JSON.stringify(query.data?.aliases[alias])} onClick={() => probe.mutate({ alias, capability: probeTypes[alias] ?? 'chat' })}>{t('probeSaved')}</Button><Button type="button" className="secondary" disabled={!saved.omniroute_base_url || !value.model || value.destination !== 'remote' || draftProbe.isPending} onClick={() => draftProbe.mutate({ alias, mapping: value, capability: probeTypes[alias] ?? 'chat' })}>{t('probeDraft')}</Button></div>
        </div>; })}
      </fieldset>
      <fieldset className="sub-panel"><legend>{t('webSearch')}</legend>
        <label className="field"><span className="label">{t('provider')}</span><Select value={saved.web_search_provider} onValueChange={(value) => set({ web_search_provider: value as AISettings['web_search_provider'] })}><SelectTrigger><SelectValue /></SelectTrigger><SelectContent><SelectItem value="none">{t('none')}</SelectItem><SelectItem value="tavily">Tavily</SelectItem><SelectItem value="brave">Brave</SelectItem></SelectContent></Select></label>
        <label className="field"><span className="label">{t('providerEndpoint')}</span><Input type="url" value={saved.web_search_endpoint ?? ''} onChange={(event) => set({ web_search_endpoint: event.target.value || null })} /></label>
        <p className="muted">{t('credential')} {saved.web_search_credential_configured ? t('configured') : t('notConfigured')}</p>
        <label className="field"><span className="label">{t('providerCredential')}</span><Input type="password" autoComplete="new-password" value={searchKey} onChange={(event) => { setSearchKey(event.target.value); setSearchAction(event.target.value ? 'replaced' : 'unchanged'); }} placeholder={t('keepSavedCredential')} /></label>
        <label className="field"><Checkbox checked={searchAction === 'removed'} onCheckedChange={(checked) => setSearchAction(checked ? 'removed' : 'unchanged')} /> {t('removeSearchCredential')}</label>
      </fieldset>
      <fieldset className="sub-panel"><legend>{t('remotePermissions')}</legend>
        <p className="muted">{t('endpointGrantHelp')}</p>
        <label className="field"><Checkbox checked={saved.privacy.allow_remote_reasoning} onCheckedChange={(checked) => setPrivacy({ allow_remote_reasoning: checked === true })} /> {t('allowRemoteReasoning')}</label>
        <label className="field"><Checkbox checked={saved.privacy.allow_remote_embeddings} onCheckedChange={(checked) => setPrivacy({ allow_remote_embeddings: checked === true })} /> {t('allowRemoteEmbeddings')}</label>
        <label className="field"><Checkbox checked={saved.privacy.allow_remote_web_search} onCheckedChange={(checked) => setPrivacy({ allow_remote_web_search: checked === true })} /> {t('allowRemoteSearch')}</label>
      </fieldset>
      <label className="field"><span className="label">{t('timeout')}</span><Input type="number" min={5} max={180} value={saved.request_timeout_seconds} onChange={(event) => set({ request_timeout_seconds: Number(event.target.value) })} /></label>
      </fieldset>
      {restoredSecret && <p className="error" role="status">{t('restoredDraft')}</p>}
      {save.error && <p className="error" role="alert">{t(apiFailureKey(save.error) ?? 'saveFailed')}</p>}{probe.error && <p className="error" role="alert">{t(apiFailureKey(probe.error) ?? 'probeFailed')}</p>}{draftProbe.data && <p className="muted" role="status">{t('draftProbe')} {draftProbe.data.capability} {draftProbe.data.result}; {t('draftProbeUnstored')}</p>}{draftProbe.error && <p className="error" role="alert">{t(apiFailureKey(draftProbe.error) ?? 'draftProbeFailed')}</p>}
      <div className="form-actions"><Button type="submit" disabled={save.isPending || !dirty}>{t('saveButton')}</Button><Button type="button" className="secondary" disabled={save.isPending || !dirty} onClick={() => { sessionStorage.removeItem(draftStorageKey); setDraft(null); setGatewayKey(''); setGatewayAction('unchanged'); setSearchKey(''); setSearchAction('unchanged'); setRestoredSecret(false); save.reset(); draftProbe.reset(); discover.reset(); }}>{t('cancelChanges')}</Button></div>
    </form>
  </section><AgentSettingsWorkspace /></>;
}
