'use client';

import * as React from 'react';
import Link from 'next/link';
import { useQuery } from '@tanstack/react-query';
import { useTranslations } from 'next-intl';
import { Button } from '@/components/ui/button';
import { apiRequest } from '@/core/api';

type GateProvider = { id: string; credential_configured: boolean; local: boolean; policy_denied: boolean; destination_id: string | null };
type GateSettings = {
  providers: GateProvider[];
  defaults?: { chat?: { model: string; provider_id?: string | null } | null };
  privacy?: { allow_remote_reasoning?: boolean; reasoning_destinations?: string[] };
};

/** Settings route where providers and chat model mappings are configured. */
export const MODELS_SETTINGS_HREF = '/settings/models';
/** Readiness is cached this long; a settings save writes the shared query so it refreshes at once. */
export const PROVIDER_READY_STALE_MS = 30_000;

/**
 * Decides whether the owner's chat default maps to a usable model: a mapped model on a provider that is not
 * policy-denied and is either local (Ollama, LM Studio) or has a stored credential plus remote-reasoning consent.
 * The per-destination allow-list is deliberately NOT checked here: the server falls back from
 * `reasoning_destinations` to `permitted_destinations` (core/model_gateway/policy.py), so mirroring only one list
 * could block a send the server would accept. The gate must only block when chat is certainly unconfigured; any
 * remaining policy denial still reaches the user through the normal run error.
 *
 * @param s - AI settings read from `/api/v1/settings/ai`.
 * @returns True when a chat send can resolve a model under current policy.
 */
export function isChatProviderReady(s: GateSettings): boolean {
  const mapping = s.defaults?.chat;
  if (!mapping?.model) return false;
  const providers = s.providers ?? [];
  const id = mapping.provider_id ?? providers.find((p) => p.id === 'default')?.id ?? providers[0]?.id;
  const provider = providers.find((p) => p.id === id);
  if (!provider || provider.policy_denied) return false;
  if (provider.local) return true;
  return provider.credential_configured && s.privacy?.allow_remote_reasoning === true;
}

/**
 * Reports whether chat may send. Fails open: loading, errored or disabled reads all report ready so a settings
 * outage never locks chat. Shares the `['ai-settings']` query, so saving settings refreshes it immediately.
 *
 * @param enabled - False skips the read (e.g. the quick drawer).
 * @returns True unless settings loaded and show no usable chat model.
 */
export function useChatProviderReady(enabled = true): boolean {
  const q = useQuery({
    queryKey: ['ai-settings'],
    queryFn: () => apiRequest<GateSettings>('/api/v1/settings/ai'),
    enabled,
    staleTime: PROVIDER_READY_STALE_MS,
    retry: false,
  });
  return !enabled || q.isError || !q.data || isChatProviderReady(q.data);
}

/**
 * Non-blocking inline alert shown when a send was held as a draft because no model provider is configured.
 *
 * @param onDismiss - Hides the alert; the draft stays in the composer.
 */
export function ProviderGateAlert({ onDismiss }: { onDismiss: () => void }) {
  const t = useTranslations('chat');
  return (
    <div role="alert" className="mx-4 mb-2 flex shrink-0 flex-wrap items-center gap-2 rounded-lg border border-line bg-surface p-3 text-sm text-text">
      <div className="min-w-0 flex-1">
        <p className="font-medium">{t('providerGateTitle')}</p>
        <p className="text-muted">{t('providerGateBody')}</p>
      </div>
      <Button asChild size="sm"><Link href={MODELS_SETTINGS_HREF}>{t('providerGateAction')}</Link></Button>
      <Button size="sm" variant="ghost" onClick={onDismiss}>{t('providerGateDismiss')}</Button>
    </div>
  );
}
