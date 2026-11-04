'use client';

import { useInfiniteQuery } from '@tanstack/react-query';
import { Bell, Plus, Trash2 } from 'lucide-react';
import Link from 'next/link';
import { useTranslations } from 'next-intl';
import { useCallback, useEffect, useId, useMemo, useState } from 'react';
import { BareButton } from '@/components/bare-button';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { RadioGroup, RadioGroupItem } from '@/components/ui/radio-group';
import { SearchableSelect } from '@/components/ui/searchable-select';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import type { DataShape } from '@/core/data-shapes';
import { formatDateTime } from '@/core/i18n';
import { useDisplayPreferences } from '@/core/query-provider';
import { TEMPLATE_KIND_BY_RENDERER } from '@/core/renderer-labels';
import { uuid7 } from '@/core/uuid7';
import type { CoinGeckoCoin } from '@/modules/sources';
import { dashboardKeys, listGadgetSources, type HighlightRule, type ProviderMetrics } from './api';
import { MAX_SOURCES, splitList } from './gadget-editor-model';

/** Resolves the short localized description shown beside a renderer's SVG preview. */
export function templateDescription(renderer: string, t: ReturnType<typeof useTranslations>): string {
  const kind = TEMPLATE_KIND_BY_RENDERER[renderer];
  return kind && t.has(`templateTypes.${kind}.description`) ? t(`templateTypes.${kind}.description` as 'name') : '';
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
 * Selects only active, workspace-configured sources accepted by this renderer. Previously saved inactive or
 * incompatible IDs remain visible so owners can remove them; adding a gadget never creates or activates a source.
 *
 * @param acceptedShapes Data shapes the renderer reads; null means any shape.
 * @param mode Selection cardinality from the renderer or template.
 * @param value Currently selected source ids.
 * @param onChange Receives the next selection.
 * @param max Multi-mode selection ceiling.
 */
export function DatasourcePicker({ acceptedShapes, mode, value, onChange, invalid, describedBy, sourcesHref = '/settings/sources', max = MAX_SOURCES }: {
  acceptedShapes: readonly DataShape[] | null;
  mode: 'single' | 'multi';
  value: string[];
  onChange: (sourceIds: string[]) => void;
  invalid?: boolean;
  describedBy?: string;
  /** Settings > Sources URL for the "Open Sources" link; callers pass the renderer-filtered one. */
  sourcesHref?: string;
  /** Selection ceiling for multi mode (the renderer's `max_sources`); defaults to the generic selector ceiling. */
  max?: number;
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
          <Checkbox checked={checked} disabled={!checked && value.length >= max}
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
export function MetricPicker({ catalog, single, value, onChange, id, labelId }: { catalog: ProviderMetrics; single: boolean; value: string[]; onChange: (next: string[]) => void; id: string; labelId: string }) {
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
export function SymbolPicker({ suggested, value, onChange, labelId }: { suggested: string[]; value: string[]; onChange: (next: string[]) => void; labelId: string }) {
  const t = useTranslations('gadgetSettings');
  return <div className="space-y-2">
    {value.length > 0 && <ul className="flex flex-wrap gap-1.5">{value.map((symbol) => <li key={symbol}>
      <Badge variant="outline" className="gap-1 font-mono">{symbol}
        <BareButton type="button" aria-label={`${t('remove')} ${symbol}`} onClick={() => onChange(value.filter((item) => item !== symbol))}><Trash2 className="size-3" aria-hidden="true" /></BareButton>
      </Badge></li>)}</ul>}
    <SearchableSelect value="" options={suggested.filter((item) => !value.includes(item))} allowCustom aria-labelledby={labelId} placeholder={t('symbolPlaceholder')}
      searchLabel={t('symbolSearch')} emptyLabel={t('symbolEmpty')}
      onValueChange={(next) => { const symbol = next.trim().toUpperCase(); if (symbol && !value.includes(symbol)) onChange([...value, symbol]); }} />
  </div>;
}

/** Picks the stable CoinGecko ID represented by each collected series and labels duplicate tickers explicitly. */
export function CoinGeckoSymbolPicker({ assets, value, onChange, labelId }: { assets: CoinGeckoCoin[]; value: string[]; onChange: (next: string[]) => void; labelId: string }) {
  return <ul className="grid max-h-48 gap-1 overflow-y-auto" aria-labelledby={labelId}>
    {assets.map((coin) => {
      const symbol = coin.series_symbol ?? coin.id.toUpperCase();
      const selected = value.includes(symbol);
      return <li key={coin.id}><Button type="button" variant={selected ? 'secondary' : 'outline'} className="h-auto min-h-9 w-full justify-start whitespace-normal text-left pointer-coarse:min-h-11" aria-pressed={selected} onClick={() => onChange(selected ? value.filter((item) => item !== symbol) : [...value, symbol])}>
        {coin.name} ({coin.symbol}) · {coin.id}{selected ? ' ✓' : ''}
      </Button></li>;
    })}
  </ul>;
}

/**
 * Highlight-rule subform: lists the draft rules and adds keyword rules with a severity and notify flag.
 * Rules are plain local state; the editor only persists them when this list was touched.
 */
export function HighlightRulesField({ rules, onChange }: { rules: HighlightRule[]; onChange: (rules: HighlightRule[]) => void }) {
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
      <span className="text-xs font-semibold">{ts('gsRules', { count: rules.length })}</span>
      <span className="font-mono text-[11px] text-muted-foreground">{ts('gsRulesHint')}</span>
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

