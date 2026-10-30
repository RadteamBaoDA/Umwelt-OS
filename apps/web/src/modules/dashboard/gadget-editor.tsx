'use client';

import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Bell, Check, Plus, Trash2 } from 'lucide-react';
import Link from 'next/link';
import { useTranslations } from 'next-intl';
import { useCallback, useEffect, useId, useMemo, useRef, useState, type ReactNode } from 'react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { RadioGroup, RadioGroupItem } from '@/components/ui/radio-group';
import { SearchableSelect } from '@/components/ui/searchable-select';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { ApiError } from '@/core/api';
import { apiFailureKey } from '@/core/api-failure-key';
import { useWorkspaceSession } from '@/core/app-shell/workspace-shell';
import { formatDateTime } from '@/core/i18n';
import type { DataShape } from '@/core/data-shapes';
import { useDisplayPreferences } from '@/core/query-provider';
import { uuid7 } from '@/core/uuid7';
import {
  createGadgetDefinition, dashboardKeys, getMetricCatalog, listCompatibleSourceCounts, listGadgetRenderers, listGadgetSources, listGadgetTemplates, patchGadgetDefinition,
  type GadgetDefinition, type GadgetRenderer, type HighlightRule, type ProviderMetrics,
} from './api';
import { TEMPLATE_KIND_BY_RENDERER, templateName } from '@/core/renderer-labels';
import { GadgetTemplateSample } from './gadget-template-sample';
import { CHART_FALLBACK_LOOKBACK_DAYS, METRIC_FALLBACK_LOOKBACK_DAYS } from './gadgets/use-effective-lookback';
import { sourcesLinkFor } from './sources-link';

const MAX_SOURCES = 32;
const MAP_LAYERS = ['world_observations', 'military', 'economic', 'disaster', 'escalation'] as const;
/** Resolves the short localized description shown beside a renderer's SVG preview. */
function templateDescription(renderer: string, t: ReturnType<typeof useTranslations>): string {
  const kind = TEMPLATE_KIND_BY_RENDERER[renderer];
  return kind && t.has(`templateTypes.${kind}.description`) ? t(`templateTypes.${kind}.description` as 'name') : '';
}

type FieldKey = 'keywords' | 'limit' | 'symbols' | 'metrics' | 'lookback' | 'map' | 'cii' | 'channels' | 'rules';
const LIST_RENDERERS = ['news_feed', 'text_panel', 'table_panel', 'video_panel', 'telegram_feed', 'highlights', 'watch_rules'];

/**
 * Editor fields that matter to one renderer. `symbols` is only a candidate here: the editor additionally requires the
 * selected datasource's provider to be symbol-keyed before showing it.
 *
 * @param renderer Renderer id.
 * @returns The set of field keys the renderer reads.
 */
export function fieldsFor(renderer: string): ReadonlySet<FieldKey> {
  const out = new Set<FieldKey>();
  if (LIST_RENDERERS.includes(renderer)) (['keywords', 'limit', 'rules'] as const).forEach((key) => out.add(key));
  if (['finance_chart', 'provider_observation', 'weather'].includes(renderer)) { out.add('metrics'); out.add('lookback'); }
  if (renderer === 'finance_chart' || renderer === 'provider_observation') out.add('symbols');
  if (renderer === 'map') out.add('map');
  if (renderer === 'intelligence_panel') out.add('cii');
  if (renderer === 'telegram_feed') out.add('channels');
  return out;
}

/**
 * Loads datasource rows page by page (shared TanStack cache entry) so the picker and the editor see the same rows.
 *
 * @param enabled False defers the request (for example while a dialog using the rows is closed).
 * @returns The infinite query and the flattened loaded rows.
 */
export function useSourceRows(enabled = true) {
  const query = useInfiniteQuery({
    queryKey: [...dashboardKeys.sources, 'editor-paged'],
    queryFn: ({ signal, pageParam }) => listGadgetSources(100, pageParam, signal),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    // The editor and the picker share this entry; a short stale window stops the second mount re-fetching page one.
    staleTime: 10_000,
    enabled,
  });
  const rows = useMemo(() => (query.data?.pages ?? []).flatMap((page) => page.items), [query.data]);
  return { query, rows };
}

/**
 * Default chart metric for a provider's catalog entry: `close` when listed, else the first metric
 * (the backend `default_metric` applies the same rule). Callers pass an entry; with none (or an entry listing no
 * metrics) the `close` fallback is only a display/typing hint and must never be persisted for an unknown provider.
 *
 * @param entry Catalog entry of the bound provider, if known.
 * @returns A metric id.
 */
export function defaultMetric(entry?: ProviderMetrics): string {
  const ids = entry?.metrics.map((item) => item.id) ?? [];
  return ids.includes('close') || ids.length === 0 ? 'close' : ids[0];
}

/**
 * Window (days) a new gadget of `renderer` starts with when the provider catalog has no default: the value the renderer
 * itself falls back to at runtime (Metric 30, chart 90) so an untouched editor and an unset stored gadget agree.
 */
function fallbackLookback(renderer: string): number {
  if (renderer === 'map' || renderer === 'intelligence_panel' || renderer === 'provider_observation') return METRIC_FALLBACK_LOOKBACK_DAYS;
  return CHART_FALLBACK_LOOKBACK_DAYS;
}

/** True when a code-less FastAPI/pydantic 422 `details` list names `lookback_days` in an item's `loc` path. */
function namesLookbackField(details: unknown): boolean {
  return Array.isArray(details) && details.some((item) => Array.isArray((item as { loc?: unknown })?.loc) && ((item as { loc: unknown[] }).loc).includes('lookback_days'));
}

/** Splits comma-separated text into trimmed, non-empty entries. */
function splitList(value: string, upper = false): string[] {
  return value.split(',').map((item) => (upper ? item.trim().toUpperCase() : item.trim())).filter(Boolean);
}

/**
 * Selects only active, workspace-configured sources accepted by this renderer. Previously saved inactive or
 * incompatible IDs remain visible so owners can remove them; adding a gadget never creates or activates a source.
 *
 * @param acceptedShapes Data shapes the renderer reads; null means any shape.
 * @param mode Selection cardinality from the renderer or template.
 * @param value Currently selected source ids.
 * @param onChange Receives the next selection.
 */
export function DatasourcePicker({ acceptedShapes, mode, value, onChange, invalid, describedBy, sourcesHref = '/settings/sources' }: {
  acceptedShapes: readonly DataShape[] | null;
  mode: 'single' | 'multi';
  value: string[];
  onChange: (sourceIds: string[]) => void;
  invalid?: boolean;
  describedBy?: string;
  /** Settings > Sources URL for the "Open Sources" link; callers pass the renderer-filtered one. */
  sourcesHref?: string;
}) {
  const t = useTranslations('gadgetSettings');
  const legendId = useId();
  const ts = useTranslations('sources');
  const display = useDisplayPreferences();
  // Follows next_cursor page by page so owners with more than 100 sources can still reach every compatible one.
  const { query: sources, rows: loaded } = useSourceRows();
  /** Provider label from the sources catalog (`provider<PascalId>`), else the raw provider id. */
  const providerLabel = (provider: string | null) => {
    if (!provider) return '';
    const key = `provider${provider.split('_').map((part) => part.charAt(0).toUpperCase() + part.slice(1)).join('')}`;
    return ts.has(key) ? ts(key as 'name') : provider;
  };
  /** Secondary line: provider label and last successful collection time (or "never updated"). */
  const meta = (option: { provider: string | null; last_success_at: string | null }) => [
    providerLabel(option.provider),
    option.last_success_at ? t('datasourceUpdated', { when: formatDateTime(option.last_success_at, display.locale, display.timezone) }) : t('datasourceNeverUpdated'),
  ].filter(Boolean).join(' · ');
  /** True when the source's data shapes intersect the renderer's accepted shapes (any shape when unconstrained). */
  const allowed = useCallback(
    (item: { data_shapes?: readonly DataShape[] }) => acceptedShapes === null || (item.data_shapes ?? []).some((shape) => acceptedShapes.includes(shape)),
    [acceptedShapes],
  );
  const options = useMemo(() => {
    const rows: ((typeof loaded)[number] & { placeholder?: boolean })[] = loaded.filter((item) => item.status === 'active' && allowed(item));
    const selected = new Set(rows.map((item) => item.id));
    for (const id of value) {
      if (!selected.has(id)) {
        const saved = loaded.find((item) => item.id === id);
        // `placeholder` marks an id no loaded page contains yet; it is neither inactive nor incompatible until proven missing.
        rows.push(saved ?? { id, name: id, type: 'unknown', status: 'archived', provider: null, generation: 0, local_only: false, last_success_at: null, data_shapes: [], placeholder: true });
      }
    }
    return rows;
  }, [allowed, loaded, value]);

  // Keep paging (bounded by the page count) while no row is loaded or a selected id is still missing, so the owner
  // never sees "none, add in Sources" or a false Inactive badge for a source that only sits on a later page.
  const { hasNextPage, isFetchingNextPage, isFetchNextPageError, fetchNextPage } = sources;
  const selectedMissing = value.some((id) => !loaded.some((item) => item.id === id));
  useEffect(() => {
    if ((options.length === 0 || selectedMissing) && hasNextPage && !isFetchingNextPage && !isFetchNextPageError) void fetchNextPage({ cancelRefetch: false });
  }, [options.length, selectedMissing, hasNextPage, isFetchingNextPage, isFetchNextPageError, fetchNextPage]);
  /** Status badge: a not-yet-loaded saved id shows a neutral loading hint until every page is read, then Inactive. */
  const statusBadge = (option: (typeof options)[number]) => {
    // Neutral until every page loaded successfully: pending, paging or failed reads cannot prove the id is missing.
    if (option.placeholder && (sources.isPending || hasNextPage || sources.isError || isFetchNextPageError)) return <span className="text-xs text-muted-foreground">{t('datasourceLoading')}</span>;
    return option.status !== 'active' ? <Badge variant="destructive">{t('datasourceInactive')}</Badge> : !allowed(option) && <Badge variant="destructive">{t('datasourceIncompatible')}</Badge>;
  };

  const body = mode === 'single'
    ? <RadioGroup value={value[0] ?? ''} aria-labelledby={legendId} aria-describedby={describedBy} aria-invalid={invalid} className="space-y-1"
      onValueChange={(next) => onChange([next])}>
      {options.map((option) => <label key={option.id} className="flex min-h-9 items-center gap-2 text-sm">
        <RadioGroupItem value={option.id} />
        <span className="flex flex-wrap items-center gap-2"><span>{option.name}<span className="block text-xs text-muted-foreground">{meta(option)}</span></span>{statusBadge(option)}</span>
      </label>)}
    </RadioGroup>
    : <div role="group" aria-labelledby={legendId} aria-describedby={describedBy} className="space-y-1">
      {options.map((option) => {
        const checked = value.includes(option.id);
        return <label key={option.id} className="flex min-h-9 items-center gap-2 text-sm">
          <Checkbox checked={checked} disabled={!checked && value.length >= MAX_SOURCES}
            onCheckedChange={(next) => onChange(next === true ? [...value, option.id] : value.filter((item) => item !== option.id))} />
          <span className="flex flex-wrap items-center gap-2"><span>{option.name}<span className="block text-xs text-muted-foreground">{meta(option)}</span></span>{statusBadge(option)}</span>
        </label>;
      })}
    </div>;

  return <div className="space-y-2">
    <div id={legendId} className="text-sm font-semibold">{t('pickDatasource')}</div>
    {mode === 'multi' && <p className="text-xs text-muted-foreground">{t('datasourceLimit')}</p>}
    {sources.isPending && <p role="status" className="text-xs text-muted-foreground">{t('datasourceLoading')}</p>}
    {sources.isError && <p role="alert" className="text-xs text-destructive">{t('datasourceLoadFailed')}</p>}
    <div className="max-h-56 overflow-y-auto rounded-md border border-border p-2">
      {body}
      {sources.hasNextPage && <Button type="button" size="sm" variant="outline" disabled={sources.isFetchingNextPage} onClick={() => void sources.fetchNextPage()}>{t('datasourceLoadMore')}</Button>}
      {sources.isSuccess && options.length === 0 && !sources.hasNextPage && <p className="text-xs text-muted-foreground">{t('datasourceNoneAddInSources')}</p>}
    </div>
    <Link href={sourcesHref} className="text-xs underline">{t('openSources')}</Link>
  </div>;
}

/**
 * Metric control driven by the selected provider's catalog: a single Select for charts, a checkbox list otherwise.
 * Open catalogs (`metrics_open`) also accept a typed metric id through a searchable field.
 *
 * @param catalog Catalog entry of the selected datasource's provider.
 * @param single True for one metric (chart), false for a multi-select list.
 * @param value Selected metric ids.
 * @param onChange Receives the next selection.
 * @param id DOM id of the single Select trigger (target of the field label).
 * @param labelId DOM id of the visible field label; the multi-select group is named by it.
 */
function MetricPicker({ catalog, single, value, onChange, id, labelId }: { catalog: ProviderMetrics; single: boolean; value: string[]; onChange: (next: string[]) => void; id: string; labelId: string }) {
  const t = useTranslations('gadgetSettings');
  /** Localized metric name (`metricNames.<id>`) falling back to the raw id, with the unit appended when known. */
  const label = (metric: string) => {
    const name = t.has(`metricNames.${metric}`) ? t(`metricNames.${metric}` as 'name') : metric;
    const unit = catalog.metrics.find((item) => item.id === metric)?.unit;
    return unit ? `${name} (${unit})` : name;
  };
  const ids = catalog.metrics.map((item) => item.id);
  // Saved or typed ids outside the listed catalog stay selectable so they can be unchecked.
  const all = [...ids, ...value.filter((item) => !ids.includes(item))];
  const typed = catalog.metrics_open && <SearchableSelect value="" options={ids} allowCustom aria-label={t('metricSearch')} placeholder={t('metricSearch')}
    searchLabel={t('metricSearch')} emptyLabel={t('metricEmpty')}
    onValueChange={(next) => onChange(single ? [next] : [...new Set([...value, next])])} />;
  if (single) {
    return <>
      <Select value={value[0] ?? ''} onValueChange={(next) => onChange([next])}>
        <SelectTrigger id={id} aria-label={t('metricLabel')} className="h-9"><SelectValue placeholder={t('metricPlaceholder')} /></SelectTrigger>
        <SelectContent>{all.map((metric) => <SelectItem key={metric} value={metric}>{label(metric)}</SelectItem>)}</SelectContent>
      </Select>
      {typed}
    </>;
  }
  return <div role="group" aria-labelledby={labelId} className="space-y-1">
    {all.map((metric) => <label key={metric} className="flex min-h-8 items-center gap-2 text-xs">
      <Checkbox checked={value.includes(metric)} onCheckedChange={(checked) => onChange(checked === true ? [...value, metric] : value.filter((item) => item !== metric))} />
      <span>{label(metric)}</span>
    </label>)}
    {typed}
  </div>;
}

/**
 * Symbol chips plus a searchable field over the provider's suggested symbols that also accepts typed text,
 * stored uppercase and de-duplicated.
 */
function SymbolPicker({ suggested, value, onChange, labelId }: { suggested: string[]; value: string[]; onChange: (next: string[]) => void; labelId: string }) {
  const t = useTranslations('gadgetSettings');
  return <div className="space-y-2">
    {value.length > 0 && <ul className="flex flex-wrap gap-1.5">{value.map((symbol) => <li key={symbol}>
      <Badge variant="outline" className="gap-1 font-mono">{symbol}
        <button type="button" aria-label={`${t('remove')} ${symbol}`} onClick={() => onChange(value.filter((item) => item !== symbol))}><Trash2 className="size-3" aria-hidden="true" /></button>
      </Badge></li>)}</ul>}
    <SearchableSelect value="" options={suggested.filter((item) => !value.includes(item))} allowCustom aria-labelledby={labelId} placeholder={t('symbolPlaceholder')}
      searchLabel={t('symbolSearch')} emptyLabel={t('symbolEmpty')}
      onValueChange={(next) => { const symbol = next.trim().toUpperCase(); if (symbol && !value.includes(symbol)) onChange([...value, symbol]); }} />
  </div>;
}

/**
 * Highlight-rule subform: lists the draft rules and adds keyword rules with a severity and notify flag.
 * Rules are plain local state; the editor only persists them when this list was touched.
 */
function HighlightRulesField({ rules, onChange }: { rules: HighlightRule[]; onChange: (rules: HighlightRule[]) => void }) {
  const ts = useTranslations('uiSweep');
  const [keywords, setKeywords] = useState('');
  const [severity, setSeverity] = useState<HighlightRule['severity']>('warning');
  const [notify, setNotify] = useState(true);
  const notifyId = useId();
  /** Appends a rule built from the subform fields, ignoring empty keyword text. */
  const add = () => {
    const parsed = splitList(keywords);
    if (parsed.length === 0) return;
    onChange([...rules, { id: uuid7(), keywords: parsed, severity, notify }]);
    setKeywords('');
  };
  return <div className="space-y-2.5 border-t border-border pt-3">
    <div className="flex items-center justify-between">
      <span className="text-xs font-bold">{ts('gsRules', { count: rules.length })}</span>
      <span className="font-mono text-[10px] text-muted-foreground">{ts('gsRulesHint')}</span>
    </div>
    {rules.length > 0 && <ul className="space-y-1.5">{rules.map((rule) => <li key={rule.id} className="flex items-center justify-between rounded-lg border border-border p-2 text-xs">
      <span className="flex min-w-0 items-center gap-2">
        <Badge variant={rule.severity === 'critical' ? 'destructive' : 'outline'}>{ts(rule.severity === 'critical' ? 'gsCritical' : rule.severity === 'warning' ? 'gsWarning' : 'gsInfo')}</Badge>
        <span className="truncate font-medium">{rule.keywords.join(', ')}</span>
        {rule.notify && <Bell className="size-3 shrink-0 text-muted-foreground" aria-hidden="true" />}
      </span>
      <Button type="button" size="icon" variant="ghost" aria-label={ts('gsRemoveRule')} onClick={() => onChange(rules.filter((item) => item.id !== rule.id))}><Trash2 className="size-3.5" aria-hidden="true" /></Button>
    </li>)}</ul>}
    <div className="space-y-2 rounded-lg border border-border p-2.5">
      <span className="text-[11px] font-semibold text-muted-foreground">{ts('gsAddRule')}</span>
      <div className="flex items-center gap-2">
        <Input value={keywords} onChange={(event) => setKeywords(event.target.value)} aria-label={ts('gsRuleKeywords')} placeholder={ts('gsRuleKeywords')} className="h-8 flex-1 text-xs" />
        <Select value={severity} onValueChange={(next) => setSeverity(next as HighlightRule['severity'])}>
          <SelectTrigger className="h-8 w-28 text-xs" aria-label={ts('gsWarning')}><SelectValue /></SelectTrigger>
          <SelectContent>
            <SelectItem value="info">{ts('gsInfo')}</SelectItem>
            <SelectItem value="warning">{ts('gsWarning')}</SelectItem>
            <SelectItem value="critical">{ts('gsCritical')}</SelectItem>
          </SelectContent>
        </Select>
        <Button type="button" size="sm" disabled={!keywords.trim()} onClick={add}><Plus className="size-3" aria-hidden="true" />{ts('gsAdd')}</Button>
      </div>
      <div className="flex items-center gap-2">
        <Checkbox id={notifyId} checked={notify} onCheckedChange={(next) => setNotify(next === true)} />
        <Label htmlFor={notifyId} className="cursor-pointer text-[11px] font-normal">{ts('gsNotify')}</Label>
      </div>
    </div>
  </div>;
}

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
 * One editor for creating a generic renderer template on the dashboard and editing a saved definition. New gadgets
 * select a display type, then an already-configured compatible datasource; datasource creation remains in Settings.
 * Saving uses the existing revision-guarded definition contract and never changes layout placement.
 */
export function GadgetEditor({ definition = null, onSaved, onCancel, footerExtra, saveLabel, afterName }: GadgetEditorProps) {
  const t = useTranslations('gadgetSettings');
  const td = useTranslations('dashboard');
  const session = useWorkspaceSession();
  const client = useQueryClient();
  const nameId = useId();
  const limitId = useId();
  const renderers = useQuery({ queryKey: dashboardKeys.renderers, queryFn: ({ signal }) => listGadgetRenderers(signal), staleTime: Infinity });
  const templates = useQuery({ queryKey: dashboardKeys.templates, queryFn: ({ signal }) => listGadgetTemplates(signal), enabled: !definition, staleTime: Infinity });
  const catalogQuery = useQuery({ queryKey: dashboardKeys.metricCatalog, queryFn: ({ signal }) => getMetricCatalog(signal), staleTime: Infinity });
  const counts = useQuery({ queryKey: dashboardKeys.compatibleCounts, queryFn: ({ signal }) => listCompatibleSourceCounts(signal), enabled: !definition });
  const [choice, setChoice] = useState<string | null>(definition ? definition.renderer : null);
  const { rows: sourceRows } = useSourceRows();
  const [renderer, setRenderer] = useState(definition?.renderer ?? '');
  const [name, setName] = useState(definition?.name ?? '');
  const [sourceIds, setSourceIds] = useState<string[]>(definition?.source_ids ?? []);
  const [keywords, setKeywords] = useState((definition?.filters?.keywords ?? []).join(', '));
  const [exclude, setExclude] = useState((definition?.filters?.exclude_keywords ?? []).join(', '));
  const [limit, setLimit] = useState(String(definition?.filters?.limit ?? 25));
  const [symbols, setSymbols] = useState((definition?.scope?.symbols ?? []).join(', '));
  const [metrics, setMetrics] = useState((definition?.scope?.metrics ?? []).join(', '));
  // A saved definition with no lookback keeps it unset (empty field) so a rename never invents one.
  const [lookback, setLookback] = useState(definition ? String(definition.scope?.lookback_days ?? '') : String(fallbackLookback(renderer)));
  // Once the owner typed a lookback, a later provider change must not overwrite it with the catalog default.
  const [lookbackTouched, setLookbackTouched] = useState(false);
  const [regions, setRegions] = useState((definition?.scope?.regions ?? []).join(', '));
  // Chart region (one value: names such as "Korea, Rep." contain commas, so it is not a comma list).
  const [region, setRegion] = useState(definition?.scope?.regions?.[0] ?? '');
  const [channels, setChannels] = useState((definition?.scope?.channel_ids ?? []).join(', '));
  const [cii, setCii] = useState((definition?.scope?.cii_country_codes ?? []).join(', '));
  const [layers, setLayers] = useState<string[]>(definition?.scope?.map_layer_ids ?? []);
  const [engine, setEngine] = useState<'globe' | 'flat'>(definition?.filters?.map_engine ?? 'globe');
  const [precise, setPrecise] = useState(definition?.filters?.show_precise_locations ?? false);
  const [rules, setRules] = useState<HighlightRule[]>(definition?.highlight_rules ?? []);
  const [rulesTouched, setRulesTouched] = useState(false);
  const [touched, setTouched] = useState(false);
  // Latest saved revision so a retry after a failed follow-up step (for example a title patch) does not conflict.
  const [base, setBase] = useState<GadgetDefinition | null>(definition);

  const descriptor: GadgetRenderer | undefined = (renderers.data ?? []).find((item) => item.id === renderer);
  const template = templates.data?.find((item) => item.renderer === renderer);
  const sourceMode = descriptor?.source_mode ?? 'none';
  const acceptedShapes = descriptor?.accepted_shapes ?? null;
  // Existing gadgets with no source keep saving; new ones must pick a datasource when the renderer reads sources.
  const needsSource = sourceMode !== 'none' && (!definition || definition.source_ids.length > 0);
  const limitNumber = Number(limit);
  const limitValid = Number.isInteger(limitNumber) && limitNumber >= 1 && limitNumber <= 100;
  // Empty means unset. Map/intelligence windows are capped to 30 on save, so only the other renderers can be out of range.
  const lookbackNumber = Number(lookback);
  const lookbackValid = lookback.trim() === '' || renderer === 'map' || renderer === 'intelligence_panel' || (Number.isInteger(lookbackNumber) && lookbackNumber >= 1 && lookbackNumber <= 3660);
  const nameValid = name.trim().length > 0;
  const sourceValid = !needsSource || sourceIds.length > 0;
  const keywordsValid = splitList(keywords).length <= 32 && splitList(exclude).length <= 32;
  const fields = fieldsFor(renderer);
  // Catalog entry of the first selected datasource's provider; absent means the free-text fallbacks stay in place.
  const provider = sourceRows.find((row) => row.id === sourceIds[0])?.provider ?? null;
  const entry = provider ? catalogQuery.data?.[provider] : undefined;
  const showSymbols = fields.has('symbols') && entry?.symbol_keyed === true;
  // Provider unknown (catalog failed, source absent from the loaded page, provider not in the catalog): symbols stay visible
  // and editable as free text so a saved definition is never opaque. Hidden while the catalog is still loading or nothing is bound.
  const showSymbolsFallback = fields.has('symbols') && entry === undefined && !catalogQuery.isPending && (sourceIds.length > 0 || splitList(symbols, true).length > 0);
  // Region-keyed providers (World Bank countries, OpenAQ locations) plot one region per chart.
  const showRegion = renderer === 'finance_chart' && entry?.region_keyed === true;
  // Provider catalog drives the metric control; non-chart renderers with a catalog render a checkbox group.
  const metricGroup = entry !== undefined && entry.metrics.length > 0 && renderer !== 'finance_chart';
  // Measurement renderers wait for the catalog so the default metric comes from the provider, not a guess.
  const catalogLoading = fields.has('metrics') && catalogQuery.isPending;
  const hasAdvanced = fields.has('keywords') || fields.has('limit') || fields.has('rules') || fields.has('lookback') || fields.has('map') || fields.has('cii') || fields.has('channels');
  const detailsRef = useRef<HTMLDetailsElement>(null);
  // Why Save is disabled; shown beside Save so a problem inside the collapsed Advanced section is never hidden.
  const blockReason = !nameValid ? t('nameRequired') : !sourceValid ? t('datasourceRequired') : !limitValid ? t('limitHelp') : !lookbackValid ? t('lookbackHelp') : !keywordsValid ? t('keywordsHelp') : catalogLoading ? t('metricCatalogLoading') : null;
  // Open Advanced as soon as one of its fields is invalid; Save is disabled then, so no submit could do it.
  useEffect(() => { if ((!limitValid || !keywordsValid || !lookbackValid) && detailsRef.current) detailsRef.current.open = true; }, [limitValid, keywordsValid, lookbackValid]);
  const valid = renderer !== '' && descriptor?.runtime_state === 'available' && nameValid && sourceValid && limitValid && lookbackValid && keywordsValid && !catalogLoading;

  /** Starts a new definition from one available renderer and clears configuration from any prior selection. */
  function choose(nextRenderer: string) {
    // `template` still reflects the previous renderer here, so resolve the picked one explicitly.
    const picked = templates.data?.find((item) => item.renderer === nextRenderer);
    setChoice(nextRenderer);
    // Reset every draft field so nothing typed for a previous template leaks into the next one.
    setSourceIds([]);
    setKeywords((picked?.filters.keywords ?? []).join(', '));
    setExclude((picked?.filters.exclude_keywords ?? []).join(', '));
    setRegions((picked?.scope.regions ?? []).join(', '));
    setRegion('');
    setChannels((picked?.scope.channel_ids ?? []).join(', '));
    setCii((picked?.scope.cii_country_codes ?? []).join(', '));
    setLayers(picked?.scope.map_layer_ids ?? []);
    setSymbols((picked?.scope.symbols ?? []).join(', '));
    setMetrics((picked?.scope.metrics ?? []).join(', '));
    setLookback(String(picked?.scope.lookback_days ?? fallbackLookback(nextRenderer)));
    setLookbackTouched(false);
    setLimit(String(picked?.filters.limit ?? 25));
    setRules([]);
    setRulesTouched(false);
    setRenderer(nextRenderer);
    setName(templateName(nextRenderer, t));
  }

  /**
   * Applies a datasource selection. For charts the default metric follows the new provider: `close` (else the
   * first catalog metric) is chosen whenever the current one is not offered (open catalogs keep typed ids).
   * When the provider actually changed and the new one suggests symbols, symbols it does not suggest are dropped;
   * a new chart's lookback follows the provider's catalog default, an empty chart symbol list is preselected with the first suggestion, and region-keyed charts do the same for
   * the region. Re-selecting the same provider keeps the owner's symbols and region.
   */
  function changeSources(next: string[]) {
    /** Provider id of a loaded source row, or null when the id is absent or its page is not loaded yet. */
    const providerOf = (id: string | undefined) => sourceRows.find((row) => row.id === id)?.provider ?? null;
    const providerChanged = providerOf(next[0]) !== providerOf(sourceIds[0]);
    setSourceIds(next);
    const nextEntry = catalogQuery.data?.[providerOf(next[0]) ?? ''];
    const current = splitList(metrics)[0];
    // A changed provider must not keep a metric it does not list, even when its catalog is open (typed ids are
    // only trusted while the provider stays the same).
    const metricFits = current !== undefined && (nextEntry?.metrics.some((item) => item.id === current)
      || (!providerChanged && nextEntry?.metrics_open === true));
    if (renderer === 'finance_chart' && nextEntry && nextEntry.metrics.length > 0 && !metricFits) setMetrics(defaultMetric(nextEntry));
    // A Metric gadget keeps only metrics the new provider lists (the save would 422 on a stale id); typed ids of
    // an open catalog are trusted only while the provider stays the same.
    if (renderer === 'provider_observation' && providerChanged && nextEntry) {
      setMetrics(splitList(metrics).filter((id) => nextEntry.metrics.some((item) => item.id === id)).join(', '));
    }
    if (!nextEntry) return;
    // A new chart on a slow series (annual World Bank, monthly OECD) starts with a window that spans several periods;
    // a Metric gadget never starts below its 30-day runtime window (same rule as useEffectiveLookback).
    if ((renderer === 'finance_chart' || renderer === 'provider_observation') && !base && providerChanged && !lookbackTouched) setLookback(String(Math.max(nextEntry.default_lookback_days ?? 0, renderer === 'provider_observation' ? fallbackLookback(renderer) : 0) || fallbackLookback(renderer)));
    if (fields.has('symbols')) {
      let nextSymbols = splitList(symbols, true);
      // Changed provider: keep only symbols it suggests; a provider without suggestions cannot validate any.
      if (providerChanged) nextSymbols = nextSymbols.filter((symbol) => nextEntry.suggested_symbols.includes(symbol));
      if (renderer === 'finance_chart' && nextSymbols.length === 0 && nextEntry.symbol_keyed && nextEntry.seed_symbol && nextEntry.suggested_symbols.length > 0) {
        nextSymbols = [nextEntry.suggested_symbols[0]];
      }
      setSymbols(nextSymbols.join(', '));
    }
    if (renderer === 'finance_chart') {
      if (!nextEntry.region_keyed) setRegion('');
      else if (providerChanged || !region) setRegion(nextEntry.suggested_regions[0] ?? '');
    }
  }

  const save = useMutation({
    mutationFn: async () => {
      const filters = {
        limit: limitNumber,
        keywords: splitList(keywords).length > 0 ? [...new Set(splitList(keywords))] : undefined,
        exclude_keywords: splitList(exclude).length > 0 ? [...new Set(splitList(exclude))] : undefined,
        ...(renderer === 'map' ? { map_engine: engine, show_precise_locations: precise } : {}),
      };
      // Empty means unset (stored null stays null); JSON drops the undefined key.
      const days = lookback.trim() === '' ? undefined : Number(lookback);
      const capped = days === undefined ? undefined : Math.min(30, days);
      // Unchanged source on a saved definition: stored metrics/symbols/regions go back as stored (a preset-created
      // chart may legitimately have none yet); defaults are injected only for new definitions or a changed source.
      const sourcesUnchanged = base !== null && JSON.stringify(sourceIds) === JSON.stringify(base.source_ids);
      const sameList = (a: string[], b?: string[]) => JSON.stringify(a) === JSON.stringify(b ?? []);
      // The region field holds one value, so a legacy chart with several stored regions is unchanged while that field still shows the first.
      const regionUnchanged = base !== null && region.trim() === (base.scope?.regions?.[0] ?? '');
      const keysChanged = base !== null && !(sameList(splitList(symbols, true), base.scope?.symbols) && regionUnchanged);
      // A legacy multi-metric chart keeps its stored metrics exactly while nothing that affects them changed.
      const metricsUnchanged = base !== null && sameList(splitList(metrics), base.scope?.metrics);
      const scope = {
        ...(base?.scope ?? template?.scope ?? {}),
        ...(renderer === 'finance_chart' ? {
          symbols: entry && entry.symbol_keyed !== true ? [] : splitList(symbols, true),
          // A chart plots one metric: a legacy multi-metric row keeps its first one so a symbol edit still validates.
          // Without a known provider entry nothing is invented: stored metrics go back unchanged (even when empty).
          // Adding symbols/regions to a chart saved without any makes it plottable, so the default metric is injected then too.
          metrics: entry && (!sourcesUnchanged || keysChanged) ? [splitList(metrics)[0] ?? defaultMetric(entry)] : metricsUnchanged && sourcesUnchanged && !keysChanged ? splitList(metrics) : splitList(metrics).slice(0, 1),
          regions: entry && entry.region_keyed !== true ? [] : regionUnchanged && sourcesUnchanged ? (base?.scope?.regions ?? []) : region.trim() ? [region.trim()] : [],
          lookback_days: days,
        }
          // A Metric gadget on a symbol-keyed provider keeps its picked symbols; clearing them must persist, so always send the list.
          : renderer === 'provider_observation' ? { metrics: splitList(metrics), symbols: entry && !entry.symbol_keyed ? [] : splitList(symbols, true), lookback_days: days }
          : renderer === 'weather' ? { metrics: splitList(metrics), lookback_days: days }
            : renderer === 'map' ? { regions: splitList(regions), map_layer_ids: layers, lookback_days: capped }
              : renderer === 'intelligence_panel' ? { regions: splitList(regions), cii_country_codes: splitList(cii, true), lookback_days: capped }
                : renderer === 'telegram_feed' ? { channel_ids: splitList(channels) }
                  : splitList(symbols, true).length > 0 ? { symbols: splitList(symbols, true) } : {}),
      };
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
      if (base) setBase(saved);
      await onSaved(saved);
    },
  });

  // A 422 with a known code and `details.field` is shown under that field; anything else stays the generic banner.
  const failure = save.error instanceof ApiError && save.error.status === 422 && t.has(`fieldErrors.${save.error.code}`)
    ? { field: String((save.error.details as { field?: unknown } | null | undefined)?.field ?? ''), message: t(`fieldErrors.${save.error.code}` as 'name') }
    // FastAPI's code-less pydantic 422 lists the offending `loc`; a lookback range violation maps to its own field.
    : save.error instanceof ApiError && save.error.status === 422 && namesLookbackField(save.error.details)
      ? { field: 'lookback_days', message: t('lookbackHelp') } : null;
  /** Localized server error for one field name, or null when the failure belongs elsewhere. */
  const fieldError = (field: string) => (failure?.field === field ? failure.message : null);
  // map_layer_ids lives inside Advanced, so surface it there as soon as the server rejects it.
  useEffect(() => { if ((failure?.field === 'map_layer_ids' || failure?.field === 'lookback_days') && detailsRef.current) detailsRef.current.open = true; }, [failure?.field]);
  const showTemplates = !definition && choice === null;
  // A field error for a field this editor does not currently render would otherwise be invisible, so show it near Save.
  /** True when the editor currently renders a control for the server-reported field name. */
  const fieldRendered = (field: string) => field === 'symbols' ? showSymbols || showSymbolsFallback : field === 'regions' ? showRegion : field === 'metrics' ? fields.has('metrics') : field === 'map_layer_ids' ? fields.has('map') : field === 'lookback_days' ? fields.has('lookback') : field === 'source_ids';
  const unrenderedFailure = failure && !fieldRendered(failure.field) ? failure.message : null;
  const error = save.error && !failure ? t((apiFailureKey(save.error) === 'conflict' ? 'conflict' : 'saveFailed')) : unrenderedFailure;
  const sourceErrorId = `${nameId}-source-error`;

  if (showTemplates) {
    return <div className="space-y-3">
      <h3 className="text-sm font-semibold">{t('pickTemplate')}</h3>
      {(renderers.isPending || templates.isPending) && <p role="status" className="text-xs text-muted-foreground">{t('loading')}</p>}
      {(renderers.isError || templates.isError) && <p role="alert" className="text-xs text-destructive">{t('loadFailed')} <Button type="button" size="sm" variant="outline" onClick={() => { void renderers.refetch(); void templates.refetch(); }}>{t('retry')}</Button></p>}
      {counts.isError && <p role="status" className="text-xs text-muted-foreground">{t('compatibleCountsFailed')} <Button type="button" size="sm" variant="outline" onClick={() => { void counts.refetch(); }}>{t('retry')}</Button></p>}
      {renderers.isSuccess && templates.isSuccess && <ul className="grid gap-3 sm:grid-cols-2">
        {templates.data.flatMap((item) => {
          const info = renderers.data.find((rendererInfo) => rendererInfo.id === item.renderer && rendererInfo.runtime_state === 'available');
          if (!info) return [];
          const count = info.source_mode === 'none' ? null : counts.data?.[item.renderer] ?? null;
          const body = <>
            <GadgetTemplateSample renderer={item.renderer} />
            <span className="min-w-0"><span className="block text-sm font-semibold">{templateName(item.renderer, t)}</span>
              <span className="block text-xs text-muted-foreground">{templateDescription(item.renderer, t)}</span>
              {count !== null && <span className="block text-xs text-muted-foreground">{count === 0 ? t('addCompatibleSource') : t('compatibleCount', { count })}</span>}</span>
          </>;
          const tileClass = 'flex min-h-11 w-full items-center gap-3 rounded-lg border border-border p-3 text-left hover:border-primary focus-visible:outline-2';
          // A zero count means choosing this tile could never be saved, so it routes to the filtered Sources page instead.
          return [<li key={item.id}>{count === 0
            ? <Link href={sourcesLinkFor(info)} className={tileClass}>{body}</Link>
            : <button type="button" onClick={() => choose(item.renderer)} className={tileClass}>{body}</button>}</li>];
        })}
      </ul>}
      {onCancel && <div className="flex justify-end"><Button type="button" variant="outline" onClick={onCancel}>{t('cancel')}</Button></div>}
    </div>;
  }

  return <form className="space-y-4" noValidate onSubmit={(event) => { event.preventDefault(); setTouched(true); if (valid) save.mutate(); }}>
    <fieldset disabled={save.isPending} className="m-0 min-w-0 space-y-4 border-0 p-0">
      {!definition && <div className="flex items-center gap-2 text-xs text-muted-foreground">
        <Check className="size-3.5" aria-hidden="true" />
        <span>{templateName(renderer, t)}</span>
        <Button type="button" variant="ghost" size="sm" onClick={() => { setChoice(null); setRenderer(''); }}>{t('backToTemplates')}</Button>
      </div>}
      {sourceMode !== 'none' && <div>
        <DatasourcePicker acceptedShapes={acceptedShapes} mode={sourceMode === 'single' ? 'single' : 'multi'} value={sourceIds} onChange={changeSources}
          invalid={!sourceValid} describedBy={!sourceValid ? sourceErrorId : undefined} sourcesHref={descriptor ? sourcesLinkFor(descriptor) : undefined} />
        {fieldError('source_ids') && <p role="alert" className="mt-1 text-xs text-destructive">{fieldError('source_ids')}</p>}
        {!sourceValid && <p id={sourceErrorId} className="mt-1 text-xs text-muted-foreground">{t('datasourceRequired')}</p>}
      </div>}
      {fields.has('metrics') && <div data-field="metrics" className="space-y-1.5">
        {/* A checkbox group has no single control to point `for` at, so it is named through aria-labelledby instead. */}
        <Label id={`${nameId}-met-label`} htmlFor={metricGroup ? undefined : `${nameId}-met`} className="text-xs font-semibold">{t('metricLabel')}</Label>
        {entry && entry.metrics.length > 0
          ? <MetricPicker id={`${nameId}-met`} labelId={`${nameId}-met-label`} catalog={entry} single={renderer === 'finance_chart'} value={renderer === 'finance_chart' && splitList(metrics).length === 0 ? [defaultMetric(entry)] : splitList(metrics)} onChange={(next) => setMetrics(next.join(', '))} />
          : <Input id={`${nameId}-met`} value={metrics} onChange={(event) => setMetrics(event.target.value)} placeholder={renderer === 'finance_chart' ? 'close, volume' : renderer === 'provider_observation' ? td('providerObsMetricsPlaceholder') : 'temperature_2m, precipitation'} className="h-8 font-mono text-xs" />}
        {fieldError('metrics') && <p role="alert" className="text-xs text-destructive">{fieldError('metrics')}</p>}
      </div>}
      {showSymbols && entry && <div data-field="symbols" className="space-y-1.5">
        <Label id={`${nameId}-sym-label`} className="text-xs font-semibold">{t('symbolsLabel')}</Label>
        <SymbolPicker labelId={`${nameId}-sym-label`} suggested={entry.suggested_symbols} value={splitList(symbols, true)} onChange={(next) => setSymbols(next.join(', '))} />
        {!entry.seed_symbol && <p className="text-xs text-muted-foreground">{t('symbolsOwnerHint')}</p>}
        {fieldError('symbols') && <p role="alert" className="text-xs text-destructive">{fieldError('symbols')}</p>}
      </div>}
      {showSymbolsFallback && <div data-field="symbols" className="space-y-1.5">
        <Label htmlFor={`${nameId}-sym`} className="text-xs font-semibold">{t('symbolsLabel')}</Label>
        <Input id={`${nameId}-sym`} value={symbols} onChange={(event) => setSymbols(event.target.value)} aria-describedby={`${nameId}-sym-help`} className="h-8 font-mono text-xs" />
        <p id={`${nameId}-sym-help`} className="text-xs text-muted-foreground">{t('symbolsUnknownProviderHint')}</p>
        {fieldError('symbols') && <p role="alert" className="text-xs text-destructive">{fieldError('symbols')}</p>}
      </div>}
      {showRegion && entry && <div data-field="regions" className="space-y-1.5">
        <Label id={`${nameId}-reg-label`} className="text-xs font-semibold">{t('regionLabel')}</Label>
        <SearchableSelect value={region} options={entry.suggested_regions} allowCustom aria-labelledby={`${nameId}-reg-label`} aria-describedby={`${nameId}-reg-help`} placeholder={t('regionPlaceholder')}
          searchLabel={t('regionSearch')} emptyLabel={t('regionEmpty')} onValueChange={(next) => setRegion(next.trim())} />
        <p id={`${nameId}-reg-help`} className="text-xs text-muted-foreground">{t('regionHelp')}</p>
        {fieldError('regions') && <p role="alert" className="text-xs text-destructive">{fieldError('regions')}</p>}
      </div>}
      <div className="space-y-1.5">
        <Label htmlFor={nameId} className="text-xs font-semibold">{t('name')}</Label>
        <Input id={nameId} value={name} maxLength={120} className="h-9" aria-invalid={touched && !nameValid} aria-describedby={touched && !nameValid ? `${nameId}-error` : undefined} onBlur={() => setTouched(true)} onChange={(event) => { setTouched(true); setName(event.target.value); }} />
        {touched && !nameValid && <p id={`${nameId}-error`} role="alert" className="text-xs text-destructive">{t('nameRequired')}</p>}
      </div>
      {afterName}
      {hasAdvanced && <details ref={detailsRef} className="rounded-md border border-border p-3">
        <summary className="cursor-pointer text-sm font-semibold">{t('advanced')}</summary>
        <div className="mt-3 space-y-3">
          {fields.has('keywords') && <div className="grid gap-3 sm:grid-cols-2">
            <div className="space-y-1.5"><Label htmlFor={`${nameId}-kw`} className="text-xs font-semibold">{t('keywords')}</Label><Input id={`${nameId}-kw`} value={keywords} onChange={(event) => setKeywords(event.target.value)} placeholder={t('keywordsPlaceholder')} className="h-8 text-xs" /></div>
            <div className="space-y-1.5"><Label htmlFor={`${nameId}-ex`} className="text-xs font-semibold">{t('excludeKeywords')}</Label><Input id={`${nameId}-ex`} value={exclude} onChange={(event) => setExclude(event.target.value)} placeholder={t('excludePlaceholder')} className="h-8 text-xs" /></div>
          </div>}
          {fields.has('limit') && <div className="space-y-1.5">
            <Label htmlFor={limitId} className="text-xs font-semibold">{t('limit')}</Label>
            <Input id={limitId} type="number" min={1} max={100} value={limit} aria-invalid={!limitValid} aria-describedby={`${limitId}-help`} onChange={(event) => setLimit(event.target.value)} className="h-8 w-28 font-mono text-xs" />
            <p id={`${limitId}-help`} className={limitValid ? 'text-xs text-muted-foreground' : 'text-xs text-destructive'}>{t('limitHelp')}</p>
          </div>}
          {fields.has('lookback') && <div className="space-y-1.5"><Label htmlFor={`${nameId}-look`} className="text-xs font-semibold">{td('observationLookback')}</Label><Input id={`${nameId}-look`} type="number" min={1} max={3660} value={lookback} aria-invalid={!lookbackValid || fieldError('lookback_days') !== null} aria-describedby={lookbackValid ? undefined : `${nameId}-look-help`} onChange={(event) => { setLookback(event.target.value); setLookbackTouched(true); }} className="h-8 w-28 font-mono text-xs" />
            {!lookbackValid && <p id={`${nameId}-look-help`} className="text-xs text-destructive">{t('lookbackHelp')}</p>}
            {fieldError('lookback_days') && <p role="alert" data-field="lookback_days" className="text-xs text-destructive">{fieldError('lookback_days')}</p>}</div>}
          {(fields.has('map') || fields.has('cii')) && <section className="space-y-3 rounded-md border border-border p-3">
            <h4 className="text-sm font-semibold">{renderer === 'map' ? td('mapScopeTitle') : td('intelligenceScopeTitle')}</h4>
            <div className="space-y-1.5"><Label htmlFor={`${nameId}-reg`} className="text-xs font-semibold">{td('mapRegionsLabel')}</Label><Input id={`${nameId}-reg`} value={regions} onChange={(event) => setRegions(event.target.value)} placeholder={td('mapRegionsPlaceholder')} className="h-8 font-mono text-xs" /><p className="text-xs text-muted-foreground">{td('mapRegionsHelp')}</p></div>
            <div className="space-y-1.5"><Label htmlFor={`${nameId}-mlook`} className="text-xs font-semibold">{td('mapLookbackLabel')}</Label><Input id={`${nameId}-mlook`} type="number" min={1} max={30} value={lookback} onChange={(event) => setLookback(event.target.value)} className="h-8 font-mono text-xs" /></div>
            {fields.has('map') && <>
              <div className="space-y-1.5">
                <Label htmlFor={`${nameId}-eng`} className="text-xs font-semibold">{td('mapEngineLabel')}</Label>
                <Select value={engine} onValueChange={(next) => setEngine(next as 'globe' | 'flat')}>
                  <SelectTrigger id={`${nameId}-eng`} className="h-9"><SelectValue /></SelectTrigger>
                  <SelectContent><SelectItem value="globe">{td('mapGlobeEngine')}</SelectItem><SelectItem value="flat">{td('mapFlatEngine')}</SelectItem></SelectContent>
                </Select>
              </div>
              <fieldset data-field="map_layer_ids" className="space-y-2">
                <legend className="text-xs font-semibold">{td('mapLayerLabel')}</legend>
                {MAP_LAYERS.map((layerId) => <label key={layerId} className="flex items-start gap-2 text-xs">
                  <Checkbox checked={layers.includes(layerId)} disabled={layerId !== 'world_observations' && !layers.includes(layerId)} onCheckedChange={(checked) => setLayers((current) => checked === true ? [...current, layerId] : current.filter((item) => item !== layerId))} />
                  <span>{td(`mapLayer_${layerId}`)}{layerId !== 'world_observations' && <span className="block text-muted-foreground">{td('mapLayerUnavailable')}</span>}</span>
                </label>)}
              {fieldError('map_layer_ids') && <p role="alert" className="text-xs text-destructive">{fieldError('map_layer_ids')}</p>}
              </fieldset>
              <label className="flex items-start gap-2 text-xs"><Checkbox checked={precise} onCheckedChange={(checked) => setPrecise(checked === true)} /><span>{td('mapPreciseLocationOptIn')}</span></label>
            </>}
            {fields.has('cii') && <div className="space-y-1.5"><Label htmlFor={`${nameId}-cii`} className="text-xs font-semibold">{td('ciiCountryScopeLabel')}</Label><Input id={`${nameId}-cii`} value={cii} onChange={(event) => setCii(event.target.value)} placeholder="US, VN" className="h-8 font-mono text-xs uppercase" /><p className="text-xs text-muted-foreground">{td('ciiCountryScopeHelp')}</p></div>}
          </section>}
          {fields.has('channels') && <div className="space-y-1.5"><Label htmlFor={`${nameId}-tg`} className="text-xs font-semibold">{td('telegramChannels')}</Label><Input id={`${nameId}-tg`} value={channels} onChange={(event) => setChannels(event.target.value)} placeholder="-1001234567890, 123456789" aria-describedby={`${nameId}-tg-help`} className="h-8 font-mono text-xs" /><p id={`${nameId}-tg-help`} className="text-xs text-muted-foreground">{td('telegramChannelsHelp')}</p></div>}
          {fields.has('rules') && <HighlightRulesField rules={rules} onChange={(next) => { setRules(next); setRulesTouched(true); }} />}
        </div>
      </details>}
    </fieldset>
    {error && <p role="alert" className="text-xs text-destructive">{error}</p>}
    {blockReason && <p id={`${nameId}-block`} role="status" className="text-xs text-muted-foreground">{blockReason}</p>}
    <div className="flex flex-wrap items-center justify-end gap-2">
      {footerExtra && <div className="me-auto">{footerExtra}</div>}
      {onCancel && <Button type="button" variant="outline" disabled={save.isPending} onClick={onCancel}>{t('cancel')}</Button>}
      <Button type="submit" disabled={save.isPending || !valid} aria-describedby={blockReason ? `${nameId}-block` : undefined}>{save.isPending ? t('saving') : (saveLabel ?? t('save'))}</Button>
    </div>
  </form>;
}
