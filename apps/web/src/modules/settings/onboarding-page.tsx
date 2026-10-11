'use client';

import { CheckIcon } from 'lucide-react';
import Link from 'next/link';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslations } from 'next-intl';
import { Button } from '@/components/ui/button';
import { ApiError, apiRequest } from '@/core/api';
import { useWorkspaceSession } from '@/core/app-shell/workspace-shell';
import { listSources } from '@/modules/sources/api';
import { getSearchIndexStatus } from '@/modules/search/api';
import { OnboardingTopics } from './onboarding-topics';
import { getOnboardingState, saveOnboardingStep, type OnboardingStep } from './onboarding-api';

type ReadinessAI = {
  providers: { base_url: string | null; credential_configured: boolean; local: boolean }[];
  endpoint_policy_denied: boolean;
  chat_alias: string;
  aliases: Record<string, { model: string; destination: string }>;
  capabilities: { alias: string; capability: string; result: string; expires_at: string }[];
  privacy: { allow_remote_reasoning: boolean; allow_remote_embeddings: boolean };
};

const steps: { id: OnboardingStep; label: 'stepAi' | 'stepCapability' | 'stepSources' | 'stepSamples' | 'stepIndexing' | 'stepComplete' }[] = [
  { id: 'ai_privacy', label: 'stepAi' },
  { id: 'capability', label: 'stepCapability' },
  { id: 'sources', label: 'stepSources' },
  { id: 'sample_or_import', label: 'stepSamples' },
  { id: 'indexing', label: 'stepIndexing' },
  { id: 'complete', label: 'stepComplete' },
];

/** Renders resumable setup steps and live readiness from the owning AI, Sources, and Search APIs. */
export function OnboardingPage() {
  const t = useTranslations('onboarding');
  const { csrfToken } = useWorkspaceSession();
  const client = useQueryClient();
  const progress = useQuery({ queryKey: ['onboarding-progress'], queryFn: ({ signal }) => getOnboardingState(signal) });
  const ai = useQuery({ queryKey: ['ai-settings'], queryFn: () => apiRequest<ReadinessAI>('/api/v1/settings/ai') });
  const sources = useQuery({ queryKey: ['onboarding-sources'], queryFn: () => listSources() });
  const index = useQuery({ queryKey: ['search-index'], queryFn: () => getSearchIndexStatus() });
  const save = useMutation({
    mutationFn: (input: { step: OnboardingStep; dataChoice?: 'sample' | 'personal_import' | null }) => saveOnboardingStep(input.step, progress.data?.configuration_revision ?? 1, csrfToken, input.dataChoice),
    onSuccess: (value) => client.setQueryData(['onboarding-progress'], value),
  });

  if (progress.isPending) return <section className="content-panel skeleton" aria-label={t('loading')} />;
  if (progress.isError || !progress.data) return <section className="content-panel" aria-labelledby="onboarding-title">
    <h1 id="onboarding-title">{t('title')}</h1><p className="error" role="alert">{t('loadFailed')}</p>
    <Button type="button" className="secondary" onClick={() => progress.refetch()}>{t('retry')}</Button>
  </section>;

  const currentIndex = steps.findIndex((step) => step.id === progress.data.current_step);
  const currentStep = steps[Math.max(0, currentIndex)];
  const activeSources = sources.data?.items.filter((source) => source.status === 'active').length ?? 0;
  const chatAlias = ai.data?.aliases[ai.data.chat_alias];
  const aiReady = Boolean(ai.data?.providers?.some((item) => item.base_url && (item.credential_configured || item.local))
    && !ai.data.endpoint_policy_denied && ai.data.privacy.allow_remote_reasoning
    && chatAlias?.model && chatAlias.destination === 'remote');
  const chatCapability = ai.data?.capabilities.some((capability) => capability.alias === ai.data?.chat_alias
    && capability.capability === 'chat' && capability.result === 'supported') ?? false;
  const aiUnresolved = ai.isPending || ai.isError || !ai.data;
  const indexUnresolved = index.isPending || index.isError || !index.data;
  const embeddingAlias = ai.data?.aliases.embedding;
  // SearchIndexStatus omits embedding version and gateway identity. Matching model IDs alone
  // cannot establish the execution fences checked by semantic search, so active generations
  // with a matching model remain unknown instead of being presented as usable.
  const semanticUnavailable = Boolean(ai.data && (
    !ai.data.privacy.allow_remote_embeddings || ai.data.endpoint_policy_denied
    || !embeddingAlias?.model || embeddingAlias.destination !== 'remote'
  )) || Boolean(index.data && (
    index.data.status !== 'active' || index.data.indexed_items === 0
    || Boolean(embeddingAlias?.model && index.data.model_id !== embeddingAlias.model)
  ));
  /** Maps the owner SearchIndexStatus state to the matching localized progress message. */
  const indexState = (state: string) => {
    switch (state) {
      case 'queued': return t('indexQueued');
      case 'running': return t('indexRunning');
      case 'active': return t('indexActive');
      case 'failed': return t('indexFailed');
      case 'unavailable': return t('indexUnavailable');
      case 'retired': return t('indexRetired');
      default: return t('unknown');
    }
  };
  /** Distinguishes a network failure from an API response that reports unavailable state. */
  const networkUnavailable = (error: unknown) => !(error instanceof ApiError);
  /** Labels whether one live readiness query completed and met its owner-defined condition. */
  const status = (ready: boolean, unresolved: boolean) => unresolved ? t('unknown') : t(ready ? 'ready' : 'needsSetup');

  /** Saves the selected next step before allowing the owner to continue. */
  const move = (step: OnboardingStep, dataChoice = progress.data.data_choice) => save.mutate({ step, dataChoice });
  const stepContent = currentStep.id === 'ai_privacy'
    ? <><h2>{t('aiTitle')}</h2><p>{t('aiText')}</p><p role="status">{status(aiReady, ai.isPending || ai.isError)} · {ai.isError && networkUnavailable(ai.error) ? t('offline') : ai.isPending || ai.isError ? t('unknown') : t(aiReady ? 'aiReady' : 'aiNeedsSetup')}</p><Button asChild className="secondary"><Link href="/settings/ai">{t('openAiSettings')}</Link></Button></>
    : currentStep.id === 'capability'
      ? <><h2>{t('capabilityTitle')}</h2><p>{t('capabilityText')}</p><p role="status">{status(chatCapability && chatAlias?.destination === 'remote', aiUnresolved)} · {ai.isError && networkUnavailable(ai.error) ? t('offline') : aiUnresolved ? t('unknown') : chatCapability ? t('capabilityReady') : t('capabilityNeedsProbe')}</p><Button asChild className="secondary"><Link href="/settings/ai">{t('openAiSettings')}</Link></Button></>
      : currentStep.id === 'sources'
        ? <><h2>{t('sourcesTitle')}</h2><p>{t('sourcesText')}</p><p role="status">{sources.isPending ? t('unknown') : sources.isError && networkUnavailable(sources.error) ? t('offline') : sources.isError ? t('sourceUnknown') : activeSources ? t('sourceCount', { count: activeSources }) : t('sourceNone')}</p><Button asChild className="secondary"><Link href="/sources">{t('openSources')}</Link></Button><OnboardingTopics /></>
        : currentStep.id === 'sample_or_import'
          ? <><h2>{t('samplesTitle')}</h2><p>{t('samplesText')}</p><p><code>{t('seedCommand')}</code></p><div className="form-actions"><Button type="button" aria-pressed={progress.data.data_choice === 'sample'} className={progress.data.data_choice === 'sample' ? '' : 'secondary'} disabled={save.isPending} onClick={() => move('sample_or_import', 'sample')}>{t(progress.data.data_choice === 'sample' ? 'sampleSelected' : 'chooseSample')}</Button><Button type="button" aria-pressed={progress.data.data_choice === 'personal_import'} className={progress.data.data_choice === 'personal_import' ? '' : 'secondary'} disabled={save.isPending} onClick={() => move('sample_or_import', 'personal_import')}>{t(progress.data.data_choice === 'personal_import' ? 'importSelected' : 'choosePersonalImport')}</Button><Button asChild className="secondary"><Link href="/sources">{t('openSources')}</Link></Button></div></>
          : currentStep.id === 'indexing'
            ? <><h2>{t('indexingTitle')}</h2><p>{t('indexingText')}</p><p role="status">{indexUnresolved ? index.isError && networkUnavailable(index.error) ? t('offline') : t('unknown') : `${indexState(index.data?.status ?? 'unavailable')} · ${t('indexCount', { count: index.data?.indexed_items ?? 0 })} · ${t('indexFailures', { count: index.data?.failed_items ?? 0 })}`}</p><p role="status">{aiUnresolved || indexUnresolved ? t('unknown') : semanticUnavailable ? t('semanticUnavailable') : t('semanticCompatibilityUnknown')}</p>{!aiUnresolved && !indexUnresolved && !semanticUnavailable ? <p>{t('semanticCompatibilityHelp')}</p> : null}<p role="status">{t('lexicalIndependent')}</p><div className="form-actions"><Button asChild className="secondary"><Link href="/search">{t('openSearch')}</Link></Button><Button asChild className="secondary"><Link href="/sources">{t('openSources')}</Link></Button></div></>
            : <><h2>{t('completeTitle')}</h2><p>{t('completeText')}</p><p role="status">{t('finished')}</p><Button asChild><Link href="/app">{t('openDashboard')}</Link></Button></>;

  const done = Boolean(progress.data.completed_at);
  return <div className="mx-auto flex w-full max-w-[1200px] flex-wrap items-start gap-6">
      <nav aria-label={t('currentStep')} className="flex-[0_0_260px] max-[720px]:flex-[1_1_100%]">
        <ol className="grid gap-1">
          {steps.map((step, position) => {
            const complete = done || position < currentIndex;
            const current = !done && step.id === currentStep.id;
            return <li key={step.id}>
              <button type="button" aria-current={current ? 'step' : undefined} disabled={save.isPending || done || position > currentIndex} onClick={() => move(step.id)}
                className={`flex min-h-11 w-full items-start gap-3 rounded-[9px] border px-3 py-2.5 text-left text-sm disabled:cursor-default ${current ? 'border-border bg-card' : 'border-transparent hover:bg-secondary'}`}>
                <span aria-hidden="true" className={`inline-flex size-6 shrink-0 items-center justify-center rounded-full border text-xs font-bold ${complete ? 'border-primary bg-primary text-primary-foreground' : current ? 'border-primary text-primary' : 'border-border text-muted-foreground'}`}>{complete ? <CheckIcon className="size-3.5" /> : position + 1}</span>
                <span className="grid"><strong className={current ? 'font-semibold' : 'font-medium'}>{t(step.label)}</strong><span className="muted text-xs">{t(complete ? 'stepDone' : current ? 'stepCurrent' : 'stepUpcoming')}</span></span>
              </button>
            </li>;
          })}
        </ol>
        <p className="muted mt-3 text-xs">{t('personalSourcesNote')}</p>
      </nav>
      <section className="flex min-w-0 flex-[999_1_560px] flex-col gap-6 rounded-[18px] border border-border bg-card p-8 max-[720px]:p-5" aria-labelledby="onboarding-title">
        <header><span className="brand">{t('eyebrow')}</span><h1 id="onboarding-title" className="mt-1 mb-1 text-[30px] font-bold leading-tight tracking-[-0.04em]">{t('title')}</h1><p className="muted m-0">{t('description')}</p><p role="status" aria-live="polite" className="mt-2 mb-0 text-sm font-semibold">{save.isPending ? t('saving') : done ? t('finished') : t('progress', { current: currentIndex + 1, total: steps.length })}</p></header>
        <div className="grid gap-4" aria-live="polite">{stepContent}</div>
        {save.error && <p className="error" role="alert">{t('saveFailed')}</p>}
        {!done && <div className="flex flex-wrap justify-end gap-2 border-t border-border pt-5">
          <Button type="button" className="secondary" disabled={save.isPending || currentIndex <= 0} onClick={() => move(steps[currentIndex - 1].id)}>{t('previous')}</Button>
          <Button type="button" disabled={save.isPending || (currentStep.id === 'sample_or_import' && progress.data.data_choice === null)} onClick={() => move(steps[Math.min(steps.length - 1, currentIndex + 1)].id)}>
            {currentStep.id === 'indexing' ? t('finish') : t('next')}
          </Button>
        </div>}
      </section>
  </div>;
}
