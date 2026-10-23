'use client';

import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslations } from 'next-intl';
import { useEffect, useRef, useState } from 'react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Label } from '@/components/ui/label';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { useWorkspaceSession } from '@/core/app-shell/workspace-shell';
import { ApiError } from '@/core/api';
import { safeHttpUrl } from '@/core/safe-url';
import { archiveSource, connectorKeys, disableBuiltinSource, enableBuiltinSource, listBuiltinSources, sourceKeys, type BuiltinCategory, type BuiltinSource, type DeclaredUse } from './api';
import { WizardShell } from './source-wizard';
import { SourceAiToggles, type SourceAiOptions } from './source-ai-toggles';
import { collectAndWait, waitForActivation } from './use-source-actions';

const CATEGORY_ORDER: BuiltinCategory[] = ['news', 'economy', 'hazards', 'space', 'security', 'tech'];
const CATEGORY_KEY = {
  news: 'builtinCategoryNews', economy: 'builtinCategoryEconomy', hazards: 'builtinCategoryHazards',
  space: 'builtinCategorySpace', security: 'builtinCategorySecurity', tech: 'builtinCategoryTech',
} as const;
const USES = [['personal', 'termsUsePersonal'], ['noncommercial', 'termsUseNoncommercial'], ['commercial', 'termsUseCommercial']] as const;

/**
 * Lists built-ins that were added earlier but are switched off, with a Resume action for each.
 * Resume reruns the same idempotent enable sequence as the wizard, so review-class built-ins also get
 * their operator review recorded. Renders nothing when no added built-in is paused.
 */
export function BuiltinResumeList({ onChanged }: { onChanged: () => void }) {
  const t = useTranslations('sources');
  const { csrfToken } = useWorkspaceSession();
  const queryClient = useQueryClient();
  const builtins = useQuery({ queryKey: connectorKeys.builtins, queryFn: ({ signal }) => listBuiltinSources(signal) });
  const [busyId, setBusyId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const paused = (builtins.data ?? []).filter((item) => item.source_id !== null && !item.enabled);
  if (paused.length === 0) return null;

  /** Re-enables one paused built-in with the personal deployment use, then refreshes built-in and source caches. */
  async function resume(item: BuiltinSource) {
    setBusyId(item.id);
    setError(null);
    try {
      await enableBuiltinSource(item.id, 'personal', csrfToken);
    } catch {
      setError(t('builtinResumeFailed'));
    } finally {
      setBusyId(null);
      void queryClient.invalidateQueries({ queryKey: connectorKeys.builtins });
      void queryClient.invalidateQueries({ queryKey: sourceKeys.all });
      onChanged();
    }
  }

  return <section className="flex flex-col gap-2" aria-label={t('builtinResumeTitle')}>
    <h3 className="m-0 text-sm font-semibold">{t('builtinResumeTitle')}</h3>
    {error && <p className="text-danger" role="alert">{error}</p>}
    {paused.map((item) => <div key={item.id} className="flex items-center justify-between gap-3 rounded-md border border-border p-3">
      <span className="font-medium">{item.label}</span>
      <Button variant="outline" disabled={busyId !== null} onClick={() => void resume(item)}>{busyId === item.id ? t('enabling') : t('builtinResume')}</Button>
    </div>)}
  </section>;
}

/** Groups built-ins by category in the fixed display order, dropping empty categories. */
function groupByCategory(items: BuiltinSource[]): [BuiltinCategory, BuiltinSource[]][] {
  return CATEGORY_ORDER.map((category): [BuiltinCategory, BuiltinSource[]] => [category, items.filter((item) => item.category === category)])
    .filter(([, rows]) => rows.length > 0);
}

/** Message key (in the `sources` namespace) for a failed built-in enable; 403 operator/terms codes get specific text. */
export function builtinEnableErrorKey(cause: unknown): 'builtinErrorOperator' | 'builtinErrorTerms' | 'builtinAlreadyAdded' | 'builtinErrorGeneric' {
  const message = cause instanceof ApiError ? cause.message : '';
  const code = cause instanceof ApiError ? cause.code : null;
  if (message === 'operator_required') return 'builtinErrorOperator';
  if (message === 'provider_terms_ineligible') return 'builtinErrorTerms';
  if (message === 'builtin_already_added' || code === 'builtin_already_added') return 'builtinAlreadyAdded';
  return 'builtinErrorGeneric';
}

/**
 * Terms and declared-use step for enabling one built-in: attribution, terms link, operator-review note,
 * deployment-use select and AI toggles. Controlled by the caller; shared by the wizard and the gadget editor dialog.
 * An `error` (already localized) renders as a role=alert line below the fields.
 */
export function BuiltinTermsFields({ builtin, use, onUseChange, aiOptions, onAiOptionsChange, error }: {
  builtin: BuiltinSource;
  use: DeclaredUse;
  onUseChange: (use: DeclaredUse) => void;
  aiOptions: SourceAiOptions;
  onAiOptionsChange: (options: SourceAiOptions) => void;
  error?: string | null;
}) {
  const t = useTranslations('sources');
  return <section className="grid gap-3 text-sm" aria-label={t('builtinDialogTitle', { name: builtin.label })}>
    <h3 className="m-0 text-base font-semibold">{t('builtinDialogTitle', { name: builtin.label })}</h3>
    <p className="text-muted-foreground">{t('builtinTermsHint')}</p>
    <p><strong>{t('guideAttribution')}:</strong> {builtin.attribution}</p>
    <p><a className="text-accent underline" href={safeHttpUrl(builtin.terms_url)} target="_blank" rel="noopener noreferrer">{t('providerTerms')}</a> · {t('termsVersion')}: {builtin.terms_checked_on}</p>
    {builtin.eligibility === 'review' && <p role="note">{t('builtinOperatorNote')}</p>}
    <div className="field"><Label htmlFor="builtin-declared-use">{t('termsDeclaredUse')}</Label>
      <Select value={use} onValueChange={(value) => onUseChange(value as DeclaredUse)}>
        <SelectTrigger id="builtin-declared-use"><SelectValue /></SelectTrigger>
        <SelectContent>{USES.map(([value, label]) => <SelectItem key={value} value={value}>{t(label)}</SelectItem>)}</SelectContent>
      </Select></div>
    <SourceAiToggles idPrefix="builtin-source" value={aiOptions} onChange={onAiOptionsChange} />
    {error && <p className="text-danger" role="alert">{error}</p>}
  </section>;
}

type TestState = { phase: 'idle' | 'activating' | 'collecting' | 'succeeded' | 'no_changes' | 'failed'; code?: string | null; runId?: string | null };

/**
 * Three-step dialog for adding one built-in source: pick, confirm terms and declared use, then enable and prove a fetch.
 * A built-in that already has a source (`source_id` set, active or paused) is shown disabled with an "Added" badge;
 * resume it from the table instead. Enabling records the terms acknowledgement server-side. If the test fetch has not
 * succeeded when the owner discards the dialog, the created source is paused and archived so no half-added source remains.
 */
export function BuiltinWizard({ onClose, onChanged }: { onClose: () => void; onChanged: () => void }) {
  const t = useTranslations('sources');
  const { csrfToken } = useWorkspaceSession();
  const queryClient = useQueryClient();
  const builtins = useQuery({ queryKey: connectorKeys.builtins, queryFn: ({ signal }) => listBuiltinSources(signal) });
  const [index, setIndex] = useState(0);
  const [selected, setSelected] = useState<BuiltinSource | null>(null);
  const [use, setUse] = useState<DeclaredUse>('personal');
  const [aiOptions, setAiOptions] = useState<SourceAiOptions>({ graph_enabled: true, rag_index_enabled: true });
  const [error, setError] = useState<string | null>(null);
  const [createdId, setCreatedId] = useState('');
  const [test, setTest] = useState<TestState>({ phase: 'idle' });
  const controller = useRef<AbortController | null>(null);
  useEffect(() => () => controller.current?.abort(), []);
  const verified = test.phase === 'succeeded' || test.phase === 'no_changes';
  const testing = test.phase === 'activating' || test.phase === 'collecting';

  /** Refreshes the built-in availability and the source table after the server state changed. */
  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: connectorKeys.builtins });
    void queryClient.invalidateQueries({ queryKey: sourceKeys.all });
    onChanged();
  };
  /** Maps a failed enable to the most specific localized message. */
  const fail = (cause: unknown) => setError(t(builtinEnableErrorKey(cause)));

  /** Waits for activation, runs one real collection and records the outcome; failures keep the dialog open for retry or discard. */
  async function runTest(sourceId: string) {
    controller.current?.abort();
    const ctl = new AbortController();
    controller.current = ctl;
    const current = () => controller.current === ctl && !ctl.signal.aborted;
    setTest({ phase: 'activating' });
    try {
      const activation = await waitForActivation(sourceId, ctl.signal);
      if (!activation || !current()) return;
      if (!activation.ok) { setTest({ phase: 'failed', code: activation.code }); return; }
      setTest({ phase: 'collecting' });
      const final = await collectAndWait(sourceId, csrfToken, { signal: ctl.signal });
      if (!final || !current()) return;
      if (final.status === 'succeeded' || final.status === 'no_changes') setTest({ phase: final.status, runId: final.ingestion_run_id });
      else setTest({ phase: 'failed', code: final.error_code ?? final.status });
      refresh();
    } catch (cause) {
      if (current()) setTest({ phase: 'failed', code: cause instanceof ApiError ? cause.message || `http_${cause.status}` : 'request_failed' });
    }
  }

  /** Enables the picked built-in when leaving the terms step, then starts the test fetch for the created source. */
  async function onNext(): Promise<boolean> {
    if (index !== 1 || !selected) return true;
    if (createdId) { void runTest(createdId); return true; }
    setError(null);
    try {
      const result = await enableBuiltinSource(selected.id, use, csrfToken, undefined, aiOptions);
      if (!result.source_id) { setError(t('builtinErrorGeneric')); return false; }
      setCreatedId(result.source_id);
      refresh();
      void runTest(result.source_id);
      return true;
    } catch (cause) {
      fail(cause);
      return false;
    }
  }

  /** Pauses and archives the unverified source this dialog created, then closes. */
  async function discard() {
    controller.current?.abort();
    if (selected) await disableBuiltinSource(selected.id, csrfToken);
    await archiveSource(createdId, csrfToken);
    refresh();
    onClose();
  }

  const steps = [
    { id: 'pick', title: t('stepBuiltinPick'), valid: selected !== null && selected.source_id === null },
    { id: 'terms', title: t('stepBuiltinTerms'), valid: true },
    { id: 'verify', title: t('stepVerify'), valid: verified },
  ];
  return <WizardShell title={t('wizardTitleBuiltin')} mode="add" steps={steps} stepIndex={index} onStepChange={setIndex} busy={testing} onNext={onNext} onRequestClose={onClose}
    final={{ label: t('done'), disabled: !verified, onClick: onClose }}
    discard={createdId ? { needed: !verified, onConfirm: discard } : undefined}>
    {index === 0 && <section className="flex flex-col gap-3" aria-label={t('stepBuiltinPick')}>
      <p className="text-muted-foreground">{t('builtinPickHint')}</p>
      {builtins.isPending && <p className="text-muted-foreground">{t('loading')}</p>}
      {builtins.isError && <p className="text-danger" role="alert">{t('builtinLoadFailed')} <Button variant="outline" onClick={() => builtins.refetch()}>{t('retry')}</Button></p>}
      {groupByCategory(builtins.data ?? []).map(([category, rows]) => <div key={category} className="flex flex-col gap-2" role="group" aria-label={t(CATEGORY_KEY[category])}>
        <h3 className="m-0 text-sm font-semibold uppercase text-muted-foreground">{t(CATEGORY_KEY[category])}</h3>
        {rows.map((row) => {
          const added = row.source_id !== null;
          return <button key={row.id} type="button" disabled={added} aria-pressed={selected?.id === row.id} onClick={() => { setSelected(row); setUse('personal'); setError(null); }}
            className={`flex w-full items-start justify-between gap-3 rounded-md border bg-transparent p-3 text-left disabled:cursor-not-allowed disabled:opacity-60 ${selected?.id === row.id ? 'border-primary' : 'border-border'}`}>
            <span className="min-w-0">
              <span className="m-0 flex flex-wrap items-center gap-2 font-medium">{row.label}
                {added && <Badge variant="secondary">{t('builtinAdded')}</Badge>}
                {row.execution === 'experimental' && <Badge variant="outline">{t('builtinExperimental')}</Badge>}
                {row.eligibility === 'review' && <Badge variant="secondary">{t('builtinReviewClass')}</Badge>}
              </span>
              <span className="block text-sm text-muted-foreground">{row.attribution}</span>
            </span>
          </button>;
        })}
      </div>)}
    </section>}
    {index === 1 && selected && <BuiltinTermsFields builtin={selected} use={use} onUseChange={setUse} aiOptions={aiOptions} onAiOptionsChange={setAiOptions} error={error} />}
    {index === 2 && <section className="flex flex-col gap-3" aria-live="polite" aria-label={t('verifyTitle')}>
      <h3 className="m-0 text-base font-semibold">{t('verifyTitle')}</h3>
      {test.phase === 'activating' && <p role="status">{t('verifyWaitingActivation')}</p>}
      {test.phase === 'collecting' && <p role="status">{t('verifyCollecting')}</p>}
      {test.phase === 'succeeded' && <p role="status">{t('verifySucceeded')}</p>}
      {test.phase === 'no_changes' && <p role="status">{t('verifyNoChanges')}</p>}
      {test.phase === 'failed' && <p className="error" role="alert">{t('verifyFailed', { code: test.code ?? 'unknown' })}</p>}
      {test.runId && <p className="text-muted-foreground">{t('verifyRunId', { id: test.runId })}</p>}
      {test.phase === 'failed' && createdId && <div><Button variant="outline" onClick={() => void runTest(createdId)}>{t('verifyRetry')}</Button></div>}
    </section>}
  </WizardShell>;
}
