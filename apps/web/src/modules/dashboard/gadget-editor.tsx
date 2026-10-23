'use client';

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Bell, Check, Plus, Trash2 } from 'lucide-react';
import Link from 'next/link';
import { useTranslations } from 'next-intl';
import { useEffect, useId, useMemo, useRef, useState, type ReactNode } from 'react';
import { AlertDialog, AlertDialogContent, AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle } from '@/components/ui/alert-dialog';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { RadioGroup, RadioGroupItem } from '@/components/ui/radio-group';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { apiFailureKey } from '@/core/api-failure-key';
import { useWorkspaceSession } from '@/core/app-shell/workspace-shell';
import { uuid7 } from '@/core/uuid7';
import { connectorKeys, enableBuiltinSource, listBuiltinSources, type BuiltinSource, type DeclaredUse } from '@/modules/sources/api';
import { BuiltinTermsFields, builtinEnableErrorKey } from '@/modules/sources/builtin-sources';
import type { SourceAiOptions } from '@/modules/sources/source-ai-toggles';
import { waitForActivation } from '@/modules/sources/use-source-actions';
import {
  createGadgetDefinition, dashboardKeys, listGadgetRenderers, listGadgetSources, listGadgetTemplates, patchGadgetDefinition,
  type GadgetDefinition, type GadgetRenderer, type GadgetTemplate, type HighlightRule,
} from './api';

const MAX_SOURCES = 32;
const MAP_LAYERS = ['world_observations', 'military', 'economic', 'disaster', 'escalation'] as const;
const CUSTOM = '__custom__';

/**
 * Localized display name of a renderer id (`renderers.<id>`); unknown ids fall back to the raw id.
 *
 * @param t Translator scoped to the gadgetSettings namespace.
 * @param id Renderer id from the renderer catalog.
 */
export function rendererName(t: ReturnType<typeof useTranslations>, id: string): string {
  return t.has(`renderers.${id}`) ? t(`renderers.${id}` as 'name') : id;
}

/** Splits comma-separated text into trimmed, non-empty entries. */
function splitList(value: string, upper = false): string[] {
  return value.split(',').map((item) => (upper ? item.trim().toUpperCase() : item.trim())).filter(Boolean);
}

type PickerOption =
  | { kind: 'source'; id: string; name: string; badge: 'incompatible' | 'inactive' | null }
  | { kind: 'builtin'; builtin: BuiltinSource };

/**
 * Datasource picker: merges active configured sources (filtered by provider) with built-ins that are not yet
 * enabled for those providers. Picking a built-in opens the enable dialog; the new source is auto-selected and the
 * picker reports `onActivatingChange(true)` until the connector is active so the caller can keep Save disabled.
 * `mode` single renders a radio group, multi renders checkboxes capped at 32. Saved selections that no longer
 * match the providers stay listed with a warning badge instead of being dropped. A failing built-in catalog degrades
 * silently to enabled sources only.
 *
 * @param providers Accepted providers; null means any provider (no built-in suggestions).
 * @param mode Selection cardinality from the renderer or template.
 * @param value Currently selected source ids.
 * @param onChange Receives the next selection.
 * @param onActivatingChange Fired with true while a just-enabled source is waiting to become active.
 */
export function DatasourcePicker({ providers, mode, value, onChange, onActivatingChange, invalid, describedBy }: {
  providers: readonly string[] | null;
  mode: 'single' | 'multi';
  value: string[];
  onChange: (sourceIds: string[]) => void;
  onActivatingChange?: (activating: boolean) => void;
  invalid?: boolean;
  describedBy?: string;
}) {
  const t = useTranslations('gadgetSettings');
  const client = useQueryClient();
  const legendId = useId();
  const sources = useQuery({ queryKey: [...dashboardKeys.sources, 'editor'], queryFn: ({ signal }) => listGadgetSources(100, undefined, signal) });
  const builtins = useQuery({ queryKey: connectorKeys.builtins, queryFn: ({ signal }) => listBuiltinSources(signal), retry: false });
  const [enabling, setEnabling] = useState<BuiltinSource | null>(null);
  const [activation, setActivation] = useState<{ id: string; name: string; phase: 'waiting' | 'failed'; code?: string | null } | null>(null);
  // Built-ins enabled in this session are hidden at once, before the catalog refetch reports them as enabled.
  const [enabledBuiltins, setEnabledBuiltins] = useState<string[]>([]);
  // Latest selection, so an async activation failure removes only its own id and keeps later changes.
  const valueRef = useRef(value);
  useEffect(() => { valueRef.current = value; }, [value]);
  const allowed = (provider: string | null) => providers === null || (provider !== null && providers.includes(provider));

  const options = useMemo<PickerOption[]>(() => {
    const rows: PickerOption[] = [];
    const seen = new Set<string>();
    const known = sources.data?.items ?? [];
    for (const item of known) {
      const selected = value.includes(item.id);
      if (!selected && !(item.status === 'active' && allowed(item.provider))) continue;
      seen.add(item.id);
      rows.push({ kind: 'source', id: item.id, name: item.name, badge: !allowed(item.provider) ? 'incompatible' : item.status !== 'active' ? 'inactive' : null });
    }
    // A saved id missing from the bounded listing stays visible so the owner can deselect it.
    for (const id of value) if (!seen.has(id)) rows.push({ kind: 'source', id, name: activation?.id === id ? activation.name : id, badge: activation?.id === id ? null : 'inactive' });
    const rank = (b: BuiltinSource) => (b.eligibility === 'review' ? 1 : 0);
    const extra = providers === null || builtins.isError ? [] : (builtins.data ?? [])
      .filter((b) => !b.enabled && !enabledBuiltins.includes(b.id) && providers.includes(b.id)).sort((a, b) => rank(a) - rank(b));
    return [...rows, ...extra.map((builtin) => ({ kind: 'builtin' as const, builtin }))];
    // eslint-disable-next-line react-hooks/exhaustive-deps -- `allowed` derives from `providers` only.
  }, [sources.data, builtins.data, builtins.isError, value, providers, enabledBuiltins, activation]);

  // Polls the freshly enabled source until active; failure drops the selection and offers a retry.
  useEffect(() => {
    if (!activation || activation.phase !== 'waiting') return undefined;
    const ctl = new AbortController();
    const { id } = activation;
    const fail = (code: string | null) => { onChange(valueRef.current.filter((item) => item !== id)); setActivation((current) => ({ id, name: current?.name ?? id, phase: 'failed', code })); };
    waitForActivation(id, ctl.signal).then((result) => {
      if (!result || ctl.signal.aborted) return;
      if (result.ok) { setActivation(null); void client.invalidateQueries({ queryKey: dashboardKeys.sources }); } else fail(result.code);
    }).catch(() => { if (!ctl.signal.aborted) fail('request_failed'); });
    return () => ctl.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps -- re-run only when a new activation attempt starts.
  }, [activation]);
  useEffect(() => { onActivatingChange?.(activation?.phase === 'waiting'); }, [activation, onActivatingChange]);

  /** Selects the source just created by the enable dialog and starts waiting for activation. */
  function handleEnabled(sourceId: string) {
    const name = enabling?.label ?? sourceId;
    if (enabling) setEnabledBuiltins((current) => [...current, enabling.id]);
    setEnabling(null);
    onChange(mode === 'single' ? [sourceId] : [...value.filter((item) => item !== sourceId), sourceId].slice(0, MAX_SOURCES));
    setActivation({ id: sourceId, name, phase: 'waiting' });
    void client.invalidateQueries({ queryKey: connectorKeys.builtins });
    void client.invalidateQueries({ queryKey: dashboardKeys.sources });
  }

  /** Renders badges for one option. */
  const badges = (option: PickerOption): ReactNode => option.kind === 'builtin'
    ? <>
      <Badge variant="outline">{t('datasourceEnable')}</Badge>
      {option.builtin.eligibility === 'review' && <Badge variant="secondary">{t('datasourceOperatorApproval')}</Badge>}
    </>
    : option.badge && <Badge variant="destructive">{option.badge === 'incompatible' ? t('datasourceIncompatible') : t('datasourceInactive')}</Badge>;
  const labelOf = (option: PickerOption) => option.kind === 'builtin' ? option.builtin.label : option.name;
  const keyOf = (option: PickerOption) => option.kind === 'builtin' ? `builtin:${option.builtin.id}` : option.id;

  const body = mode === 'single'
    ? <RadioGroup value={value[0] ?? ''} aria-labelledby={legendId} aria-describedby={describedBy} aria-invalid={invalid} className="space-y-1"
      onValueChange={(next) => {
        const option = options.find((item) => keyOf(item) === next);
        if (option?.kind === 'builtin') setEnabling(option.builtin); else onChange([next]);
      }}>
      {options.map((option) => <label key={keyOf(option)} className="flex min-h-9 items-center gap-2 text-sm">
        <RadioGroupItem value={keyOf(option)} />
        <span className="flex flex-wrap items-center gap-2">{labelOf(option)}{badges(option)}</span>
      </label>)}
    </RadioGroup>
    : <div role="group" aria-labelledby={legendId} aria-describedby={describedBy} className="space-y-1">
      {options.map((option) => {
        const checked = option.kind === 'source' && value.includes(option.id);
        return <label key={keyOf(option)} className="flex min-h-9 items-center gap-2 text-sm">
          <Checkbox checked={checked} disabled={!checked && option.kind === 'source' && value.length >= MAX_SOURCES}
            onCheckedChange={(next) => {
              if (option.kind === 'builtin') setEnabling(option.builtin);
              else onChange(next === true ? [...value, option.id] : value.filter((item) => item !== option.id));
            }} />
          <span className="flex flex-wrap items-center gap-2">{labelOf(option)}{badges(option)}</span>
        </label>;
      })}
    </div>;

  return <div className="space-y-2">
    <div id={legendId} className="text-sm font-semibold">{t('pickDatasource')}</div>
    {mode === 'multi' && <p className="text-xs text-muted-foreground">{t('datasourceLimit')}</p>}
    {(sources.isPending || builtins.isPending) && <p role="status" className="text-xs text-muted-foreground">{t('loading')}</p>}
    {sources.isError && <p role="alert" className="text-xs text-destructive">{t('datasourceLoadFailed')}</p>}
    <div className="max-h-56 overflow-y-auto rounded-md border border-border p-2">
      {body}
      {sources.isSuccess && options.length === 0 && <p className="text-xs text-muted-foreground">{t('datasourceNoneAddInSources')}</p>}
    </div>
    {builtins.isError && <p className="text-xs text-muted-foreground">{t('datasourceBuiltinsUnavailable')}</p>}
    <Link href="/settings/sources" className="text-xs underline">{t('openSources')}</Link>
    {activation?.phase === 'waiting' && <p role="status" className="text-xs text-muted-foreground">{t('datasourceActivating')}</p>}
    {activation?.phase === 'failed' && <div role="alert" className="flex flex-wrap items-center gap-2 text-xs text-destructive">
      <span>{t('datasourceActivationFailed', { code: activation.code ?? 'unknown' })}</span>
      <Button type="button" size="sm" variant="outline" onClick={() => setActivation({ id: activation.id, name: activation.name, phase: 'waiting' })}>{t('retry')}</Button>
    </div>}
    {enabling && <BuiltinEnableDialog key={enabling.id} builtin={enabling} onEnabled={handleEnabled} onCancel={() => setEnabling(null)} />}
  </div>;
}

/**
 * Confirmation dialog that enables one built-in source from inside the gadget editor. Shows the shared terms and
 * declared-use fields, calls the enable endpoint and reports the created source id. Failures (including 403
 * operator_required or terms errors) are mapped to a message, shown in a role=alert line and keep the dialog open.
 */
export function BuiltinEnableDialog({ builtin, onEnabled, onCancel }: {
  builtin: BuiltinSource;
  onEnabled: (sourceId: string) => void;
  onCancel: () => void;
}) {
  const t = useTranslations('gadgetSettings');
  const tsrc = useTranslations('sources');
  const { csrfToken } = useWorkspaceSession();
  const [use, setUse] = useState<DeclaredUse>('personal');
  const [aiOptions, setAiOptions] = useState<SourceAiOptions>({ graph_enabled: true, rag_index_enabled: true });
  const [error, setError] = useState<string | null>(null);
  const enable = useMutation({
    mutationFn: () => enableBuiltinSource(builtin.id, use, csrfToken, undefined, aiOptions),
    onSuccess: (result) => { if (result.source_id) onEnabled(result.source_id); else setError(tsrc('builtinErrorGeneric')); },
    onError: (cause) => setError(tsrc(builtinEnableErrorKey(cause))),
  });
  return <AlertDialog open onOpenChange={(open) => { if (!open && !enable.isPending) onCancel(); }}>
    <AlertDialogContent className="max-h-[85vh] overflow-y-auto">
      <AlertDialogHeader>
        <AlertDialogTitle>{t('datasourceEnableTitle', { name: builtin.label })}</AlertDialogTitle>
        <AlertDialogDescription>{t('datasourceEnableHint')}</AlertDialogDescription>
      </AlertDialogHeader>
      <BuiltinTermsFields builtin={builtin} use={use} onUseChange={setUse} aiOptions={aiOptions} onAiOptionsChange={setAiOptions} error={error} />
      <AlertDialogFooter>
        <Button type="button" variant="outline" disabled={enable.isPending} onClick={onCancel}>{t('cancel')}</Button>
        <Button type="button" disabled={enable.isPending} onClick={() => { setError(null); enable.mutate(); }}>{enable.isPending ? t('datasourceEnabling') : t('datasourceEnableConfirm')}</Button>
      </AlertDialogFooter>
    </AlertDialogContent>
  </AlertDialog>;
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
}

/**
 * One editor for gadget definitions, used by the Settings library, the dashboard Add dialog and the per-gadget
 * Configure modal. New gadgets start from a labeled template (or Custom), then a datasource, a prefilled name and a
 * collapsed Advanced section (keywords, limit, per-renderer scope, highlight rules). One Save creates or patches the
 * definition; Save stays disabled until the name and (when the renderer reads sources) a datasource are valid and
 * no just-enabled source is still activating. Editing never blocks on a saved source that became incompatible.
 */
export function GadgetEditor({ definition = null, onSaved, onCancel, footerExtra, saveLabel }: GadgetEditorProps) {
  const t = useTranslations('gadgetSettings');
  const td = useTranslations('dashboard');
  const session = useWorkspaceSession();
  const client = useQueryClient();
  const nameId = useId();
  const limitId = useId();
  const renderers = useQuery({ queryKey: dashboardKeys.renderers, queryFn: ({ signal }) => listGadgetRenderers(signal) });
  const templates = useQuery({ queryKey: dashboardKeys.templates, queryFn: ({ signal }) => listGadgetTemplates(signal), enabled: !definition });
  const [choice, setChoice] = useState<string | null>(definition ? definition.renderer : null);
  const [renderer, setRenderer] = useState(definition?.renderer ?? '');
  const [template, setTemplate] = useState<GadgetTemplate | null>(null);
  const [name, setName] = useState(definition?.name ?? '');
  const [sourceIds, setSourceIds] = useState<string[]>(definition?.source_ids ?? []);
  const [keywords, setKeywords] = useState((definition?.filters?.keywords ?? []).join(', '));
  const [exclude, setExclude] = useState((definition?.filters?.exclude_keywords ?? []).join(', '));
  const [limit, setLimit] = useState(String(definition?.filters?.limit ?? 25));
  const [symbols, setSymbols] = useState((definition?.scope?.symbols ?? []).join(', '));
  const [metrics, setMetrics] = useState((definition?.scope?.metrics ?? []).join(', '));
  const [lookback, setLookback] = useState(String(definition?.scope?.lookback_days ?? (definition?.renderer === 'map' || definition?.renderer === 'intelligence_panel' ? 30 : 90)));
  const [regions, setRegions] = useState((definition?.scope?.regions ?? []).join(', '));
  const [channels, setChannels] = useState((definition?.scope?.channel_ids ?? []).join(', '));
  const [cii, setCii] = useState((definition?.scope?.cii_country_codes ?? []).join(', '));
  const [layers, setLayers] = useState<string[]>(definition?.scope?.map_layer_ids ?? []);
  const [engine, setEngine] = useState<'globe' | 'flat'>(definition?.filters?.map_engine ?? 'globe');
  const [precise, setPrecise] = useState(definition?.filters?.show_precise_locations ?? false);
  const [rules, setRules] = useState<HighlightRule[]>(definition?.highlight_rules ?? []);
  const [rulesTouched, setRulesTouched] = useState(false);
  const [activating, setActivating] = useState(false);
  const [touched, setTouched] = useState(false);
  // Latest saved revision so a retry after a failed follow-up step (for example a title patch) does not conflict.
  const [base, setBase] = useState<GadgetDefinition | null>(definition);

  const descriptor: GadgetRenderer | undefined = (renderers.data ?? []).find((item) => item.id === renderer);
  const sourceMode = descriptor?.source_mode ?? 'none';
  const providers = template && template.providers.length > 0 ? template.providers : (descriptor?.source_providers ?? null);
  // Existing gadgets with no source keep saving; new ones must pick a datasource when the renderer reads sources.
  const needsSource = sourceMode !== 'none' && (!definition || definition.source_ids.length > 0);
  const limitNumber = Number(limit);
  const limitValid = Number.isInteger(limitNumber) && limitNumber >= 1 && limitNumber <= 100;
  const nameValid = name.trim().length > 0;
  const sourceValid = !needsSource || sourceIds.length > 0;
  const keywordsValid = splitList(keywords).length <= 32 && splitList(exclude).length <= 32;
  const detailsRef = useRef<HTMLDetailsElement>(null);
  // Why Save is disabled; shown beside Save so a problem inside the collapsed Advanced section is never hidden.
  const blockReason = !nameValid ? t('nameRequired') : !sourceValid ? t('datasourceRequired') : !limitValid ? t('limitHelp') : !keywordsValid ? t('keywordsHelp') : null;
  // Open Advanced as soon as one of its fields is invalid; Save is disabled then, so no submit could do it.
  useEffect(() => { if ((!limitValid || !keywordsValid) && detailsRef.current) detailsRef.current.open = true; }, [limitValid, keywordsValid]);
  const valid = renderer !== '' && nameValid && sourceValid && limitValid && keywordsValid;

  /** Applies a template (or Custom) to the draft: renderer, prefilled name, scope and filters. */
  function choose(id: string) {
    setChoice(id);
    // Reset every draft field so nothing typed for a previous template leaks into the next one.
    setSourceIds([]);
    setKeywords('');
    setExclude('');
    setRegions('');
    setChannels('');
    setCii('');
    setLayers([]);
    setSymbols('');
    setMetrics('');
    setLookback('90');
    setLimit('25');
    setRules([]);
    setRulesTouched(false);
    if (id === CUSTOM) { setTemplate(null); setRenderer(''); setName(''); return; }
    const picked = templates.data?.find((item) => item.id === id);
    if (!picked) return;
    setTemplate(picked);
    setRenderer(picked.renderer);
    setName(t.has(`templates.${id}.name`) ? t(`templates.${id}.name` as 'name') : id);
    setLimit(String(picked.filters.limit ?? 25));
    setKeywords((picked.filters.keywords ?? []).join(', '));
    setExclude((picked.filters.exclude_keywords ?? []).join(', '));
    setRegions((picked.scope.regions ?? []).join(', '));
    setChannels((picked.scope.channel_ids ?? []).join(', '));
    setCii((picked.scope.cii_country_codes ?? []).join(', '));
    setLayers(picked.scope.map_layer_ids ?? []);
    setMetrics((picked.scope.metrics ?? []).join(', '));
    setSymbols((picked.scope.symbols ?? []).join(', '));
    setLookback(String(picked.scope.lookback_days ?? (picked.renderer === 'map' || picked.renderer === 'intelligence_panel' ? 30 : 90)));
  }

  const save = useMutation({
    mutationFn: async () => {
      const filters = {
        limit: limitNumber,
        keywords: splitList(keywords).length > 0 ? [...new Set(splitList(keywords))] : undefined,
        exclude_keywords: splitList(exclude).length > 0 ? [...new Set(splitList(exclude))] : undefined,
        ...(renderer === 'map' ? { map_engine: engine, show_precise_locations: precise } : {}),
      };
      const days = Number(lookback);
      const scope = {
        ...(base?.scope ?? template?.scope ?? {}),
        ...(renderer === 'finance_chart' ? { symbols: splitList(symbols, true), metrics: splitList(metrics).length > 0 ? splitList(metrics) : ['close'], lookback_days: days }
          : renderer === 'weather' || renderer === 'provider_observation' ? { metrics: splitList(metrics), lookback_days: days }
            : renderer === 'map' ? { regions: splitList(regions), map_layer_ids: layers, lookback_days: Math.min(30, days) }
              : renderer === 'intelligence_panel' ? { regions: splitList(regions), cii_country_codes: splitList(cii, true), lookback_days: Math.min(30, days) }
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

  const showTemplates = !definition && choice === null;
  const error = save.error ? t((apiFailureKey(save.error) === 'conflict' ? 'conflict' : 'saveFailed')) : null;
  const sourceErrorId = `${nameId}-source-error`;

  if (showTemplates || (!definition && choice === CUSTOM && renderer === '')) {
    return <div className="space-y-3">
      <h3 className="text-sm font-semibold">{t('pickTemplate')}</h3>
      {templates.isPending && <p role="status" className="text-xs text-muted-foreground">{t('loading')}</p>}
      {templates.isError && <p role="alert" className="text-xs text-destructive">{t('loadFailed')} <Button type="button" size="sm" variant="outline" onClick={() => templates.refetch()}>{t('retry')}</Button></p>}
      {choice === null && <ul className="grid gap-2 sm:grid-cols-2">
        {(templates.data ?? []).map((item) => <li key={item.id}>
          <button type="button" onClick={() => choose(item.id)} className="min-h-11 w-full rounded-lg border border-border p-3 text-left hover:border-primary focus-visible:outline-2">
            <span className="block text-sm font-semibold">{t.has(`templates.${item.id}.name`) ? t(`templates.${item.id}.name` as 'name') : item.id}</span>
            {t.has(`templates.${item.id}.description`) && <span className="block text-xs text-muted-foreground">{t(`templates.${item.id}.description` as 'name')}</span>}
          </button>
        </li>)}
        <li><button type="button" onClick={() => choose(CUSTOM)} className="min-h-11 w-full rounded-lg border border-dashed border-border p-3 text-left hover:border-primary focus-visible:outline-2">
          <span className="block text-sm font-semibold">{t('templateCustom')}</span>
          <span className="block text-xs text-muted-foreground">{t('templateCustomHint')}</span>
        </button></li>
      </ul>}
      {choice === CUSTOM && <div className="space-y-2">
        <Label htmlFor={`${nameId}-renderer`} className="text-xs font-semibold">{t('customRenderer')}</Label>
        <Select value={renderer} onValueChange={(next) => { setRenderer(next); setName(rendererName(t, next)); setSourceIds([]); }}>
          <SelectTrigger id={`${nameId}-renderer`} className="h-9"><SelectValue placeholder={t('customRenderer')} /></SelectTrigger>
          <SelectContent>{(renderers.data ?? []).filter((item) => item.runtime_state === 'available').map((item) => <SelectItem key={item.id} value={item.id}>{rendererName(t, item.id)}</SelectItem>)}</SelectContent>
        </Select>
        <Button type="button" variant="outline" size="sm" onClick={() => { setChoice(null); setRenderer(''); }}>{t('backToTemplates')}</Button>
      </div>}
      {onCancel && choice === null && <div className="flex justify-end"><Button type="button" variant="outline" onClick={onCancel}>{t('cancel')}</Button></div>}
    </div>;
  }

  return <form className="space-y-4" noValidate onSubmit={(event) => { event.preventDefault(); setTouched(true); if (valid && !activating) save.mutate(); }}>
    <fieldset disabled={save.isPending} className="m-0 min-w-0 space-y-4 border-0 p-0">
      {!definition && <div className="flex items-center gap-2 text-xs text-muted-foreground">
        <Check className="size-3.5" aria-hidden="true" />
        <span>{template ? (t.has(`templates.${template.id}.name`) ? t(`templates.${template.id}.name` as 'name') : template.id) : `${t('templateCustom')}: ${rendererName(t, renderer)}`}</span>
        <Button type="button" variant="ghost" size="sm" onClick={() => { setChoice(null); setRenderer(''); setTemplate(null); }}>{t('backToTemplates')}</Button>
      </div>}
      {sourceMode !== 'none' && <div>
        <DatasourcePicker providers={providers} mode={sourceMode === 'single' ? 'single' : 'multi'} value={sourceIds} onChange={setSourceIds}
          onActivatingChange={setActivating} invalid={!sourceValid} describedBy={!sourceValid ? sourceErrorId : undefined} />
        {!sourceValid && <p id={sourceErrorId} className="mt-1 text-xs text-muted-foreground">{t('datasourceRequired')}</p>}
      </div>}
      <div className="space-y-1.5">
        <Label htmlFor={nameId} className="text-xs font-semibold">{t('name')}</Label>
        <Input id={nameId} value={name} maxLength={120} className="h-9" aria-invalid={touched && !nameValid} aria-describedby={touched && !nameValid ? `${nameId}-error` : undefined} onBlur={() => setTouched(true)} onChange={(event) => { setTouched(true); setName(event.target.value); }} />
        {touched && !nameValid && <p id={`${nameId}-error`} role="alert" className="text-xs text-destructive">{t('nameRequired')}</p>}
      </div>
      <details ref={detailsRef} className="rounded-md border border-border p-3">
        <summary className="cursor-pointer text-sm font-semibold">{t('advanced')}</summary>
        <div className="mt-3 space-y-3">
          <div className="grid gap-3 sm:grid-cols-2">
            <div className="space-y-1.5"><Label htmlFor={`${nameId}-kw`} className="text-xs font-semibold">{t('keywords')}</Label><Input id={`${nameId}-kw`} value={keywords} onChange={(event) => setKeywords(event.target.value)} placeholder={t('keywordsPlaceholder')} className="h-8 text-xs" /></div>
            <div className="space-y-1.5"><Label htmlFor={`${nameId}-ex`} className="text-xs font-semibold">{t('excludeKeywords')}</Label><Input id={`${nameId}-ex`} value={exclude} onChange={(event) => setExclude(event.target.value)} placeholder={t('excludePlaceholder')} className="h-8 text-xs" /></div>
          </div>
          <div className="space-y-1.5">
            <Label htmlFor={limitId} className="text-xs font-semibold">{t('limit')}</Label>
            <Input id={limitId} type="number" min={1} max={100} value={limit} aria-invalid={!limitValid} aria-describedby={`${limitId}-help`} onChange={(event) => setLimit(event.target.value)} className="h-8 w-28 font-mono text-xs" />
            <p id={`${limitId}-help`} className={limitValid ? 'text-xs text-muted-foreground' : 'text-xs text-destructive'}>{t('limitHelp')}</p>
          </div>
          {renderer === 'finance_chart' && <div className="space-y-1.5"><Label htmlFor={`${nameId}-sym`} className="text-xs font-semibold">{t('symbols')}</Label><Input id={`${nameId}-sym`} value={symbols} onChange={(event) => setSymbols(event.target.value)} placeholder="NVDA, TSLA, BTC" className="h-8 font-mono text-xs uppercase" /></div>}
          {(renderer === 'finance_chart' || renderer === 'weather' || renderer === 'provider_observation') && <div className="grid gap-3 sm:grid-cols-2">
            <div className="space-y-1.5"><Label htmlFor={`${nameId}-met`} className="text-xs font-semibold">{td('observationMetrics')}</Label><Input id={`${nameId}-met`} value={metrics} onChange={(event) => setMetrics(event.target.value)} placeholder={renderer === 'finance_chart' ? 'close, volume' : renderer === 'provider_observation' ? td('providerObsMetricsPlaceholder') : 'temperature_2m, precipitation'} className="h-8 font-mono text-xs" /></div>
            <div className="space-y-1.5"><Label htmlFor={`${nameId}-look`} className="text-xs font-semibold">{td('observationLookback')}</Label><Input id={`${nameId}-look`} type="number" min={1} max={366} value={lookback} onChange={(event) => setLookback(event.target.value)} className="h-8 font-mono text-xs" /></div>
          </div>}
          {(renderer === 'map' || renderer === 'intelligence_panel') && <section className="space-y-3 rounded-md border border-border p-3">
            <h4 className="text-sm font-semibold">{renderer === 'map' ? td('mapScopeTitle') : td('intelligenceScopeTitle')}</h4>
            <div className="space-y-1.5"><Label htmlFor={`${nameId}-reg`} className="text-xs font-semibold">{td('mapRegionsLabel')}</Label><Input id={`${nameId}-reg`} value={regions} onChange={(event) => setRegions(event.target.value)} placeholder={td('mapRegionsPlaceholder')} className="h-8 font-mono text-xs" /><p className="text-xs text-muted-foreground">{td('mapRegionsHelp')}</p></div>
            <div className="space-y-1.5"><Label htmlFor={`${nameId}-mlook`} className="text-xs font-semibold">{td('mapLookbackLabel')}</Label><Input id={`${nameId}-mlook`} type="number" min={1} max={30} value={lookback} onChange={(event) => setLookback(event.target.value)} className="h-8 font-mono text-xs" /></div>
            {renderer === 'map' && <>
              <div className="space-y-1.5">
                <Label htmlFor={`${nameId}-eng`} className="text-xs font-semibold">{td('mapEngineLabel')}</Label>
                <Select value={engine} onValueChange={(next) => setEngine(next as 'globe' | 'flat')}>
                  <SelectTrigger id={`${nameId}-eng`} className="h-9"><SelectValue /></SelectTrigger>
                  <SelectContent><SelectItem value="globe">{td('mapGlobeEngine')}</SelectItem><SelectItem value="flat">{td('mapFlatEngine')}</SelectItem></SelectContent>
                </Select>
              </div>
              <fieldset className="space-y-2">
                <legend className="text-xs font-semibold">{td('mapLayerLabel')}</legend>
                {MAP_LAYERS.map((layerId) => <label key={layerId} className="flex items-start gap-2 text-xs">
                  <Checkbox checked={layers.includes(layerId)} onCheckedChange={(checked) => setLayers((current) => checked === true ? [...current, layerId] : current.filter((item) => item !== layerId))} />
                  <span>{td(`mapLayer_${layerId}`)}{layerId !== 'world_observations' && <span className="block text-muted-foreground">{td('mapLayerUnavailable')}</span>}</span>
                </label>)}
              </fieldset>
              <label className="flex items-start gap-2 text-xs"><Checkbox checked={precise} onCheckedChange={(checked) => setPrecise(checked === true)} /><span>{td('mapPreciseLocationOptIn')}</span></label>
            </>}
            {renderer === 'intelligence_panel' && <div className="space-y-1.5"><Label htmlFor={`${nameId}-cii`} className="text-xs font-semibold">{td('ciiCountryScopeLabel')}</Label><Input id={`${nameId}-cii`} value={cii} onChange={(event) => setCii(event.target.value)} placeholder="US, VN" className="h-8 font-mono text-xs uppercase" /><p className="text-xs text-muted-foreground">{td('ciiCountryScopeHelp')}</p></div>}
          </section>}
          {renderer === 'telegram_feed' && <div className="space-y-1.5"><Label htmlFor={`${nameId}-tg`} className="text-xs font-semibold">{td('telegramChannels')}</Label><Input id={`${nameId}-tg`} value={channels} onChange={(event) => setChannels(event.target.value)} placeholder="-1001234567890, 123456789" className="h-8 font-mono text-xs" /></div>}
          <HighlightRulesField rules={rules} onChange={(next) => { setRules(next); setRulesTouched(true); }} />
        </div>
      </details>
    </fieldset>
    {error && <p role="alert" className="text-xs text-destructive">{error}</p>}
    {activating && <p role="status" className="text-xs text-muted-foreground">{t('datasourceActivating')}</p>}
    {blockReason && <p id={`${nameId}-block`} role="status" className="text-xs text-muted-foreground">{blockReason}</p>}
    <div className="flex flex-wrap items-center justify-end gap-2">
      {footerExtra && <div className="me-auto">{footerExtra}</div>}
      {onCancel && <Button type="button" variant="outline" disabled={save.isPending} onClick={onCancel}>{t('cancel')}</Button>}
      <Button type="submit" disabled={save.isPending || activating || !valid} aria-describedby={blockReason ? `${nameId}-block` : undefined}>{save.isPending ? t('saving') : (saveLabel ?? t('save'))}</Button>
    </div>
  </form>;
}
