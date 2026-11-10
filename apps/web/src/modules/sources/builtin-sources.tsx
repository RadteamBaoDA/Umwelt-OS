'use client';

import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslations } from 'next-intl';
import { useState } from 'react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Label } from '@/components/ui/label';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { useWorkspaceSession } from '@/core/app-shell/workspace-shell';
import { ApiError } from '@/core/api';
import { safeHttpUrl } from '@/core/safe-url';
import { archiveSource, connectorKeys, disableBuiltinSource, enableBuiltinSource, listBuiltinSources, sourceKeys, type BuiltinCategory, type BuiltinSource, type DeclaredUse, type RealtimeMode } from './api';
import { BuiltinOptions, type BuiltinOptionValues } from './builtin-options';
import { OPTION_SPECS } from './builtin-option-specs';
import { matchesShapes } from './shape-filter';
import type { DataShape } from '@/core/data-shapes';
import { WizardShell } from './source-wizard';
import type { SourceAiOptions } from './source-ai-toggles';
import { useBuiltinTest, type TestState } from './use-builtin-test';
import { BareButton } from '@/components/bare-button';

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
    {error && <p className="text-destructive" role="alert">{error}</p>}
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

const COLLECT_ERROR_CODES = ['provider_rate_limited', 'provider_collection_failed', 'retry_exhausted', 'transient_failure', 'collection_unsupported', 'admission_expired', 'executor_error', 'invalid_normalization_record', 'record_exceeds_normalization_limit', 'cancelled', 'source_generation_changed', 'required_credential_binding_unresolved', 'provider_revoked', 'provider_timeout', 'provider_unavailable', 'provider_response_invalid', 'schema_changed', 'invalid_credential', 'credential_missing', 'collection_incomplete', 'destination_blocked'] as const;

/** Message key (in the `sources` namespace) of a plain-language collection-run error. */
export type MessageKey = `collectError_${(typeof COLLECT_ERROR_CODES)[number]}`;

/**
 * Maps a collection run's `error_code` to its `collectError_<code>` message (sentence plus suggested action).
 * Pure; unknown, empty or missing codes return null so the caller keeps its raw-code fallback text.
 */
export function collectErrorKey(code: string | null | undefined): MessageKey | null {
  return (COLLECT_ERROR_CODES as readonly string[]).includes(code ?? '') ? `collectError_${code as (typeof COLLECT_ERROR_CODES)[number]}` : null;
}

/**
 * Terms, declared-use, and data-tracking mode step for enabling one built-in: attribution, terms link,
 * operator-review note, deployment-use select, and realtime vs near-realtime tracking mode select.
 * Controlled by the caller; shared by the wizard and the gadget editor dialog.
 * An `error` (already localized) renders as a role=alert line below the fields.
 */
export function BuiltinTermsFields({ builtin, use, onUseChange, realtimeMode, onRealtimeModeChange, error }: {
  builtin: BuiltinSource;
  use: DeclaredUse;
  onUseChange: (use: DeclaredUse) => void;
  realtimeMode?: RealtimeMode;
  onRealtimeModeChange?: (mode: RealtimeMode) => void;
  aiOptions?: SourceAiOptions;
  onAiOptionsChange?: (options: SourceAiOptions) => void;
  error?: string | null;
}) {
  const t = useTranslations('sources');
  const defaultMode: RealtimeMode = builtin.realtime_tier ?? 'near_realtime';
  const [localRealtimeMode, setLocalRealtimeMode] = useState<RealtimeMode>(defaultMode);
  const effectiveRealtimeMode = realtimeMode ?? localRealtimeMode;
  /** Records the chosen realtime tier locally (uncontrolled use) and reports it to the parent when controlled. */
  const handleRealtimeModeChange = (next: RealtimeMode) => {
    setLocalRealtimeMode(next);
    onRealtimeModeChange?.(next);
  };
  return <section className="grid gap-3 text-sm" aria-label={t('builtinDialogTitle', { name: builtin.label })}>
    <h3 className="m-0 text-base font-semibold">{t('builtinDialogTitle', { name: builtin.label })}</h3>
    <p className="text-muted-foreground">{t('builtinTermsHint')}</p>
    <p><strong>{t('guideAttribution')}:</strong> {builtin.attribution}</p>
    <p><a className="text-foreground underline underline-offset-2" href={safeHttpUrl(builtin.terms_url)} target="_blank" rel="noopener noreferrer">{t('providerTerms')}</a> · {t('termsVersion')}: {builtin.terms_checked_on}</p>
    {builtin.eligibility === 'review' && <p role="note">{t('builtinOperatorNote')}</p>}
    <div className="field"><Label htmlFor="builtin-declared-use">{t('termsDeclaredUse')}</Label>
      <Select value={use} onValueChange={(value) => onUseChange(value as DeclaredUse)}>
        <SelectTrigger id="builtin-declared-use"><SelectValue /></SelectTrigger>
        <SelectContent>{USES.map(([value, label]) => <SelectItem key={value} value={value}>{t(label)}</SelectItem>)}</SelectContent>
      </Select></div>
    <div className="field"><Label htmlFor="builtin-realtime-mode">{t('trackingModeLabel')}</Label>
      <Select value={effectiveRealtimeMode} onValueChange={(value) => handleRealtimeModeChange(value as RealtimeMode)}>
        <SelectTrigger id="builtin-realtime-mode"><SelectValue /></SelectTrigger>
        <SelectContent>
          <SelectItem value="realtime">{t('trackingModeRealtime')}</SelectItem>
          <SelectItem value="near_realtime">{t('trackingModeNearRealtime')}</SelectItem>
        </SelectContent>
      </Select>
      <small className="text-muted-foreground">{t('trackingModeHelp')}</small></div>
    {error && <p className="text-destructive" role="alert">{error}</p>}
  </section>;
}

/**
 * Builds the enable-body fields for the options this built-in accepts (`applicable_options`); anything the owner left
 * empty is omitted so the server default applies. Returns nothing for servers that predate the option list.
 */
function applicableOptionPayload(builtin: BuiltinSource, values: BuiltinOptionValues): Record<string, unknown> {
  const allowed = new Set(builtin.applicable_options ?? []);
  const payload: Record<string, unknown> = {};
  const name = (values.name ?? builtin.label).trim();
  if (allowed.has('name') && name) payload.name = name;
  for (const key of ['schedule_interval_minutes', 'max_items', 'max_age_days', 'feed_sections'] as const) {
    if (allowed.has(key) && values[key] != null) payload[key] = values[key];
  }
  for (const key of ['include_keywords', 'exclude_keywords'] as const) {
    if (allowed.has(key) && values[key]?.length) payload[key] = values[key];
  }
  // Per-type options: undefined, empty text and empty multi-selects are omitted so the server default applies.
  for (const spec of OPTION_SPECS) {
    const value = (values as Record<string, unknown>)[spec.key];
    if (allowed.has(spec.key) && !isEmptyOptionValue(value)) payload[spec.key] = value;
  }
  return payload;
}

/** True when a per-type option value is "empty" (unset, blank text or an empty multi-select) and must be omitted from the body. */
function isEmptyOptionValue(value: unknown): boolean {
  return value == null || value === '' || (Array.isArray(value) && value.length === 0);
}

/** Shows the "all added / none compatible" status line when a shape filter leaves nothing pickable. */
function ShapeFilterEmpty({ rows, shapeFilter, onClearFilter, onAddConnector }: { rows: BuiltinSource[]; shapeFilter: DataShape[] | undefined; onClearFilter?: () => void; onAddConnector?: () => void }) {
  const t = useTranslations('sources');
  if (!(Boolean(shapeFilter?.length) && !rows.some((row) => matchesShapes(row.data_shapes, shapeFilter) && row.source_id === null))) return null;
  // Compatible built-ins that are all already added/paused read as "all added", not "none compatible".
  const anyCompatible = rows.some((row) => matchesShapes(row.data_shapes, shapeFilter));
  return <p role="status" className="text-muted-foreground">{anyCompatible ? t('addBuiltinAllAdded') : t('shapeFilterNoBuiltin')} {onAddConnector && !anyCompatible && <Button variant="outline" size="sm" onClick={onAddConnector}>{t('addConnector')}</Button>} {onClearFilter && <Button variant="outline" size="sm" onClick={onClearFilter}>{t('shapeFilterClear')}</Button>}</p>;
}

/** One selectable built-in row: realtime tier, Added/experimental/review badges and attribution; disabled once added. */
function BuiltinPickRow({ row, selected, onPick }: { row: BuiltinSource; selected: boolean; onPick: (row: BuiltinSource) => void }) {
  const t = useTranslations('sources');
  const added = row.source_id !== null;
  const tier = row.realtime_tier ?? 'near_realtime';
  return <BareButton type="button" disabled={added} aria-pressed={selected} onClick={() => onPick(row)}
    className={`flex w-full items-start justify-between gap-3 rounded-md border bg-transparent p-3 text-left disabled:cursor-not-allowed disabled:opacity-60 ${selected ? 'border-primary' : 'border-border'}`}>
    <span className="min-w-0">
      <span className="m-0 flex flex-wrap items-center gap-2 font-medium">{row.label}
        <Badge variant={tier === 'realtime' ? 'default' : 'secondary'}>{t(tier === 'realtime' ? 'badgeRealtime' : 'badgeNearRealtime')}</Badge>
        {added && <Badge variant="secondary">{t('builtinAdded')}</Badge>}
        {row.execution === 'experimental' && <Badge variant="outline">{t('builtinExperimental')}</Badge>}
        {row.eligibility === 'review' && <Badge variant="secondary">{t('builtinReviewClass')}</Badge>}
      </span>
      <span className="block text-sm text-muted-foreground">{row.attribution}</span>
    </span>
  </BareButton>;
}

/** Step 1: loading/error/empty states plus the category-grouped list of pickable built-ins. */
function BuiltinPickStep({ builtins, shapeFilter, selectedId, onPick, onClearFilter, onAddConnector }: {
  builtins: { isPending: boolean; isError: boolean; isSuccess: boolean; data: BuiltinSource[] | undefined; refetch: () => unknown };
  shapeFilter?: DataShape[];
  selectedId: string | undefined;
  onPick: (row: BuiltinSource) => void;
  onClearFilter?: () => void;
  onAddConnector?: () => void;
}) {
  const t = useTranslations('sources');
  const rows = builtins.data ?? [];
  return <section className="flex flex-col gap-3" aria-label={t('stepBuiltinPick')}>
    <p className="text-muted-foreground">{t('builtinPickHint')}</p>
    {builtins.isPending && <p className="text-muted-foreground">{t('loading')}</p>}
    {builtins.isError && <p className="text-destructive" role="alert">{t('builtinLoadFailed')} <Button variant="outline" onClick={() => builtins.refetch()}>{t('retry')}</Button></p>}
    {builtins.isSuccess && <ShapeFilterEmpty rows={rows} shapeFilter={shapeFilter} onClearFilter={onClearFilter} onAddConnector={onAddConnector} />}
    {groupByCategory(rows.filter((row) => matchesShapes(row.data_shapes, shapeFilter))).map(([category, group]) => <div key={category} className="flex flex-col gap-2" role="group" aria-label={t(CATEGORY_KEY[category])}>
      <h3 className="m-0 text-sm font-semibold text-muted-foreground">{t(CATEGORY_KEY[category])}</h3>
      {group.map((row) => <BuiltinPickRow key={row.id} row={row} selected={selectedId === row.id} onPick={onPick} />)}
    </div>)}
  </section>;
}

/** Step 3: live status of the verification fetch (activation, collection, outcome) with a retry once it failed. */
function BuiltinVerifyStep({ test, createdId, onRetry }: { test: TestState; createdId: string; onRetry: () => void }) {
  const t = useTranslations('sources');
  return <section className="flex flex-col gap-3" aria-live="polite" aria-label={t('verifyTitle')}>
    <h3 className="m-0 text-base font-semibold">{t('verifyTitle')}</h3>
    {test.phase === 'activating' && <p role="status">{t('verifyWaitingActivation')}</p>}
    {test.phase === 'collecting' && <p role="status">{t('verifyCollecting')}</p>}
    {test.phase === 'succeeded' && <p role="status">{t('verifySucceeded')}</p>}
    {test.phase === 'no_changes' && <p role="status">{t('verifyNoChanges')}</p>}
    {test.phase === 'failed' && <p className="error" role="alert">{t('verifyFailed', { code: test.code ?? 'unknown' })}</p>}
    {test.runId && <p className="text-muted-foreground">{t('verifyRunId', { id: test.runId })}</p>}
    {test.phase === 'failed' && createdId && <div><Button variant="outline" onClick={onRetry}>{t('verifyRetry')}</Button></div>}
  </section>;
}

/**
 * Wizard selection/enable state: picked built-in, declared use, realtime override, option values, AI options, error and
 * created source id. `pick` resets the per-built-in fields exactly as choosing a row did inline.
 */
function useBuiltinWizardState() {
  const [selected, setSelected] = useState<BuiltinSource | null>(null);
  const [use, setUse] = useState<DeclaredUse>('personal');
  const [realtimeModeOverride, setRealtimeModeOverride] = useState<RealtimeMode | null>(null);
  const [optionValues, setOptionValues] = useState<BuiltinOptionValues>({});
  const [aiOptions, setAiOptions] = useState<SourceAiOptions>({ graph_enabled: false, rag_index_enabled: false });
  const [error, setError] = useState<string | null>(null);
  const [createdId, setCreatedId] = useState('');
  /** Selects a built-in row and resets use, realtime override, options (default section) and error. */
  const pick = (row: BuiltinSource) => {
    setSelected(row); setUse('personal'); setRealtimeModeOverride(null);
    setOptionValues({ feed_sections: row.default_section ? [row.default_section] : undefined });
    setError(null);
  };
  return { selected, use, setUse, realtimeModeOverride, setRealtimeModeOverride, optionValues, setOptionValues, aiOptions, setAiOptions, error, setError, createdId, setCreatedId, pick };
}

/**
 * Three-step dialog for adding one built-in source: pick, confirm terms and declared use, then enable and prove a fetch.
 * A built-in that already has a source (`source_id` set, active or paused) is shown disabled with an "Added" badge;
 * resume it from the table instead. Enabling records the terms acknowledgement server-side. If the test fetch has not
 * succeeded when the owner discards the dialog, the created source is paused and archived so no half-added source remains.
 * When a shape filter leaves no built-in at all (for example Weather), the step points to "Connect a source" and
 * `onAddConnector` (when given) opens that wizard.
 */
export function BuiltinWizard({ onClose, onChanged, shapeFilter, onClearFilter, onAddConnector }: { onClose: () => void; onChanged: () => void; shapeFilter?: DataShape[]; onClearFilter?: () => void; onAddConnector?: () => void }) {
  const t = useTranslations('sources');
  const { csrfToken } = useWorkspaceSession();
  const queryClient = useQueryClient();
  const builtins = useQuery({ queryKey: connectorKeys.builtins, queryFn: ({ signal }) => listBuiltinSources(signal) });
  const [index, setIndex] = useState(0);
  const state = useBuiltinWizardState();
  const { selected, use, realtimeModeOverride, optionValues, aiOptions, error, createdId } = state;

  /** Refreshes the built-in availability and the source table after the server state changed. */
  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: connectorKeys.builtins });
    void queryClient.invalidateQueries({ queryKey: sourceKeys.all });
    onChanged();
  };
  const { test, runTest, abort, verified, testing } = useBuiltinTest(csrfToken, refresh);

  /** Enables the picked built-in when leaving the terms step, then starts the test fetch for the created source. */
  async function onNext(): Promise<boolean> {
    if (index !== 1 || !selected) return true;
    if (createdId) { void runTest(createdId); return true; }
    state.setError(null);
    try {
      const options = { ...aiOptions, ...(realtimeModeOverride ? { realtime_mode: realtimeModeOverride } : {}), ...applicableOptionPayload(selected, optionValues) };
      const result = await enableBuiltinSource(selected.id, use, csrfToken, undefined, options);
      if (!result.source_id) { state.setError(t('builtinErrorGeneric')); return false; }
      state.setCreatedId(result.source_id);
      refresh();
      void runTest(result.source_id);
      return true;
    } catch (cause) {
      /** Maps a failed enable to the most specific localized message. */
      state.setError(t(builtinEnableErrorKey(cause)));
      return false;
    }
  }

  /** Pauses and archives the unverified source this dialog created, then closes. */
  async function discard() {
    abort();
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
    {index === 0 && <BuiltinPickStep builtins={builtins} shapeFilter={shapeFilter} selectedId={selected?.id} onPick={state.pick} onClearFilter={onClearFilter} onAddConnector={onAddConnector} />}
    {index === 1 && selected && <BuiltinTermsFields builtin={selected} use={use} onUseChange={state.setUse} realtimeMode={realtimeModeOverride ?? selected.realtime_tier ?? 'near_realtime'} onRealtimeModeChange={state.setRealtimeModeOverride} aiOptions={aiOptions} onAiOptionsChange={state.setAiOptions} error={error} />}
    {index === 1 && selected && <BuiltinOptions builtin={selected} values={optionValues} showName showSchedule onChange={(key, value) => state.setOptionValues((current) => ({ ...current, [key]: value }))} />}
    {index === 2 && <BuiltinVerifyStep test={test} createdId={createdId} onRetry={() => void runTest(createdId)} />}
  </WizardShell>;
}
