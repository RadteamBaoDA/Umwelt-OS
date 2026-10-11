'use client';

import { Check, Plus, Trash2, X } from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { FieldGrid } from './settings-layout';

export const providerKinds = ['openai', 'openrouter', 'groq', 'gemini', 'anthropic', 'ollama', 'lmstudio', 'omniroute', 'custom'] as const;
export type ProviderKind = (typeof providerKinds)[number];
export type CredentialAction = 'unchanged' | 'replaced' | 'removed';
export type ProviderRead = { id: string; kind: ProviderKind; label: string; base_url: string | null; destination_id: string | null; credential_configured: boolean; local: boolean; policy_denied: boolean };
export type ProviderSecret = { key: string; action: CredentialAction };
export const maxProviders = 8;
/** Frontend-only presets: default endpoint, locality and model-id hints per provider kind. */
export const providerPresets: Record<ProviderKind, { baseUrl: string; local: boolean; models: string[] }> = {
  openai: { baseUrl: 'https://api.openai.com/v1', local: false, models: ['gpt-4o', 'gpt-4o-mini', 'text-embedding-3-small'] },
  openrouter: { baseUrl: 'https://openrouter.ai/api/v1', local: false, models: ['openai/gpt-4o-mini', 'anthropic/claude-3.5-sonnet'] },
  groq: { baseUrl: 'https://api.groq.com/openai/v1', local: false, models: ['llama-3.3-70b-versatile', 'llama-3.1-8b-instant'] },
  gemini: { baseUrl: 'https://generativelanguage.googleapis.com/v1beta/openai', local: false, models: ['gemini-2.0-flash', 'gemini-1.5-pro'] },
  anthropic: { baseUrl: 'https://api.anthropic.com/v1', local: false, models: ['claude-sonnet-4-5', 'claude-haiku-4-5'] },
  ollama: { baseUrl: 'http://localhost:11434/v1', local: true, models: ['llama3.2', 'qwen2.5', 'nomic-embed-text'] },
  lmstudio: { baseUrl: 'http://localhost:1234/v1', local: true, models: [] },
  omniroute: { baseUrl: '', local: false, models: [] },
  custom: { baseUrl: '', local: false, models: [] },
};

/** Builds a fresh unsaved provider with a unique id derived from its kind. */
export function newProvider(kind: ProviderKind, existing: ProviderRead[]): ProviderRead {
  const ids = new Set(existing.map((item) => item.id));
  let n = 1;
  while (ids.has(`${kind}-${n}`)) n += 1;
  const preset = providerPresets[kind];
  return { id: `${kind}-${n}`, kind, label: kind, base_url: preset.baseUrl || null, destination_id: null, credential_configured: false, local: preset.local, policy_denied: false };
}

/** Provider id used by an alias mapping: its own id, else "default", else the first provider. */
export function aliasProviderId(providerId: string | null | undefined, providers: ProviderRead[]): string {
  return providerId ?? providers.find((item) => item.id === 'default')?.id ?? providers[0]?.id ?? '';
}

/** Parses a stored settings draft; returns null for other revisions, old (providers-less) shapes or bad JSON. Never carries keys. */
export function parseDraftSnapshot<T extends { providers?: ProviderRead[] }>(raw: string | null, revision: number): { revision: number; draft: (T & { providers: ProviderRead[] }) | null; gatewayAction?: CredentialAction; searchAction?: CredentialAction; providerActions: Record<string, CredentialAction>; hadSecret: boolean } | null {
  try {
    const snap = raw ? JSON.parse(raw) : null;
    if (!snap || snap.revision !== revision) return null;
    const actions = snap.providerActions && typeof snap.providerActions === 'object' ? snap.providerActions : {};
    return { ...snap, draft: snap.draft ? { ...snap.draft, providers: Array.isArray(snap.draft.providers) ? snap.draft.providers : [] } : null, providerActions: actions, hadSecret: Boolean(snap.hadSecret) };
  } catch {
    return null;
  }
}

type Translate = (key: string, values?: Record<string, string | number>) => string;
type Props = {
  providers: ProviderRead[];
  savedIds: string[];
  secrets: Record<string, ProviderSecret>;
  models: Record<string, string[]>;
  disabled: boolean;
  discoveringId: string | null;
  discoverError: { id: string; text: string } | null;
  onChange: (providers: ProviderRead[]) => void;
  onSecret: (id: string, secret: ProviderSecret) => void;
  onDiscover: (provider: ProviderRead) => void;
  t: Translate;
};

/** Editable list of provider cards: preset, label, endpoint, credential, model discovery and removal. */
export function ProviderEditor({ providers, savedIds, secrets, models, disabled, discoveringId, discoverError, onChange, onSecret, onDiscover, t }: Props) {
  const patch = (id: string, change: Partial<ProviderRead>) => onChange(providers.map((item) => (item.id === id ? { ...item, ...change } : item)));
  return <div className="flex flex-col gap-4">
    {providers.length === 0 && <p className="muted m-0">{t('providersEmpty')}</p>}
    {providers.map((provider) => {
      const secret = secrets[provider.id] ?? { key: '', action: 'unchanged' as const };
      const found = models[provider.id];
      const isNew = !savedIds.includes(provider.id);
      const hostHint = provider.kind === 'ollama' || provider.kind === 'lmstudio';
      return <div key={provider.id} className="flex flex-col gap-3 rounded-lg border border-border p-4" data-testid={`provider-${provider.id}`}>
        <div className="flex flex-wrap items-center gap-2">
          <h3 className="m-0 mr-auto text-base font-bold">{provider.label || provider.id}</h3>
          {provider.local && <Badge variant="outline">{t('providerLocal')}</Badge>}
          {provider.policy_denied && <Badge variant="destructive">{t('providerPolicyDenied')}</Badge>}
          <Button type="button" className="secondary" disabled={disabled} aria-label={t('providerDelete', { label: provider.label || provider.id })} onClick={() => onChange(providers.filter((item) => item.id !== provider.id))}><Trash2 className="size-4" aria-hidden="true" /></Button>
        </div>
        <FieldGrid>
          <label className="field"><span className="label">{t('providerKind')}</span>
            <Select disabled={disabled} value={provider.kind} onValueChange={(value) => {
              const kind = value as ProviderKind;
              const old = providerPresets[provider.kind];
              const preset = providerPresets[kind];
              patch(provider.id, { kind, local: preset.local, base_url: !provider.base_url || provider.base_url === old.baseUrl ? preset.baseUrl || null : provider.base_url });
            }}><SelectTrigger aria-label={t('providerKind')}><SelectValue /></SelectTrigger><SelectContent>{providerKinds.map((kind) => <SelectItem key={kind} value={kind}>{t(`kind_${kind}`)}</SelectItem>)}</SelectContent></Select></label>
          <label className="field"><span className="label">{t('providerLabel')}</span><Input disabled={disabled} maxLength={80} value={provider.label} onChange={(event) => patch(provider.id, { label: event.target.value })} /></label>
          <label className="field"><span className="label">{t('providerBaseUrl')}</span><Input type="url" disabled={disabled} value={provider.base_url ?? ''} onChange={(event) => patch(provider.id, { base_url: event.target.value || null })} placeholder={providerPresets[provider.kind].baseUrl || 'https://gateway.example/v1'} /></label>
          <label className="field"><span className="label">{t('providerKey')}</span><Input type="password" disabled={disabled || secret.action === 'removed'} autoComplete="new-password" value={secret.key} onChange={(event) => onSecret(provider.id, { key: event.target.value, action: event.target.value ? 'replaced' : 'unchanged' })} placeholder={provider.local ? t('providerKeyOptional') : t('gatewayCredentialHelp')} /></label>
        </FieldGrid>
        {hostHint && <p className="muted m-0 text-xs">{t('providerDockerHint')}</p>}
        <p className="muted m-0 text-xs">{t('credential')} {secret.action === 'removed' ? t('providerKeyWillRemove') : provider.credential_configured ? t('configured') : t('notConfigured')}{provider.destination_id ? ` · ${provider.destination_id}` : ''}. {t('credentialNeverShown')}</p>
        {provider.policy_denied && <p className="error m-0" role="alert">{t('blockedEndpoint')}</p>}
        <div className="flex flex-wrap gap-2">
          <Button type="button" className="secondary" disabled={disabled || discoveringId === provider.id || !provider.base_url || (isNew && !provider.local && !secret.key)} onClick={() => onDiscover(provider)}>{discoveringId === provider.id ? t('checkingConnection') : t('providerDiscover')}</Button>
          {provider.credential_configured && <Button type="button" className="secondary" disabled={disabled} onClick={() => onSecret(provider.id, secret.action === 'removed' ? { key: '', action: 'unchanged' } : { key: '', action: 'removed' })}>{secret.action === 'removed' ? t('providerKeyKeep') : t('removeGatewayCredential')}</Button>}
        </div>
        <div role="status" aria-live="polite">{found && <p className="muted m-0"><Check className="inline size-4" aria-hidden="true" /> {t('providerModelsFound', { count: found.length })}{found.length > 0 && <><br />{t('availableIds')} {found.join(', ')}</>}</p>}</div>
        {discoverError?.id === provider.id && <p className="error m-0" role="alert"><X className="inline size-4" aria-hidden="true" /> {discoverError.text}</p>}
      </div>;
    })}
    <div><Button type="button" className="secondary" disabled={disabled || providers.length >= maxProviders} onClick={() => onChange([...providers, newProvider('openai', providers)])}><Plus className="size-4" aria-hidden="true" /> {t('providerAdd')}</Button>
      {providers.length >= maxProviders && <p className="muted m-0 mt-2 text-xs">{t('providerMax', { max: maxProviders })}</p>}</div>
  </div>;
}
