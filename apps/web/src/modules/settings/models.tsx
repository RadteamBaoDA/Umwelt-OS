'use client';

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import { Button } from '@/components/ui/button';
import { ApiError, apiRequest, csrfHeaders } from '@/core/api';
import { modelIdsForAlias, type ProviderRead } from './provider-editor';
import { useWorkspaceSession } from '@/core/app-shell/workspace-shell';

type Mapping = { model: string; version: string | null; destination: 'unknown' | 'remote' };
type Capability = { alias: string; capability: string; result: string; expires_at: string };
type Models = { aliases: Record<string, Mapping>; capabilities: Capability[]; credential_configured: boolean };
const aliases = ['reasoning-large', 'reasoning-small', 'fast', 'embedding', 'reranker', 'vision', 'local-private'];
const capabilities = ['chat', 'streaming', 'embeddings', 'structured', 'tools', 'reranking'];

/** Renders model settings in the consolidated settings workspace. */
export function ModelSettings() {
  const { csrfToken } = useWorkspaceSession();
  const queryClient = useQueryClient();
  const [drafts, setDrafts] = useState<Record<string, Mapping>>({});
  const [probeTypes, setProbeTypes] = useState<Record<string, string>>({});
  const providers = useQuery({ queryKey: ['ai-settings'], queryFn: () => apiRequest<{ providers: ProviderRead[] }>('/api/v1/settings/ai') });
  const query = useQuery({ queryKey: ['settings-models'], queryFn: () => apiRequest<Models>('/api/v1/settings/models') });
  const save = useMutation({ mutationFn: (values: Record<string, Mapping>) => apiRequest<Models>('/api/v1/settings/models', { method: 'PATCH', headers: { ...csrfHeaders(csrfToken), 'Content-Type': 'application/json' }, body: JSON.stringify(values) }), onSuccess: (value) => { setDrafts(value.aliases); queryClient.setQueryData(['settings-models'], value); } });
  const probe = useMutation({ mutationFn: ({ alias, capability }: { alias: string; capability: string }) => apiRequest<Capability>(`/api/v1/settings/models/${alias}/test`, { method: 'POST', headers: { ...csrfHeaders(csrfToken), 'Content-Type': 'application/json' }, body: JSON.stringify({ capability }) }), onSuccess: () => queryClient.invalidateQueries({ queryKey: ['settings-models'] }) });
  if (query.isPending) return <section className="content-panel skeleton" aria-label="Loading model settings" />;
  if (query.isError) return <section className="content-panel"><h1>Model settings unavailable</h1><p className="error">{query.error instanceof ApiError ? query.error.message : 'Could not load model settings.'}</p><Button className="secondary" onClick={() => query.refetch()}>Retry</Button></section>;
  const current = { ...query.data.aliases, ...drafts };
  return <section className="content-panel"><span className="brand">Privacy first</span><h1>Model aliases</h1><p className="muted">Provider credentials stay in server configuration. Unknown destinations are denied; model requests route through your configured providers.</p><p className="muted">Gateway credential: {query.data.credential_configured ? 'configured' : 'not configured'}</p>
    <form className="form" onSubmit={(event) => { event.preventDefault(); save.mutate(drafts); }}>
      {aliases.map((alias) => { const value = current[alias] ?? { model: '', version: null, destination: 'unknown' as const }; const saved = query.data.aliases[alias] ?? { model: '', version: null, destination: 'unknown' as const }; const hasUnsavedMapping = JSON.stringify(value) !== JSON.stringify(saved); const prior = query.data.capabilities.filter((item) => item.alias === alias); return <fieldset className="sub-panel" key={alias}><legend>{alias}</legend><div className="field"><label className="label" htmlFor={`${alias}-model`}>Configured model ID</label><input className="input" id={`${alias}-model`} list={`${alias}-model-list`} value={value.model} maxLength={200} onChange={(event) => setDrafts({ ...drafts, [alias]: { ...value, model: event.target.value } })} /><datalist id={`${alias}-model-list`}>{modelIdsForAlias(alias, providers.data?.providers ?? []).map((id) => <option key={id} value={id} />)}</datalist></div><div className="field"><label className="label" htmlFor={`${alias}-version`}>Provider/model version (optional)</label><input className="input" id={`${alias}-version`} value={value.version ?? ''} maxLength={200} onChange={(event) => setDrafts({ ...drafts, [alias]: { ...value, version: event.target.value || null } })} /></div><div className="field"><label className="label" htmlFor={`${alias}-destination`}>Destination policy</label><select className="input" id={`${alias}-destination`} value={value.destination} onChange={(event) => setDrafts({ ...drafts, [alias]: { ...value, destination: event.target.value as Mapping['destination'] } })}><option value="unknown">Unknown (deny)</option><option value="remote">Remote provider</option></select></div>{prior.map((item) => <p className="muted" key={`${item.capability}-${item.expires_at}`}>{item.capability}: {item.result} (expires {new Date(item.expires_at).toLocaleString()})</p>)}<div className="form-actions"><select aria-label={`${alias} capability to probe`} className="input" value={probeTypes[alias] ?? 'chat'} onChange={(event) => setProbeTypes({ ...probeTypes, [alias]: event.target.value })}>{capabilities.map((capability) => <option key={capability}>{capability}</option>)}</select><Button type="button" className="secondary" disabled={probe.isPending || !value.model || hasUnsavedMapping} onClick={() => probe.mutate({ alias, capability: probeTypes[alias] ?? 'chat' })}>Probe saved mapping</Button></div>{hasUnsavedMapping && <p className="muted">Save alias changes before probing; the probe uses the saved mapping.</p>}</fieldset>; })}
      {save.error && <p className="error" role="alert">{save.error instanceof ApiError ? save.error.message : 'Could not save model settings.'}</p>}{probe.error && <p className="error" role="alert">{probe.error instanceof ApiError ? probe.error.message : 'Probe failed.'}</p>}<Button type="submit" disabled={save.isPending}>Save alias mappings</Button>
    </form>
  </section>;
}
