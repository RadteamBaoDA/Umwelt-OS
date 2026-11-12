'use client';

import Link from 'next/link';
import { useTranslations } from 'next-intl';
import React from 'react';
import { BareButton } from '@/components/bare-button';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { SearchableSelect } from '@/components/ui/searchable-select';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { templateName } from '@/core/renderer-labels';
import type { CoinGeckoCoin } from '@/modules/sources';
import type { GadgetRenderer, GadgetTemplate, ProviderMetrics } from './api';
import { providerMeta } from './live-feed/sources';
import {
  defaultMetric,
  LIVE_PROVIDERS,
  MAP_LAYERS,
  splitList,
  type FieldKey,
  type GadgetDraft,
  type WeatherLayer,
} from './gadget-editor-model';
import { CoinGeckoSymbolPicker, HighlightRulesField, MetricPicker, SymbolPicker, templateDescription } from './gadget-editor-pickers';
import { GadgetTemplateSample } from './gadget-template-sample';
import { LiveMapConfig } from './live-map-config';
import { sourcesLinkFor } from './sources-link';

// The datasource picker lives in gadget-editor.tsx (public export); sections receive it as a render prop to avoid a cycle.

/** Draft access shared by every form section. */
export interface DraftAccess {
  draft: GadgetDraft;
  /** Sets one draft field (value or functional update). */
  set: <K extends keyof GadgetDraft>(key: K, value: GadgetDraft[K] | ((current: GadgetDraft[K]) => GadgetDraft[K])) => void;
}

/** Facts derived from the draft that sections render from. */
export interface SectionFacts {
  fields: ReadonlySet<FieldKey>;
  /** Merged catalog entry of the bound providers. */
  entry: ProviderMetrics | undefined;
  chartMulti: boolean;
  metricGroup: boolean;
  showSymbols: boolean;
  showSymbolsFallback: boolean;
  showRegion: boolean;
  coinAssets: CoinGeckoCoin[] | null;
  limitValid: boolean;
  lookbackValid: boolean;
  /** Localized server error for one field name, or null. */
  fieldError: (field: string) => string | null;
  /** Base DOM id from which every field id is derived. */
  nameId: string;
  limitId: string;
}

/** Props of {@link TemplateChooser}. */
interface TemplateChooserProps {
  renderers: { isPending: boolean; isError: boolean; isSuccess: boolean; data: GadgetRenderer[] | undefined; refetch: () => unknown };
  templates: { isPending: boolean; isError: boolean; isSuccess: boolean; data: GadgetTemplate[] | undefined; refetch: () => unknown };
  counts: { isError: boolean; data: Record<string, number> | undefined; refetch: () => unknown };
  onChoose: (renderer: string) => void;
  onCancel?: () => void;
}

/** One selectable template tile; a zero compatible-source count routes to the filtered Sources page instead. */
function TemplateTile({
  item,
  info,
  count,
  onChoose,
}: {
  item: GadgetTemplate;
  info: GadgetRenderer;
  count: number | null;
  onChoose: (renderer: string) => void;
}) {
  const t = useTranslations('gadgetSettings');
  const body = (
    <>
      <GadgetTemplateSample renderer={item.renderer} />
      <span className="min-w-0">
        <span className="block text-sm font-semibold">{templateName(item.renderer, t)}</span>
        <span className="block text-xs text-muted-foreground">{templateDescription(item.renderer, t)}</span>
        {count !== null && (
          <span className="block text-xs text-muted-foreground">
            {count === 0 ? t('addCompatibleSource') : t('compatibleCount', { count })}
          </span>
        )}
      </span>
    </>
  );
  const tileClass =
    'flex min-h-9 pointer-coarse:min-h-11 w-full items-center gap-3 rounded-lg border border-border p-3 text-left hover:border-primary focus-visible:outline-2';
  return (
    <li>
      {count === 0 ? (
        <Link href={sourcesLinkFor(info)} className={tileClass}>
          {body}
        </Link>
      ) : (
        <BareButton type="button" onClick={() => onChoose(item.renderer)} className={tileClass}>
          {body}
        </BareButton>
      )}
    </li>
  );
}

/** Template grid shown before a renderer is chosen for a new gadget. */
export function TemplateChooser({ renderers, templates, counts, onChoose, onCancel }: TemplateChooserProps) {
  const t = useTranslations('gadgetSettings');
  return (
    <div className="space-y-3">
      <h3 className="text-sm font-semibold">{t('pickTemplate')}</h3>
      {(renderers.isPending || templates.isPending) && (
        <p role="status" className="text-xs text-muted-foreground">
          {t('loading')}
        </p>
      )}
      {(renderers.isError || templates.isError) && (
        <p role="alert" className="text-xs text-destructive">
          {t('loadFailed')}{' '}
          <Button
            type="button"
            size="sm"
            variant="outline"
            onClick={() => {
              void renderers.refetch();
              void templates.refetch();
            }}
          >
            {t('retry')}
          </Button>
        </p>
      )}
      {counts.isError && (
        <p role="status" className="text-xs text-muted-foreground">
          {t('compatibleCountsFailed')}{' '}
          <Button
            type="button"
            size="sm"
            variant="outline"
            onClick={() => {
              void counts.refetch();
            }}
          >
            {t('retry')}
          </Button>
        </p>
      )}
      {renderers.isSuccess && templates.isSuccess && (
        <ul className="grid gap-3 sm:grid-cols-2">
          {templates.data!.flatMap((item) => {
            const info = renderers.data!.find(
              (rendererInfo) => rendererInfo.id === item.renderer && rendererInfo.runtime_state === 'available',
            );
            if (!info) return [];
            const count = info.source_mode === 'none' || item.renderer === 'map' ? null : (counts.data?.[item.renderer] ?? null);
            return [<TemplateTile key={item.id} item={item} info={info} count={count} onChoose={onChoose} />];
          })}
        </ul>
      )}
      {onCancel && (
        <div className="flex justify-end">
          <Button type="button" variant="outline" onClick={onCancel}>
            {t('cancel')}
          </Button>
        </div>
      )}
    </div>
  );
}

/** Metrics control: provider-driven picker when the catalog lists metrics, otherwise free text. */
export function MetricsField({ draft, set, facts }: DraftAccess & { facts: SectionFacts }) {
  const t = useTranslations('gadgetSettings');
  const td = useTranslations('dashboard');
  const { entry, metricGroup, nameId, fieldError } = facts;
  const { renderer, metrics } = draft;
  const chartSingle = renderer === 'finance_chart' && !facts.chartMulti;
  return (
    <div data-field="metrics" className="space-y-1.5">
      {/* A checkbox group has no single control to point `for` at, so it is named through aria-labelledby instead. */}
      <Label id={`${nameId}-met-label`} htmlFor={metricGroup ? undefined : `${nameId}-met`} className="text-xs font-semibold">
        {t('metricLabel')}
      </Label>
      {entry && entry.metrics.length > 0 ? (
        <MetricPicker
          id={`${nameId}-met`}
          labelId={`${nameId}-met-label`}
          catalog={entry}
          single={chartSingle}
          value={chartSingle && splitList(metrics).length === 0 ? [defaultMetric(entry)] : splitList(metrics)}
          onChange={(next) => set('metrics', next.join(', '))}
        />
      ) : (
        <Input
          id={`${nameId}-met`}
          value={metrics}
          onChange={(event) => set('metrics', event.target.value)}
          placeholder={
            renderer === 'finance_chart'
              ? 'close, volume'
              : renderer === 'provider_observation'
                ? td('providerObsMetricsPlaceholder')
                : 'temperature_2m, precipitation'
          }
          className="h-8 font-mono text-xs"
        />
      )}
      {fieldError('metrics') && (
        <p role="alert" className="text-xs text-destructive">
          {fieldError('metrics')}
        </p>
      )}
    </div>
  );
}

/** Symbol controls: provider pickers for symbol-keyed providers, free text when the provider is unknown. */
export function SymbolFields({ draft, set, facts }: DraftAccess & { facts: SectionFacts }) {
  const t = useTranslations('gadgetSettings');
  const { entry, coinAssets, nameId, fieldError } = facts;
  const symbols = draft.symbols;
  return (
    <>
      {facts.showSymbols && entry && (
        <div data-field="symbols" className="space-y-1.5">
          <Label id={`${nameId}-sym-label`} className="text-xs font-semibold">
            {t('symbolsLabel')}
          </Label>
          {coinAssets ? (
            <CoinGeckoSymbolPicker
              labelId={`${nameId}-sym-label`}
              assets={coinAssets}
              value={splitList(symbols, true)}
              onChange={(next) => set('symbols', next.join(', '))}
            />
          ) : (
            <SymbolPicker
              labelId={`${nameId}-sym-label`}
              suggested={entry.suggested_symbols}
              value={splitList(symbols, true)}
              onChange={(next) => set('symbols', next.join(', '))}
            />
          )}
          {!entry.seed_symbol && <p className="text-xs text-muted-foreground">{t('symbolsOwnerHint')}</p>}
          {fieldError('symbols') && (
            <p role="alert" className="text-xs text-destructive">
              {fieldError('symbols')}
            </p>
          )}
        </div>
      )}
      {facts.showSymbolsFallback && (
        <div data-field="symbols" className="space-y-1.5">
          <Label htmlFor={`${nameId}-sym`} className="text-xs font-semibold">
            {t('symbolsLabel')}
          </Label>
          <Input
            id={`${nameId}-sym`}
            value={symbols}
            onChange={(event) => set('symbols', event.target.value)}
            aria-describedby={`${nameId}-sym-help`}
            className="h-8 font-mono text-xs"
          />
          <p id={`${nameId}-sym-help`} className="text-xs text-muted-foreground">
            {t('symbolsUnknownProviderHint')}
          </p>
          {fieldError('symbols') && (
            <p role="alert" className="text-xs text-destructive">
              {fieldError('symbols')}
            </p>
          )}
        </div>
      )}
    </>
  );
}

/** Region picker for region-keyed providers (one region per chart). */
export function RegionField({ draft, set, facts }: DraftAccess & { facts: SectionFacts }) {
  const t = useTranslations('gadgetSettings');
  const { entry, nameId, fieldError } = facts;
  if (!facts.showRegion || !entry) return null;
  return (
    <div data-field="regions" className="space-y-1.5">
      <Label id={`${nameId}-reg-label`} className="text-xs font-semibold">
        {t('regionLabel')}
      </Label>
      <SearchableSelect
        value={draft.region}
        options={entry.suggested_regions}
        allowCustom
        aria-labelledby={`${nameId}-reg-label`}
        aria-describedby={`${nameId}-reg-help`}
        placeholder={t('regionPlaceholder')}
        searchLabel={t('regionSearch')}
        emptyLabel={t('regionEmpty')}
        onValueChange={(next) => set('region', next.trim())}
      />
      <p id={`${nameId}-reg-help`} className="text-xs text-muted-foreground">
        {t('regionHelp')}
      </p>
      {fieldError('regions') && (
        <p role="alert" className="text-xs text-destructive">
          {fieldError('regions')}
        </p>
      )}
    </div>
  );
}

/** Keyword include / exclude inputs (list renderers). */
function KeywordFields({ draft, set, nameId }: DraftAccess & { nameId: string }) {
  const t = useTranslations('gadgetSettings');
  return (
    <div className="grid gap-3 sm:grid-cols-2">
      <div className="space-y-1.5">
        <Label htmlFor={`${nameId}-kw`} className="text-xs font-semibold">
          {t('keywords')}
        </Label>
        <Input
          id={`${nameId}-kw`}
          value={draft.keywords}
          onChange={(event) => set('keywords', event.target.value)}
          placeholder={t('keywordsPlaceholder')}
          className="h-8 text-xs"
        />
      </div>
      <div className="space-y-1.5">
        <Label htmlFor={`${nameId}-ex`} className="text-xs font-semibold">
          {t('excludeKeywords')}
        </Label>
        <Input
          id={`${nameId}-ex`}
          value={draft.exclude}
          onChange={(event) => set('exclude', event.target.value)}
          placeholder={t('excludePlaceholder')}
          className="h-8 text-xs"
        />
      </div>
    </div>
  );
}

/** Item limit and observation lookback inputs with their validation help. */
function LimitAndLookbackFields({ draft, set, facts }: DraftAccess & { facts: SectionFacts }) {
  const t = useTranslations('gadgetSettings');
  const td = useTranslations('dashboard');
  const { fields, limitId, limitValid, lookbackValid, nameId, fieldError } = facts;
  return (
    <>
      {fields.has('limit') && (
        <div className="space-y-1.5">
          <Label htmlFor={limitId} className="text-xs font-semibold">
            {t('limit')}
          </Label>
          <Input
            id={limitId}
            type="number"
            min={1}
            max={100}
            value={draft.limit}
            aria-invalid={!limitValid}
            aria-describedby={`${limitId}-help`}
            onChange={(event) => set('limit', event.target.value)}
            className="h-8 w-28 font-mono text-xs"
          />
          <p id={`${limitId}-help`} className={limitValid ? 'text-xs text-muted-foreground' : 'text-xs text-destructive'}>
            {t('limitHelp')}
          </p>
        </div>
      )}
      {fields.has('lookback') && (
        <div className="space-y-1.5">
          <Label htmlFor={`${nameId}-look`} className="text-xs font-semibold">
            {td('observationLookback')}
          </Label>
          <Input
            id={`${nameId}-look`}
            type="number"
            min={1}
            max={3660}
            value={draft.lookback}
            aria-invalid={!lookbackValid || fieldError('lookback_days') !== null}
            aria-describedby={lookbackValid ? undefined : `${nameId}-look-help`}
            onChange={(event) => {
              set('lookback', event.target.value);
              set('lookbackTouched', true);
            }}
            className="h-8 w-28 font-mono text-xs"
          />
          {!lookbackValid && (
            <p id={`${nameId}-look-help`} className="text-xs text-destructive">
              {t('lookbackHelp')}
            </p>
          )}
          {fieldError('lookback_days') && (
            <p role="alert" data-field="lookback_days" className="text-xs text-destructive">
              {fieldError('lookback_days')}
            </p>
          )}
        </div>
      )}
    </>
  );
}

/** Live-map options: provider toggles, free-text scope fields and the weather overlay. */
function LiveMapOptions({ draft, set, nameId }: DraftAccess & { nameId: string }) {
  const td = useTranslations('dashboard');
  const textFields = [
    [td('liveMapCountry'), draft.country, 'country'],
    [td('liveMapGithubOwner'), draft.githubOwner, 'githubOwner'],
    [td('liveMapGithubRepo'), draft.githubRepo, 'githubRepo'],
    [td('liveMapSecCik'), draft.secCik, 'secCik'],
  ] as const;
  return (
    <>
      <LiveMapConfig />
      <fieldset className="space-y-2">
        <legend className="text-xs font-semibold">{td('liveMapProviders')}</legend>
        {LIVE_PROVIDERS.map((provider) => (
          <label key={provider} className="flex items-center gap-2 text-xs">
            <Checkbox
              checked={draft.liveProviders.includes(provider)}
              onCheckedChange={(checked) =>
                set('liveProviders', (current) => (checked === true ? [...current, provider] : current.filter((item) => item !== provider)))
              }
            />
            {providerMeta(provider).name}
          </label>
        ))}
      </fieldset>
      <div className="grid gap-2 sm:grid-cols-2">
        {textFields.map(([label, value, key]) => (
          <label key={label} className="space-y-1 text-xs">
            <span>{label}</span>
            <Input value={value} onChange={(event) => set(key, event.target.value)} />
          </label>
        ))}
      </div>
      <div className="space-y-1.5">
        <Label htmlFor={`${nameId}-weather`}>{td('liveMapWeather')}</Label>
        <Select value={draft.weatherLayer} onValueChange={(value) => set('weatherLayer', value as WeatherLayer)}>
          <SelectTrigger id={`${nameId}-weather`}>
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {(['none', 'temperature', 'precipitation', 'wind', 'clouds'] as const).map((item) => (
              <SelectItem key={item} value={item}>
                {td(`liveMapWeather_${item}`)}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>
    </>
  );
}

/** Selected-sources map options: layer checkboxes and the precise-location opt-in. */
function SelectedSourcesMapOptions({ draft, set, facts }: DraftAccess & { facts: SectionFacts }) {
  const td = useTranslations('dashboard');
  const { fieldError } = facts;
  return (
    <>
      <fieldset data-field="map_layer_ids" className="space-y-2">
        <legend className="text-xs font-semibold">{td('mapLayerLabel')}</legend>
        {MAP_LAYERS.map((layerId) => (
          <label key={layerId} className="flex items-start gap-2 text-xs">
            <Checkbox
              checked={draft.layers.includes(layerId)}
              disabled={layerId !== 'world_observations' && !draft.layers.includes(layerId)}
              onCheckedChange={(checked) =>
                set('layers', (current) => (checked === true ? [...current, layerId] : current.filter((item) => item !== layerId)))
              }
            />
            <span>
              {td(`mapLayer_${layerId}`)}
              {layerId !== 'world_observations' && <span className="block text-muted-foreground">{td('mapLayerUnavailable')}</span>}
            </span>
          </label>
        ))}
        {fieldError('map_layer_ids') && (
          <p role="alert" className="text-xs text-destructive">
            {fieldError('map_layer_ids')}
          </p>
        )}
      </fieldset>
      <label className="flex items-start gap-2 text-xs">
        <Checkbox checked={draft.precise} onCheckedChange={(checked) => set('precise', checked === true)} />
        <span>{td('mapPreciseLocationOptIn')}</span>
      </label>
    </>
  );
}

/** Map engine select plus the live / selected-sources specific options. */
function MapOptions({ draft, set, facts }: DraftAccess & { facts: SectionFacts }) {
  const td = useTranslations('dashboard');
  const { nameId } = facts;
  return (
    <>
      <div className="space-y-1.5">
        <Label htmlFor={`${nameId}-eng`} className="text-xs font-semibold">
          {td('mapEngineLabel')}
        </Label>
        <Select value={draft.engine} onValueChange={(next) => set('engine', next as 'globe' | 'flat')}>
          <SelectTrigger id={`${nameId}-eng`} className="h-9">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="globe">{td('mapGlobeEngine')}</SelectItem>
            <SelectItem value="flat">{td('mapFlatEngine')}</SelectItem>
          </SelectContent>
        </Select>
      </div>
      {draft.mapMode === 'live' && <LiveMapOptions draft={draft} set={set} nameId={nameId} />}
      {draft.mapMode === 'selected_sources' && <SelectedSourcesMapOptions draft={draft} set={set} facts={facts} />}
    </>
  );
}

/** Scope section of map and intelligence renderers (regions, lookback, map options, country codes). */
function ScopeSection({ draft, set, facts }: DraftAccess & { facts: SectionFacts }) {
  const td = useTranslations('dashboard');
  const { fields, nameId } = facts;
  const { renderer, mapMode } = draft;
  return (
    <section className="space-y-3 rounded-md border border-border p-3">
      <h4 className="text-sm font-semibold">{renderer === 'map' ? td('mapScopeTitle') : td('intelligenceScopeTitle')}</h4>
      {(renderer !== 'map' || mapMode === 'selected_sources') && (
        <>
          <div className="space-y-1.5">
            <Label htmlFor={`${nameId}-reg`} className="text-xs font-semibold">
              {td('mapRegionsLabel')}
            </Label>
            <Input
              id={`${nameId}-reg`}
              value={draft.regions}
              onChange={(event) => set('regions', event.target.value)}
              placeholder={td('mapRegionsPlaceholder')}
              className="h-8 font-mono text-xs"
            />
            <p className="text-xs text-muted-foreground">{td('mapRegionsHelp')}</p>
          </div>
          <div className="space-y-1.5">
            <Label htmlFor={`${nameId}-mlook`} className="text-xs font-semibold">
              {td('mapLookbackLabel')}
            </Label>
            <Input
              id={`${nameId}-mlook`}
              type="number"
              min={1}
              max={30}
              value={draft.lookback}
              onChange={(event) => set('lookback', event.target.value)}
              className="h-8 font-mono text-xs"
            />
          </div>
        </>
      )}
      {fields.has('map') && <MapOptions draft={draft} set={set} facts={facts} />}
      {fields.has('cii') && (
        <div className="space-y-1.5">
          <Label htmlFor={`${nameId}-cii`} className="text-xs font-semibold">
            {td('ciiCountryScopeLabel')}
          </Label>
          <Input
            id={`${nameId}-cii`}
            value={draft.cii}
            onChange={(event) => set('cii', event.target.value)}
            placeholder="US, VN"
            className="h-8 font-mono text-xs uppercase"
          />
          <p className="text-xs text-muted-foreground">{td('ciiCountryScopeHelp')}</p>
        </div>
      )}
    </section>
  );
}

/** Collapsible Advanced section; keeps the original field order (keywords, limit, lookback, scope, channels, rules). */
export const AdvancedSection = React.forwardRef<HTMLDetailsElement, DraftAccess & { facts: SectionFacts }>(function AdvancedSection(
  { draft, set, facts },
  ref,
) {
  const t = useTranslations('gadgetSettings');
  const td = useTranslations('dashboard');
  const { fields, nameId } = facts;
  return (
    <details ref={ref} className="rounded-md border border-border p-3">
      <summary className="cursor-pointer text-sm font-semibold">{t('advanced')}</summary>
      <div className="mt-3 space-y-3">
        {fields.has('keywords') && <KeywordFields draft={draft} set={set} nameId={nameId} />}
        <LimitAndLookbackFields draft={draft} set={set} facts={facts} />
        {(fields.has('map') || fields.has('cii')) && <ScopeSection draft={draft} set={set} facts={facts} />}
        {fields.has('channels') && (
          <div className="space-y-1.5">
            <Label htmlFor={`${nameId}-tg`} className="text-xs font-semibold">
              {td('telegramChannels')}
            </Label>
            <Input
              id={`${nameId}-tg`}
              value={draft.channels}
              onChange={(event) => set('channels', event.target.value)}
              placeholder="-1001234567890, 123456789"
              aria-describedby={`${nameId}-tg-help`}
              className="h-8 font-mono text-xs"
            />
            <p id={`${nameId}-tg-help`} className="text-xs text-muted-foreground">
              {td('telegramChannelsHelp')}
            </p>
          </div>
        )}
        {fields.has('rules') && (
          <HighlightRulesField
            rules={draft.rules}
            onChange={(next) => {
              set('rules', next);
              set('rulesTouched', true);
            }}
          />
        )}
      </div>
    </details>
  );
});
