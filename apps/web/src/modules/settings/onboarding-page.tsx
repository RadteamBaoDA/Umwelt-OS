'use client';

import { CheckIcon } from 'lucide-react';
import Link from 'next/link';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslations } from 'next-intl';
import { useState } from 'react';
import { Button } from '@/components/ui/button';
import { ApiError, apiRequest } from '@/core/api';
import { useWorkspaceSession } from '@/core/app-shell/workspace-shell';
import { listSources, StarterPackWizard } from '@/modules/sources';
import { getSearchIndexStatus } from '@/modules/search';
import { AISettingsWorkspace } from './ai-settings';
import { OnboardingTopics } from './onboarding-topics';
import { getOnboardingState, saveOnboardingStep, type OnboardingStep } from './onboarding-api';
import { BareButton } from '@/components/bare-button';

type ReadinessAI = {
  providers: { base_url: string | null; credential_configured: boolean; local: boolean }[];
  endpoint_policy_denied: boolean;
  defaults: { chat?: { model: string; destination: string } | null; embedding?: { model: string; destination: string } | null };
  capabilities: { role: string; capability: string; result: string; expires_at: string }[];
  privacy: { allow_remote_reasoning: boolean; allow_remote_embeddings: boolean };
};

const steps: {
  id: OnboardingStep;
  label: 'step.ai' | 'step.capability' | 'step.sources' | 'step.samples' | 'step.indexing' | 'step.complete';
}[] = [
  { id: 'ai_privacy', label: 'step.ai' },
  { id: 'capability', label: 'step.capability' },
  { id: 'sources', label: 'step.sources' },
  { id: 'sample_or_import', label: 'step.samples' },
  { id: 'indexing', label: 'step.indexing' },
  { id: 'complete', label: 'step.complete' },
];

type Translate = ReturnType<typeof useTranslations<'onboarding'>>;

/** Minimal shape of a TanStack query result that the step panels read for loading/error wording. */
type QueryState = { isPending: boolean; isError: boolean; error: unknown };

type IndexQuery = QueryState & { data?: { status: string; indexed_items: number; failed_items: number } };

/** Maps the owner SearchIndexStatus state to the matching localized progress message. */
function indexStateLabel(t: Translate, state: string): string {
  switch (state) {
    case 'queued':
      return t('index.queued');
    case 'running':
      return t('index.running');
    case 'active':
      return t('index.active');
    case 'failed':
      return t('index.failed');
    case 'unavailable':
      return t('index.unavailable');
    case 'retired':
      return t('index.retired');
    default:
      return t('unknown');
  }
}

/** Distinguishes a network failure from an API response that reports unavailable state. */
const networkUnavailable = (error: unknown) => !(error instanceof ApiError);

/** Labels whether one live readiness query completed and met its owner-defined condition. */
const readinessLabel = (t: Translate, ready: boolean, unresolved: boolean) =>
  unresolved ? t('unknown') : t(ready ? 'ready' : 'needsSetup');

type Readiness = {
  aiReady: boolean;
  chatCapability: boolean;
  chatRemote: boolean;
  aiUnresolved: boolean;
  indexUnresolved: boolean;
  semanticUnavailable: boolean;
};

/** True when a usable remote chat route exists: a configured provider, no endpoint-policy denial, remote reasoning allowed and a remote chat default. */
function isAiReady(data: ReadinessAI | undefined): boolean {
  const chatDefault = data?.defaults?.chat;
  return Boolean(
    data?.providers?.some((item) => item.base_url && (item.credential_configured || item.local)) &&
    !data.endpoint_policy_denied &&
    data.privacy.allow_remote_reasoning &&
    chatDefault?.model &&
    chatDefault.destination === 'remote',
  );
}

/** True when a probe recorded a supported chat capability for the chat role. */
function hasChatCapability(data: ReadinessAI | undefined): boolean {
  return (
    data?.capabilities.some(
      (capability) => capability.role === 'chat' && capability.capability === 'chat' && capability.result === 'supported',
    ) ?? false
  );
}

/**
 * SearchIndexStatus omits embedding version and gateway identity. Matching model IDs alone
 * cannot establish the execution fences checked by semantic search, so active generations
 * with a matching model remain unknown instead of being presented as usable.
 */
function isSemanticUnavailable(
  data: ReadinessAI | undefined,
  indexData: { status: string; indexed_items: number; model_id?: string | null } | undefined,
): boolean {
  const embeddingDefault = data?.defaults?.embedding;
  const aiBlocks =
    data !== undefined &&
    (!data.privacy.allow_remote_embeddings ||
      data.endpoint_policy_denied ||
      !embeddingDefault?.model ||
      embeddingDefault.destination !== 'remote');
  const indexBlocks =
    indexData !== undefined &&
    (indexData.status !== 'active' ||
      indexData.indexed_items === 0 ||
      Boolean(embeddingDefault?.model && indexData.model_id !== embeddingDefault.model));
  return aiBlocks || indexBlocks;
}

/**
 * Derives every readiness flag the step panels show from the live AI settings and search index queries.
 * Pure; unresolved (pending, errored or empty) queries are reported through the `*Unresolved` flags.
 */
function deriveReadiness(
  ai: { data?: ReadinessAI; isPending: boolean; isError: boolean },
  index: { data?: { status: string; indexed_items: number; model_id?: string | null }; isPending: boolean; isError: boolean },
): Readiness {
  return {
    aiReady: isAiReady(ai.data),
    chatCapability: hasChatCapability(ai.data),
    chatRemote: ai.data?.defaults?.chat?.destination === 'remote',
    aiUnresolved: ai.isPending || ai.isError || !ai.data,
    indexUnresolved: index.isPending || index.isError || !index.data,
    semanticUnavailable: isSemanticUnavailable(ai.data, index.data),
  };
}

type AiStepProps = {
  t: Translate;
  ai: QueryState;
  aiReady: boolean;
  providerOpen: boolean;
  saving: boolean;
  onToggleProvider: () => void;
  onSkip: () => void;
};

/** "AI and privacy" step: readiness line, a toggle that embeds the Models workspace, and the local-only skip. */
function AiStep({ t, ai, aiReady, providerOpen, saving, onToggleProvider, onSkip }: AiStepProps) {
  return (
    <>
      <h2>{t('ai.title')}</h2>
      <p>{t('ai.text')}</p>
      <p role="status">
        {aiReady && !ai.isPending && !ai.isError
          ? `${readinessLabel(t, aiReady, false)} · `
          : ai.isPending || ai.isError
            ? `${readinessLabel(t, aiReady, true)} · `
            : ''}
        {ai.isError && networkUnavailable(ai.error)
          ? t('offline')
          : ai.isPending || ai.isError
            ? t('unknown')
            : t(aiReady ? 'ai.ready' : 'ai.needsSetup')}
      </p>
      <div className="form-actions">
        <Button type="button" variant="outline" aria-expanded={providerOpen} onClick={onToggleProvider}>
          {t(providerOpen ? 'hideProviderForm' : 'addProviderHere')}
        </Button>
        <Button type="button" variant="ghost" disabled={saving} onClick={onSkip}>
          {t('skipLocalOnly')}
        </Button>
      </div>
      {providerOpen && (
        <div className="w-full">
          <AISettingsWorkspace />
        </div>
      )}
    </>
  );
}

/** "Capability" step: reports whether the chat capability probe succeeded against a remote default and links to Models. */
function CapabilityStep({ t, ai, readiness }: { t: Translate; ai: QueryState; readiness: Readiness }) {
  const { chatCapability, chatRemote, aiUnresolved } = readiness;
  return (
    <>
      <h2>{t('capability.title')}</h2>
      <p>{t('capability.text')}</p>
      <p role="status">
        {readinessLabel(t, chatCapability && chatRemote, aiUnresolved)} ·{' '}
        {ai.isError && networkUnavailable(ai.error)
          ? t('offline')
          : aiUnresolved
            ? t('unknown')
            : chatCapability
              ? t('capability.ready')
              : t('capability.needsProbe')}
      </p>
      <Button asChild variant="outline">
        <Link href="/settings/models">{t('open.aiSettings')}</Link>
      </Button>
    </>
  );
}

type SourcesStepProps = { t: Translate; sources: QueryState; activeSources: number; onOpenStarter: () => void };

/** "Sources" step: active-source count line, starter-pack launcher, Sources link and the topics picker. */
function SourcesStep({ t, sources, activeSources, onOpenStarter }: SourcesStepProps) {
  return (
    <>
      <h2>{t('sourcesTitle')}</h2>
      <p>{t('sourcesText')}</p>
      <p role="status">
        {sources.isPending
          ? t('unknown')
          : sources.isError && networkUnavailable(sources.error)
            ? t('offline')
            : sources.isError
              ? t('source.unknown')
              : activeSources
                ? t('source.count', { count: activeSources })
                : t('source.none')}
      </p>
      <div className="form-actions">
        <Button type="button" onClick={onOpenStarter}>
          {t('starterPacks')}
        </Button>
        <Button asChild variant="outline">
          <Link href="/sources">{t('open.sources')}</Link>
        </Button>
      </div>
      <OnboardingTopics />
    </>
  );
}

type SampleStepProps = {
  t: Translate;
  dataChoice: 'sample' | 'personal_import' | null;
  saving: boolean;
  onChoose: (choice: 'sample' | 'personal_import') => void;
};

/** "Sample or import" step: two toggle buttons persisting the data choice through `onChoose`. */
function SampleStep({ t, dataChoice, saving, onChoose }: SampleStepProps) {
  return (
    <>
      <h2>{t('samplesTitle')}</h2>
      <p>{t('samplesText')}</p>
      <div className="form-actions">
        <Button
          type="button"
          aria-pressed={dataChoice === 'sample'}
          className={dataChoice === 'sample' ? '' : 'secondary'}
          disabled={saving}
          onClick={() => onChoose('sample')}
        >
          {t(dataChoice === 'sample' ? 'sampleSelected' : 'chooseSample')}
        </Button>
        <Button
          type="button"
          aria-pressed={dataChoice === 'personal_import'}
          className={dataChoice === 'personal_import' ? '' : 'secondary'}
          disabled={saving}
          onClick={() => onChoose('personal_import')}
        >
          {t(dataChoice === 'personal_import' ? 'importSelected' : 'choosePersonalImport')}
        </Button>
        <Button asChild variant="outline">
          <Link href="/sources">{t('open.sources')}</Link>
        </Button>
      </div>
    </>
  );
}

/** Lexical index progress text: offline/unknown while unresolved, otherwise state, indexed count and failure count. */
function indexProgressLine(t: Translate, index: IndexQuery, unresolved: boolean): string {
  if (unresolved) return index.isError && networkUnavailable(index.error) ? t('offline') : t('unknown');
  return `${indexStateLabel(t, index.data?.status ?? 'unavailable')} · ${t('index.count', { count: index.data?.indexed_items ?? 0 })} · ${t('index.failures', { count: index.data?.failed_items ?? 0 })}`;
}

/** "Indexing" step: lexical index progress, the semantic-search availability note and links to Search and Sources. */
function IndexingStep({ t, index, readiness }: { t: Translate; index: IndexQuery; readiness: Readiness }) {
  const { aiUnresolved, indexUnresolved, semanticUnavailable } = readiness;
  return (
    <>
      <h2>{t('indexingTitle')}</h2>
      <p>{t('indexingText')}</p>
      <p role="status">{indexProgressLine(t, index, indexUnresolved)}</p>
      <p role="status">
        {aiUnresolved || indexUnresolved
          ? t('unknown')
          : semanticUnavailable
            ? t('semantic.unavailable')
            : t('semantic.compatibilityUnknown')}
      </p>
      {!aiUnresolved && !indexUnresolved && !semanticUnavailable ? <p>{t('semantic.compatibilityHelp')}</p> : null}
      <p role="status">{t('lexicalIndependent')}</p>
      <div className="form-actions">
        <Button asChild variant="outline">
          <Link href="/search">{t('open.search')}</Link>
        </Button>
        <Button asChild variant="outline">
          <Link href="/sources">{t('open.sources')}</Link>
        </Button>
      </div>
    </>
  );
}

/** Final "complete" step: confirmation text and a Dashboard link. */
function CompleteStep({ t }: { t: Translate }) {
  return (
    <>
      <h2>{t('completeTitle')}</h2>
      <p>{t('completeText')}</p>
      <p role="status">{t('finished')}</p>
      <Button asChild>
        <Link href="/dashboard">{t('open.dashboard')}</Link>
      </Button>
    </>
  );
}

type StepRailProps = {
  t: Translate;
  done: boolean;
  currentIndex: number;
  currentId: OnboardingStep;
  saving: boolean;
  onMove: (step: OnboardingStep) => void;
};

/** Left rail listing every step; completed steps and the current one are reachable, upcoming ones are disabled and hidden on phones. */
function StepRail({ t, done, currentIndex, currentId, saving, onMove }: StepRailProps) {
  return (
    <nav aria-label={t('currentStep')} className="flex-[0_0_260px] max-[720px]:flex-[1_1_100%]">
      <ol className="grid gap-1">
        {/* eslint-disable-next-line complexity -- JSX presentational markup: the score counts every conditional-render branch (&&, ?:, ??) of this row; it holds no branching business logic, so splitting would only relocate the same render conditions */}
        {steps.map((step, position) => {
          const complete = done || position < currentIndex;
          const current = !done && step.id === currentId;
          // On phones only completed and current steps are listed; "Step n of N" in the header covers the rest.
          return (
            <li key={step.id} className={complete || current ? undefined : 'max-[720px]:hidden'}>
              <BareButton
                type="button"
                aria-current={current ? 'step' : undefined}
                disabled={saving || done || position > currentIndex}
                onClick={() => onMove(step.id)}
                className={`flex min-h-9 pointer-coarse:min-h-11 w-full items-start gap-3 rounded-md border px-3 py-2.5 text-left text-sm disabled:cursor-default ${current ? 'border-border bg-card' : 'border-transparent hover:bg-secondary'}`}
              >
                <span
                  aria-hidden="true"
                  className={`w-5 shrink-0 text-sm tabular-nums ${complete ? 'text-primary' : current ? 'font-semibold text-foreground' : 'text-muted-foreground'}`}
                >
                  {complete ? <CheckIcon className="size-3.5" /> : position + 1}
                </span>
                <span className="grid">
                  <strong className={current ? 'font-semibold' : 'font-medium'}>{t(step.label)}</strong>
                  {(complete || current) && (
                    <span className="text-muted-foreground text-xs">{t(complete ? 'step.done' : 'step.current')}</span>
                  )}
                </span>
              </BareButton>
            </li>
          );
        })}
      </ol>
      <p className="text-muted-foreground mt-3 text-xs">{t('personalSourcesNote')}</p>
    </nav>
  );
}

type StepFooterProps = {
  t: Translate;
  currentIndex: number;
  currentId: OnboardingStep;
  dataChoiceMissing: boolean;
  saving: boolean;
  onMove: (step: OnboardingStep) => void;
};

/** Previous/Next(Finish) buttons; Next stays disabled on the sample step until a data choice exists. */
function StepFooter({ t, currentIndex, currentId, dataChoiceMissing, saving, onMove }: StepFooterProps) {
  return (
    <div className="flex flex-wrap justify-end gap-2 border-t border-border pt-5">
      {currentIndex > 0 && (
        <Button type="button" variant="outline" disabled={saving} onClick={() => onMove(steps[currentIndex - 1].id)}>
          {t('previous')}
        </Button>
      )}
      <Button
        type="button"
        disabled={saving || (currentId === 'sample_or_import' && dataChoiceMissing)}
        onClick={() => onMove(steps[Math.min(steps.length - 1, currentIndex + 1)].id)}
      >
        {currentId === 'indexing' ? t('finish') : t('next')}
      </Button>
    </div>
  );
}

type StepContentProps = {
  t: Translate;
  stepId: OnboardingStep;
  ai: QueryState;
  sources: QueryState;
  index: IndexQuery;
  readiness: Readiness;
  activeSources: number;
  dataChoice: 'sample' | 'personal_import' | null;
  providerOpen: boolean;
  saving: boolean;
  onToggleProvider: () => void;
  onOpenStarter: () => void;
  onMove: (step: OnboardingStep, dataChoice?: 'sample' | 'personal_import' | null) => void;
};

/** Selects and renders the panel for the current step; each panel persists progress only through `onMove`. */
function StepContent({
  t,
  stepId,
  ai,
  sources,
  index,
  readiness,
  activeSources,
  dataChoice,
  providerOpen,
  saving,
  onToggleProvider,
  onOpenStarter,
  onMove,
}: StepContentProps) {
  switch (stepId) {
    case 'ai_privacy':
      return (
        <AiStep
          t={t}
          ai={ai}
          aiReady={readiness.aiReady}
          providerOpen={providerOpen}
          saving={saving}
          onToggleProvider={onToggleProvider}
          onSkip={() => onMove('capability')}
        />
      );
    case 'capability':
      return <CapabilityStep t={t} ai={ai} readiness={readiness} />;
    case 'sources':
      return <SourcesStep t={t} sources={sources} activeSources={activeSources} onOpenStarter={onOpenStarter} />;
    case 'sample_or_import':
      return <SampleStep t={t} dataChoice={dataChoice} saving={saving} onChoose={(choice) => onMove('sample_or_import', choice)} />;
    case 'indexing':
      return <IndexingStep t={t} index={index} readiness={readiness} />;
    default:
      return <CompleteStep t={t} />;
  }
}

/**
 * Renders resumable setup steps and live readiness from the owning AI, Sources, and Search APIs.
 * The AI step embeds the Settings › Models workspace on demand (same save flow and query cache) and offers a
 * "skip, use local only" path that advances without a provider; the step rail omits the caption on upcoming steps.
 */
export function OnboardingPage() {
  const t = useTranslations('onboarding');
  const { csrfToken } = useWorkspaceSession();
  const client = useQueryClient();
  const [providerOpen, setProviderOpen] = useState(false);
  const [starterOpen, setStarterOpen] = useState(false);
  const progress = useQuery({ queryKey: ['onboarding-progress'], queryFn: ({ signal }) => getOnboardingState(signal) });
  const ai = useQuery({ queryKey: ['ai-settings'], queryFn: () => apiRequest<ReadinessAI>('/api/v1/settings/ai') });
  const sources = useQuery({ queryKey: ['onboarding-sources'], queryFn: () => listSources() });
  const index = useQuery({ queryKey: ['search-index'], queryFn: () => getSearchIndexStatus() });
  const save = useMutation({
    mutationFn: (input: { step: OnboardingStep; dataChoice?: 'sample' | 'personal_import' | null }) =>
      saveOnboardingStep(input.step, progress.data?.configuration_revision ?? 1, csrfToken, input.dataChoice),
    onSuccess: (value) => client.setQueryData(['onboarding-progress'], value),
  });

  if (progress.isPending) return <section className="rounded-lg border bg-card p-6 skeleton" aria-label={t('loading')} />;
  if (progress.isError || !progress.data)
    return (
      <section className="rounded-lg border bg-card p-6" aria-labelledby="onboarding-title">
        <h1 id="onboarding-title">{t('title')}</h1>
        <p className="error" role="alert">
          {t('loadFailed')}
        </p>
        <Button type="button" variant="outline" onClick={() => progress.refetch()}>
          {t('retry')}
        </Button>
      </section>
    );

  const currentIndex = steps.findIndex((step) => step.id === progress.data.current_step);
  const currentStep = steps[Math.max(0, currentIndex)];
  const activeSources = sources.data?.items.filter((source) => source.status === 'active').length ?? 0;
  const readiness = deriveReadiness(ai, index);

  /** Saves the selected next step before allowing the owner to continue. */
  const move = (step: OnboardingStep, dataChoice = progress.data.data_choice) => save.mutate({ step, dataChoice });
  const stepContent = (
    <StepContent
      t={t}
      stepId={currentStep.id}
      ai={ai}
      sources={sources}
      index={index}
      readiness={readiness}
      activeSources={activeSources}
      dataChoice={progress.data.data_choice}
      providerOpen={providerOpen}
      saving={save.isPending}
      onToggleProvider={() => setProviderOpen((open) => !open)}
      onOpenStarter={() => setStarterOpen(true)}
      onMove={move}
    />
  );

  const done = Boolean(progress.data.completed_at);
  return (
    <div className="mx-auto flex w-full max-w-[1200px] flex-wrap items-start gap-6">
      <StepRail t={t} done={done} currentIndex={currentIndex} currentId={currentStep.id} saving={save.isPending} onMove={move} />
      <section
        className="flex min-w-0 flex-[999_1_560px] flex-col gap-6 rounded-lg border border-border bg-card p-6 max-[720px]:p-4"
        aria-labelledby="onboarding-title"
      >
        <header>
          <h1 id="onboarding-title" className="mt-0 mb-1 text-2xl font-semibold leading-tight">
            {t('title')}
          </h1>
          <p className="text-muted-foreground m-0">{t('description')}</p>
          <p role="status" aria-live="polite" className="mt-2 mb-0 text-sm font-semibold">
            {save.isPending ? t('saving') : done ? t('finished') : t('progress', { current: currentIndex + 1, total: steps.length })}
          </p>
        </header>
        <div className="grid justify-items-start gap-4 [&>h2]:m-0 [&>h2]:text-lg [&>h2]:font-semibold" aria-live="polite">
          {stepContent}
        </div>
        {save.error && (
          <p className="error" role="alert">
            {t('saveFailed')}
          </p>
        )}
        {starterOpen && (
          <StarterPackWizard
            onClose={() => setStarterOpen(false)}
            onChanged={() => {
              void client.invalidateQueries({ queryKey: ['onboarding-sources'] });
            }}
          />
        )}
        {!done && (
          <StepFooter
            t={t}
            currentIndex={currentIndex}
            currentId={currentStep.id}
            dataChoiceMissing={progress.data.data_choice === null}
            saving={save.isPending}
            onMove={move}
          />
        )}
      </section>
    </div>
  );
}
