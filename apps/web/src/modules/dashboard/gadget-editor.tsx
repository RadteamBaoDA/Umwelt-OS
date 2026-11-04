'use client';

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Check } from 'lucide-react';
import { useTranslations } from 'next-intl';
import { useCallback, useEffect, useId, useRef, useState, type ReactNode, type RefObject } from 'react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { ApiError } from '@/core/api';
import { apiFailureKey } from '@/core/api-failure-key';
import { useWorkspaceSession } from '@/core/app-shell/workspace-shell';
import { templateName } from '@/core/renderer-labels';
import { getConnectorConfiguration } from '@/modules/sources';
import {
  createGadgetDefinition, dashboardKeys, getMetricCatalog, listCompatibleSourceCounts, listGadgetRenderers, listGadgetTemplates, patchGadgetDefinition,
  type GadgetDefinition, type GadgetRenderer,
} from './api';
import {
  buildDefinitionPayload, draftPatchForTemplate, fieldsFor, initialDraft, mergeCatalogEntries, namesLookbackField, planSourceChange, splitList,
  type GadgetDraft,
} from './gadget-editor-model';
import { DatasourcePicker, useSourceRows } from './gadget-editor-pickers';
import {
  AdvancedSection, MetricsField, RegionField, SymbolFields, TemplateChooser, type SectionFacts,
} from './gadget-editor-sections';
import { sourcesLinkFor } from './sources-link';

export { defaultMetric, fieldsFor, mergeCatalogEntries } from './gadget-editor-model';
export { DatasourcePicker, useSourceRows } from './gadget-editor-pickers';

/** Props of the shared gadget editor. */
export interface GadgetEditorProps {
  /** Saved definition to edit; omit to create a new one from a template or the Custom tile. */
  definition?: GadgetDefinition | null;
  /** Called with the saved definition. May throw/reject; the editor shows a generic save error and stays open. */
  onSaved: (definition: GadgetDefinition) => void | Promise<void>;
  /** Cancel handler; the Cancel button is hidden when omitted. */
  onCancel?: () => void;
  /** Extra footer content (for example a Delete button) rendered before the actions. */
  footerExtra?: ReactNode;
  /** Overrides the save button label. */
  saveLabel?: string;
  /** Extra fields rendered right after the gadget Name field (for example the per-dashboard title). */
  afterName?: ReactNode;
}

/**
 * Holds the whole editable draft as one object with a keyed setter, replacing one state per field.
 *
 * @param definition Saved definition used for the initial values, or null for a new gadget.
 * @returns The draft, a keyed `set` (value or functional update) and a multi-field `patch`.
 */
function useGadgetDraft(definition: GadgetDefinition | null) {
  const [draft, setDraft] = useState<GadgetDraft>(() => initialDraft(definition));
  const set = useCallback(<K extends keyof GadgetDraft>(key: K, value: GadgetDraft[K] | ((current: GadgetDraft[K]) => GadgetDraft[K])) => {
    setDraft((current) => ({
      ...current,
      [key]: typeof value === 'function' ? (value as (c: GadgetDraft[K]) => GadgetDraft[K])(current[key]) : value,
    }));
  }, []);
  const patch = useCallback((next: Partial<GadgetDraft>) => setDraft((current) => ({ ...current, ...next })), []);
  return { draft, set, patch };
}

/** Loads the renderer, template, metric-catalog and compatible-count queries (static data is cached forever). */
function useEditorQueries(definition: GadgetDefinition | null) {
  const renderers = useQuery({ queryKey: dashboardKeys.renderers, queryFn: ({ signal }) => listGadgetRenderers(signal), staleTime: Infinity });
  const templates = useQuery({ queryKey: dashboardKeys.templates, queryFn: ({ signal }) => listGadgetTemplates(signal), enabled: !definition, staleTime: Infinity });
  const catalogQuery = useQuery({ queryKey: dashboardKeys.metricCatalog, queryFn: ({ signal }) => getMetricCatalog(signal), staleTime: Infinity });
  const counts = useQuery({ queryKey: dashboardKeys.compatibleCounts, queryFn: ({ signal }) => listCompatibleSourceCounts(signal), enabled: !definition });
  return { renderers, templates, catalogQuery, counts };
}

/** True for an integer item limit within the 1..100 range the API accepts. */
function isLimitValid(limit: string): boolean {
  const limitNumber = Number(limit);
  return Number.isInteger(limitNumber) && limitNumber >= 1 && limitNumber <= 100;
}

/**
 * Lookback validity. Empty means unset. Map/intelligence windows are capped to 30 on save, so only the other
 * renderers can be out of range.
 */
function isLookbackValid(lookback: string, renderer: string): boolean {
  if (lookback.trim() === '' || renderer === 'map' || renderer === 'intelligence_panel') return true;
  const lookbackNumber = Number(lookback);
  return Number.isInteger(lookbackNumber) && lookbackNumber >= 1 && lookbackNumber <= 3660;
}

/** Per-field validity flags of the draft. */
interface ValidityFlags {
  nameValid: boolean;
  sourceValid: boolean;
  limitValid: boolean;
  lookbackValid: boolean;
  keywordsValid: boolean;
  catalogLoading: boolean;
}

/** Why Save is disabled, in priority order; shown beside Save so a problem inside collapsed Advanced is never hidden. */
function pickBlockReason(flags: ValidityFlags, t: ReturnType<typeof useTranslations>): string | null {
  if (!flags.nameValid) return t('nameRequired');
  if (!flags.sourceValid) return t('datasourceRequired');
  if (!flags.limitValid) return t('limitHelp');
  if (!flags.lookbackValid) return t('lookbackHelp');
  if (!flags.keywordsValid) return t('keywordsHelp');
  return flags.catalogLoading ? t('metricCatalogLoading') : null;
}

/**
 * Existing gadgets with no source keep saving; new ones must pick a datasource when the renderer reads sources
 * (live maps read none).
 */
function needsDatasource(draft: GadgetDraft, sourceMode: string, definition: GadgetDefinition | null): boolean {
  if (draft.renderer === 'map' && draft.mapMode === 'live') return false;
  return sourceMode !== 'none' && (!definition || definition.source_ids.length > 0);
}

/** Field validity of the draft and the reason Save is disabled (shown beside Save). */
function useDraftValidity(draft: GadgetDraft, descriptor: GadgetRenderer | undefined, definition: GadgetDefinition | null, catalogLoading: boolean) {
  const t = useTranslations('gadgetSettings');
  const needsSource = needsDatasource(draft, descriptor?.source_mode ?? 'none', definition);
  const flags: ValidityFlags = {
    nameValid: draft.name.trim().length > 0,
    sourceValid: !needsSource || draft.sourceIds.length > 0,
    limitValid: isLimitValid(draft.limit),
    lookbackValid: isLookbackValid(draft.lookback, draft.renderer),
    keywordsValid: splitList(draft.keywords).length <= 32 && splitList(draft.exclude).length <= 32,
    catalogLoading,
  };
  const blockReason = pickBlockReason(flags, t);
  const valid = draft.renderer !== '' && descriptor?.runtime_state === 'available' && blockReason === null;
  return { ...flags, blockReason, valid };
}

/** CoinGecko assets configured on the first selected source (only when that is the single bound provider). */
function useCoinAssets(sourceIds: string[], provider: string | null) {
  const coinSource = sourceIds[0] ?? '';
  const coinConfiguration = useQuery({ queryKey: ['coingecko-gadget-source', coinSource], queryFn: ({ signal }) => getConnectorConfiguration(coinSource, signal), enabled: provider === 'coingecko' && Boolean(coinSource) });
  return provider === 'coingecko' && coinConfiguration.data ? coinConfiguration.data.configuration.coingecko_assets ?? [{ id: 'bitcoin', symbol: 'BTC', name: 'Bitcoin', series_symbol: 'BTC' }] : null;
}

/**
 * Provider-derived facts: the merged catalog entry of the bound datasources, CoinGecko assets and which symbol,
 * region and metric controls to show.
 */
function useProviderFacts(draft: GadgetDraft, queries: ReturnType<typeof useEditorQueries>, fields: ReadonlySet<string>) {
  const { rows: sourceRows } = useSourceRows();
  const { sourceIds, renderer, symbols } = draft;
  const providerOfRow = (id: string | undefined) => sourceRows.find((row) => row.id === id)?.provider ?? null;
  // Distinct providers of every selected datasource (a multi-source chart/metric/weather may mix kinds); the pickers use their merged catalog.
  const providers = [...new Set(sourceIds.map(providerOfRow).filter((item): item is string => item !== null))];
  const coinAssets = useCoinAssets(sourceIds, providers.length === 1 ? providers[0] : null);
  const catalog = queries.catalogQuery.data;
  const entry = mergeCatalogEntries(providers.map((item) => catalog?.[item]));
  // Several bound providers make the chart metric a checklist (one metric per provider, union scope).
  const chartMulti = renderer === 'finance_chart' && providers.length > 1;
  const showSymbols = fields.has('symbols') && entry?.symbol_keyed === true;
  // Provider unknown (catalog failed, source absent from the loaded page, provider not in the catalog): symbols stay visible
  // and editable as free text so a saved definition is never opaque. Hidden while the catalog is still loading or nothing is bound.
  const showSymbolsFallback = fields.has('symbols') && entry === undefined && !queries.catalogQuery.isPending && (sourceIds.length > 0 || splitList(symbols, true).length > 0);
  // Region-keyed providers (World Bank countries, OpenAQ locations) plot one region per chart.
  const showRegion = renderer === 'finance_chart' && entry?.region_keyed === true;
  // Provider catalog drives the metric control; non-chart renderers with a catalog render a checkbox group.
  const metricGroup = entry !== undefined && entry.metrics.length > 0 && (renderer !== 'finance_chart' || chartMulti);
  return { sourceRows, entry, chartMulti, showSymbols, showSymbolsFallback, showRegion, metricGroup, coinAssets, catalog };
}

/**
 * Save mutation: creates a new definition or patches the saved one (revision guarded), invalidates the definition
 * list and reports the saved definition. Source ids are sent only when changed so a saved, now-incompatible source
 * never blocks unrelated edits; highlight rules only when touched.
 */
function useSaveDefinition(
  draft: GadgetDraft,
  patch: (next: Partial<GadgetDraft>) => void,
  onSaved: GadgetEditorProps['onSaved'],
  payloadFacts: Omit<Parameters<typeof buildDefinitionPayload>[0], 'draft'>,
) {
  const session = useWorkspaceSession();
  const client = useQueryClient();
  return useMutation({
    mutationFn: async () => {
      const { filters, scope } = buildDefinitionPayload({ draft, ...payloadFacts });
      const { base, name, renderer, sourceIds, rules, rulesTouched } = draft;
      if (!base) {
        return createGadgetDefinition({ name: name.trim(), renderer, source_ids: sourceIds, scope, filters, highlight_rules: rules }, session.csrfToken);
      }
      // Source ids are sent only when changed so a saved, now-incompatible source never blocks unrelated edits.
      const sourcesChanged = JSON.stringify(sourceIds) !== JSON.stringify(base.source_ids);
      return patchGadgetDefinition(base.id, {
        expected_revision: base.revision, name: name.trim(), filters, scope,
        ...(sourcesChanged ? { source_ids: sourceIds } : {}),
        ...(rulesTouched ? { highlight_rules: rules } : {}),
      }, session.csrfToken);
    },
    onSuccess: async (saved) => {
      void client.invalidateQueries({ queryKey: dashboardKeys.definitions });
      if (draft.base) patch({ base: saved });
      await onSaved(saved);
    },
  });
}

/**
 * Maps a failed save to a field-scoped message. A 422 with a known code and `details.field` is shown under that
 * field; FastAPI's code-less pydantic 422 lists the offending `loc`, and a lookback range violation maps to its own
 * field. Anything else stays the generic banner.
 */
function useSaveFailure(error: unknown) {
  const t = useTranslations('gadgetSettings');
  const failure = error instanceof ApiError && error.status === 422 && t.has(`fieldErrors.${error.code}`)
    ? { field: String((error.details as { field?: unknown } | null | undefined)?.field ?? ''), message: t(`fieldErrors.${error.code}` as 'name') }
    : error instanceof ApiError && error.status === 422 && namesLookbackField(error.details)
      ? { field: 'lookback_days', message: t('lookbackHelp') } : null;
  /** Localized server error for one field name, or null when the failure belongs elsewhere. */
  const fieldError = (field: string) => (failure?.field === field ? failure.message : null);
  return { failure, fieldError };
}

/** Name field with touched-state validation. */
function NameField({ nameId, draft, set, nameValid, afterName }: {
  nameId: string; draft: GadgetDraft; set: ReturnType<typeof useGadgetDraft>['set']; nameValid: boolean; afterName?: ReactNode;
}) {
  const t = useTranslations('gadgetSettings');
  const { touched } = draft;
  return <>
    <div className="space-y-1.5">
      <Label htmlFor={nameId} className="text-xs font-semibold">{t('name')}</Label>
      <Input id={nameId} value={draft.name} maxLength={120} className="h-9" aria-invalid={touched && !nameValid} aria-describedby={touched && !nameValid ? `${nameId}-error` : undefined} onBlur={() => set('touched', true)} onChange={(event) => { set('touched', true); set('name', event.target.value); }} />
      {touched && !nameValid && <p id={`${nameId}-error`} role="alert" className="text-xs text-destructive">{t('nameRequired')}</p>}
    </div>
    {afterName}
  </>;
}

/** Everything the form body needs, assembled once by {@link useGadgetEditor}. */
type EditorModel = ReturnType<typeof useGadgetEditor>;

/**
 * Orchestrates the editor: queries, draft, derived validity/provider facts and the save mutation.
 *
 * Side effects: opens the Advanced section when one of its fields is invalid or the server rejects a field inside it.
 *
 * @param props Editor props (definition and save callback).
 */
function useGadgetEditor({ definition = null, onSaved }: Pick<GadgetEditorProps, 'definition' | 'onSaved'>) {
  const t = useTranslations('gadgetSettings');
  const nameId = useId();
  const limitId = useId();
  const queries = useEditorQueries(definition);
  const { draft, set, patch } = useGadgetDraft(definition);
  const { renderer } = draft;
  const descriptor = (queries.renderers.data ?? []).find((item) => item.id === renderer);
  const template = queries.templates.data?.find((item) => item.renderer === renderer);
  const fields = fieldsFor(renderer);
  const facts = useProviderFacts(draft, queries, fields);
  // Measurement renderers wait for the catalog so the default metric comes from the provider, not a guess.
  const catalogLoading = fields.has('metrics') && queries.catalogQuery.isPending;
  const validity = useDraftValidity(draft, descriptor, definition, catalogLoading);
  const { limitValid, lookbackValid } = validity;
  const save = useSaveDefinition(draft, patch, onSaved, { entry: facts.entry, chartMulti: facts.chartMulti, template });
  const { failure, fieldError } = useSaveFailure(save.error);

  /** Starts a new definition from one available renderer and clears configuration from any prior selection. */
  const choose = (nextRenderer: string) => {
    // `template` still reflects the previous renderer here, so resolve the picked one explicitly.
    const picked = queries.templates.data?.find((item) => item.renderer === nextRenderer);
    patch(draftPatchForTemplate(picked, nextRenderer, templateName(nextRenderer, t)));
  };
  /** Applies a datasource selection (see {@link planSourceChange}). */
  const changeSources = (next: string[]) => patch(planSourceChange({
    next, draft, fields, catalog: facts.catalog,
    providerOf: (id) => facts.sourceRows.find((row) => row.id === id)?.provider ?? null,
  }));

  // A field error for a field this editor does not currently render would otherwise be invisible, so show it near Save.
  /** True when the editor currently renders a control for the server-reported field name. */
  const fieldRendered = (field: string) => field === 'symbols' ? facts.showSymbols || facts.showSymbolsFallback : field === 'regions' ? facts.showRegion : field === 'metrics' ? fields.has('metrics') : field === 'map_layer_ids' ? fields.has('map') : field === 'lookback_days' ? fields.has('lookback') : field === 'source_ids';
  const unrenderedFailure = failure && !fieldRendered(failure.field) ? failure.message : null;
  const error = save.error && !failure ? t((apiFailureKey(save.error) === 'conflict' ? 'conflict' : 'saveFailed')) : unrenderedFailure;
  const sectionFacts: SectionFacts = {
    fields, entry: facts.entry, chartMulti: facts.chartMulti, metricGroup: facts.metricGroup, showSymbols: facts.showSymbols,
    showSymbolsFallback: facts.showSymbolsFallback, showRegion: facts.showRegion, coinAssets: facts.coinAssets,
    limitValid, lookbackValid, fieldError, nameId, limitId,
  };
  return { queries, draft, set, patch, descriptor, validity, save, error, fieldError, sectionFacts, failureField: failure?.field, choose, changeSources, nameId, fields, sourceMode: descriptor?.source_mode ?? 'none' };
}

/** Datasource picker with its validation messages (hidden for renderers that read no sources and for live maps). */
function SourceField({ model }: { model: EditorModel }) {
  const t = useTranslations('gadgetSettings');
  const { draft, descriptor, validity, nameId, sourceMode } = model;
  const sourceErrorId = `${nameId}-source-error`;
  const { sourceValid } = validity;
  if (sourceMode === 'none' || (draft.renderer === 'map' && draft.mapMode === 'live')) return null;
  return <div>
    <DatasourcePicker acceptedShapes={descriptor?.accepted_shapes ?? null} mode={sourceMode === 'single' ? 'single' : 'multi'} value={draft.sourceIds} onChange={model.changeSources}
      invalid={!sourceValid} describedBy={!sourceValid ? sourceErrorId : undefined} sourcesHref={descriptor ? sourcesLinkFor(descriptor) : undefined} max={descriptor?.max_sources ?? undefined} />
    {model.fieldError('source_ids') && <p role="alert" className="mt-1 text-xs text-destructive">{model.fieldError('source_ids')}</p>}
    {!sourceValid && <p id={sourceErrorId} className="mt-1 text-xs text-muted-foreground">{t('datasourceRequired')}</p>}
  </div>;
}

/** Live / selected-sources mode select (map gadgets only). */
function MapModeField({ nameId, draft, set }: { nameId: string; draft: GadgetDraft; set: EditorModel['set'] }) {
  const td = useTranslations('dashboard');
  if (draft.renderer !== 'map') return null;
  return <div className="space-y-1.5"><Label htmlFor={`${nameId}-mode`}>{td('liveMapMode')}</Label><Select value={draft.mapMode} onValueChange={(value) => set('mapMode', value as 'live' | 'selected_sources')}><SelectTrigger id={`${nameId}-mode`}><SelectValue /></SelectTrigger><SelectContent><SelectItem value="live">{td('liveMapModeLive')}</SelectItem><SelectItem value="selected_sources">{td('liveMapModeSources')}</SelectItem></SelectContent></Select></div>;
}

/** Footer: server error, Save-disabled reason and the action buttons. */
function EditorFooter({ model, props }: { model: EditorModel; props: GadgetEditorProps }) {
  const t = useTranslations('gadgetSettings');
  const { save, error, nameId, validity } = model;
  const { blockReason, valid } = validity;
  return <>
    {error && <p role="alert" className="text-xs text-destructive">{error}</p>}
    {blockReason && <p id={`${nameId}-block`} role="status" className="text-xs text-muted-foreground">{blockReason}</p>}
    <div className="flex flex-wrap items-center justify-end gap-2">
      {props.footerExtra && <div className="me-auto">{props.footerExtra}</div>}
      {props.onCancel && <Button type="button" variant="outline" disabled={save.isPending} onClick={props.onCancel}>{t('cancel')}</Button>}
      <Button type="submit" disabled={save.isPending || !valid} aria-describedby={blockReason ? `${nameId}-block` : undefined}>{save.isPending ? t('saving') : (props.saveLabel ?? t('save'))}</Button>
    </div>
  </>;
}

/**
 * Opens the Advanced section when one of its fields is invalid (Save is disabled then, so no submit could do it) or
 * when the server rejects a field that lives inside it (`map_layer_ids` and `lookback_days`).
 */
function useAutoOpenAdvanced(detailsRef: RefObject<HTMLDetailsElement | null>, validity: { limitValid: boolean; keywordsValid: boolean; lookbackValid: boolean }, failureField: string | undefined) {
  const { limitValid, keywordsValid, lookbackValid } = validity;
  useEffect(() => { if ((!limitValid || !keywordsValid || !lookbackValid) && detailsRef.current) detailsRef.current.open = true; }, [detailsRef, limitValid, keywordsValid, lookbackValid]);
  useEffect(() => { if ((failureField === 'map_layer_ids' || failureField === 'lookback_days') && detailsRef.current) detailsRef.current.open = true; }, [detailsRef, failureField]);
}

/** The editor form for a chosen renderer. */
function EditorForm({ model, props }: { model: EditorModel; props: GadgetEditorProps }) {
  const t = useTranslations('gadgetSettings');
  const { draft, set, save, validity, nameId, sectionFacts } = model;
  const { definition = null } = props;
  const detailsRef = useRef<HTMLDetailsElement>(null);
  useAutoOpenAdvanced(detailsRef, validity, model.failureField);
  const hasAdvanced = (['keywords', 'limit', 'rules', 'lookback', 'map', 'cii', 'channels'] as const).some((key) => model.fields.has(key));
  return <form className="space-y-4" noValidate onSubmit={(event) => { event.preventDefault(); set('touched', true); if (validity.valid) save.mutate(); }}>
    <fieldset disabled={save.isPending} className="m-0 min-w-0 space-y-4 border-0 p-0">
      {!definition && <div className="flex items-center gap-2 text-xs text-muted-foreground">
        <Check className="size-3.5" aria-hidden="true" />
        <span>{templateName(draft.renderer, t)}</span>
        <Button type="button" variant="ghost" size="sm" onClick={() => { set('choice', null); set('renderer', ''); }}>{t('backToTemplates')}</Button>
      </div>}
      <MapModeField nameId={nameId} draft={draft} set={set} />
      <SourceField model={model} />
      {model.fields.has('metrics') && <MetricsField draft={draft} set={set} facts={sectionFacts} />}
      <SymbolFields draft={draft} set={set} facts={sectionFacts} />
      <RegionField draft={draft} set={set} facts={sectionFacts} />
      <NameField nameId={nameId} draft={draft} set={set} nameValid={validity.nameValid} afterName={props.afterName} />
      {hasAdvanced && <AdvancedSection ref={detailsRef} draft={draft} set={set} facts={sectionFacts} />}
    </fieldset>
    <EditorFooter model={model} props={props} />
  </form>;
}

/**
 * One editor for creating a generic renderer template on the dashboard and editing a saved definition. New gadgets
 * select a display type, then an already-configured compatible datasource; datasource creation remains in Settings.
 * Saving uses the existing revision-guarded definition contract and never changes layout placement.
 */
export function GadgetEditor(props: GadgetEditorProps) {
  const model = useGadgetEditor(props);
  const { queries, draft } = model;
  const showTemplates = !props.definition && draft.choice === null;
  if (showTemplates) {
    return <TemplateChooser renderers={queries.renderers} templates={queries.templates} counts={queries.counts} onChoose={model.choose} onCancel={props.onCancel} />;
  }
  return <EditorForm model={model} props={props} />;
}
