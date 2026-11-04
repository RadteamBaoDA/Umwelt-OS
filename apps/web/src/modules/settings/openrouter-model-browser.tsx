'use client';

import { keepPreviousData, useQuery } from '@tanstack/react-query';
import { useLocale } from 'next-intl';
import { useEffect, useMemo, useState } from 'react';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { Input } from '@/components/ui/input';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { Tabs, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { ApiError } from '@/core/api';
import { modelTypeForModality, type ModelType } from './provider-editor';

type Translate = (key: string, values?: Record<string, string | number>) => string;

export const outputTabs = ['all', 'text', 'image', 'video', 'speech', 'decisions', 'embeddings', 'rerank', 'transcription', 'audio'] as const;
export const sortOptions = ['newest', 'most-popular', 'top-weekly', 'pricing-low-to-high', 'pricing-high-to-low', 'context-high-to-low', 'throughput-high-to-low', 'latency-low-to-high', 'intelligence-high-to-low', 'coding-high-to-low', 'agentic-high-to-low', 'design-arena-elo-high-to-low'] as const;
const inputModalities = ['text', 'image', 'audio', 'file'] as const;
const categories = ['programming', 'roleplay', 'marketing', 'marketing/seo', 'technology', 'science', 'translation', 'legal', 'finance', 'health', 'trivia', 'academia'] as const;
const endpointPaths = ['/api/v1/chat/completions', '/api/v1/completions', '/api/v1/messages', '/api/v1/responses', '/api/v1/embeddings', '/api/v1/rerank', '/api/alpha/decisions', '/api/alpha/extractions', '/api/v1/videos', '/api/v1/audio/speech', '/api/v1/audio/transcriptions', '/api/v1/images', '/api/v1/batches'] as const;
/** Number filters rendered as plain inputs: [wire key, label key]. */
const numberFields = [
  ['context', 'orbContext'], ['min_price', 'orbMinPrice'], ['max_price', 'orbMaxPrice'], ['min_output_price', 'orbMinOutPrice'], ['max_output_price', 'orbMaxOutPrice'],
  ['min_age_days', 'orbMinAge'], ['max_age_days', 'orbMaxAge'], ['min_intelligence_index', 'orbMinIntel'], ['max_intelligence_index', 'orbMaxIntel'],
  ['min_coding_index', 'orbMinCoding'], ['max_coding_index', 'orbMaxCoding'], ['min_agentic_index', 'orbMinAgentic'], ['max_agentic_index', 'orbMaxAgentic'],
  ['min_tool_success_rate', 'orbMinTool'], ['max_tool_success_rate', 'orbMaxTool'],
] as const;
const pageSize = 50;
const debounceMs = 600;
const listPatterns = { supported_parameters: /^[a-z_]{1,40}$/, model_authors: /^[a-z0-9][a-z0-9._-]{0,63}$/, providers: /^[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}$/ } as const;
/** Server-accepted inclusive range per number filter (out-of-range values would 422); keys are matched by suffix. */
const numberRanges: Record<string, [number, number]> = { context: [1, 10_000_000], price: [0, 10_000], output_price: [0, 10_000], age_days: [0, 36_500], intelligence_index: [0, 1000], coding_index: [0, 1000], agentic_index: [0, 1000], tool_success_rate: [0, 1] };
const knownErrors = new Set(['credential_required', 'network_denied', 'auth_error', 'timeout']);

/** Wire filters for the browse endpoint (snake_case because they go on the wire). */
export type OpenRouterFilters = {
  output_modalities: (typeof outputTabs)[number];
  sort: (typeof sortOptions)[number];
  input_modalities?: string[]; category?: string | null; supported_parameters?: string[]; q?: string; endpoints?: string[];
  context?: number; min_price?: number; max_price?: number; min_output_price?: number; max_output_price?: number; min_age_days?: number; max_age_days?: number;
  arch?: string; model_authors?: string[]; providers?: string[]; distillable?: boolean | null; zdr?: boolean; region?: 'eu' | 'us' | null;
  min_intelligence_index?: number; max_intelligence_index?: number; min_coding_index?: number; max_coding_index?: number; min_agentic_index?: number; max_agentic_index?: number;
  min_tool_success_rate?: number; max_tool_success_rate?: number;
};
export type OpenRouterModel = { id: string; name: string; created: number | null; context_length: number | null; input_modalities: string[]; output_modalities: string[]; pricing_prompt: string | null; pricing_completion: string | null; supported_parameters: string[] };
export type OpenRouterBrowseResult = { ok: boolean; error: string | null; models: OpenRouterModel[]; total_count: number; next_offset: number | null };
export type BrowseOpenRouter = (filters: OpenRouterFilters, offset: number, limit: number) => Promise<OpenRouterBrowseResult>;

/** Text-entry filter state: everything the owner types, kept as strings and debounced before it reaches the server. */
export type Typed = { q: string; inputs: string[]; category: string; params: string; authors: string; providers: string; endpoints: string[]; numbers: Record<string, string>; arch: string; region: string; distillable: string; zdr: boolean };
const emptyTyped: Typed = { q: '', inputs: [], category: 'any', params: '', authors: '', providers: '', endpoints: [], numbers: {}, arch: '', region: 'any', distillable: 'any', zdr: false };

/** Splits a comma list, keeps entries matching the wire pattern (invalid ones would 422) and caps at the server's 20. */
function parseList(raw: string, pattern: RegExp): string[] {
  return raw.split(',').map((item) => item.trim()).filter((item) => pattern.test(item)).slice(0, 20);
}

/** Builds the wire filter object from tab, sort and typed state, omitting every empty, out-of-range or inverted-pair field. */
export function buildFilters(output: OpenRouterFilters['output_modalities'], sort: OpenRouterFilters['sort'], typed: Typed): OpenRouterFilters {
  const f: OpenRouterFilters = { output_modalities: output, sort };
  const q = typed.q.trim().slice(0, 100);
  if (q) f.q = q;
  if (typed.inputs.length) f.input_modalities = typed.inputs;
  if (typed.category !== 'any') f.category = typed.category;
  const params = parseList(typed.params, listPatterns.supported_parameters); if (params.length) f.supported_parameters = params;
  const authors = parseList(typed.authors, listPatterns.model_authors); if (authors.length) f.model_authors = authors;
  const providers = parseList(typed.providers, listPatterns.providers); if (providers.length) f.providers = providers;
  if (typed.endpoints.length) f.endpoints = typed.endpoints;
  const nums: Record<string, number> = {};
  for (const [key] of numberFields) {
    const raw = typed.numbers[key]?.trim();
    const [low, high] = numberRanges[key.replace(/^m(in|ax)_/, '')] ?? [0, 0];
    if (raw && Number.isFinite(Number(raw)) && Number(raw) >= low && Number(raw) <= high) nums[key] = Number(raw);
  }
  // An inverted min/max pair can never match and the server rejects it, so neither side is sent.
  for (const key of Object.keys(nums)) {
    const max = key.replace(/^min_/, 'max_');
    if (key.startsWith('min_') && max in nums && nums[key] > nums[max]) { delete nums[key]; delete nums[max]; }
  }
  Object.assign(f, nums);
  if (/^[A-Za-z0-9 ._-]{1,40}$/.test(typed.arch)) f.arch = typed.arch;
  if (typed.region === 'eu' || typed.region === 'us') f.region = typed.region;
  if (typed.distillable !== 'any') f.distillable = typed.distillable === 'yes';
  if (typed.zdr) f.zdr = true;
  return f;
}

/** Picks the model type for a result when the "All" tab is active: the first non-text output modality wins, else chat. */
export function typeForModel(model: OpenRouterModel): ModelType {
  const order = ['embeddings', 'rerank', 'decisions', 'image', 'video', 'speech', 'transcription', 'audio', 'text'];
  const hit = order.find((modality) => model.output_modalities.includes(modality));
  return modelTypeForModality(hit ?? 'text');
}

/** Returns `value` after it has stayed unchanged for `ms`; collapses bursts of typing into one committed value. */
function useDebounced<T>(value: T, ms: number): T {
  const [settled, setSettled] = useState(value);
  useEffect(() => {
    const timer = setTimeout(() => setSettled(value), ms);
    return () => clearTimeout(timer);
  }, [value, ms]);
  return settled;
}

/** Formats a per-token price string as USD per million tokens, or an en dash when missing. */
function perMillion(price: string | null, format: Intl.NumberFormat): string {
  const value = price === null ? NaN : Number(price) * 1e6;
  return Number.isFinite(value) ? format.format(value) : '–';
}

/**
 * Browse and filter OpenRouter's catalogue and pick one model to prefill the Models add row.
 * Requests go through `browse` (rate limited server-side to 10 per minute), so only tab/sort changes fire
 * immediately; typed filters are debounced to 600 ms and superseded requests are discarded by query key.
 * Paging resets whenever the committed filters change. Models in `excluded` are listed but not pickable.
 */
export function OpenRouterModelBrowser({ providerId, browse, excluded, disabled, onPick, t }: { providerId: string; browse: BrowseOpenRouter; excluded: Set<string>; disabled: boolean; onPick: (model: OpenRouterModel, type: ModelType) => void; t: Translate }) {
  const locale = useLocale();
  const money = useMemo(() => new Intl.NumberFormat(locale, { style: 'currency', currency: 'USD', maximumFractionDigits: 4 }), [locale]);
  const [output, setOutput] = useState<OpenRouterFilters['output_modalities']>('all');
  const [sort, setSort] = useState<OpenRouterFilters['sort']>('newest');
  const [typed, setTyped] = useState<Typed>(emptyTyped);
  const [showMore, setShowMore] = useState(false);
  const [page, setPage] = useState({ key: '', offset: 0 });
  const settled = useDebounced(typed, debounceMs);
  const filters = useMemo(() => buildFilters(output, sort, settled), [output, sort, settled]);
  const key = JSON.stringify(filters);
  // Offset belongs to the filter set it was chosen under; a different committed filter set starts at page one.
  const offset = page.key === key ? page.offset : 0;
  const result = useQuery({ queryKey: ['openrouter-models', providerId, filters, offset], queryFn: () => browse(filters, offset, pageSize), placeholderData: keepPreviousData, retry: false, staleTime: 60_000 });
  const patch = (change: Partial<Typed>) => setTyped((current) => ({ ...current, ...change }));
  const data = result.data;
  const errorText = result.error instanceof ApiError && result.error.status === 429 ? t('orbRateLimited')
    : result.error ? t('orbBrowseError_other')
    : data && !data.ok ? t(`orbBrowseError_${data.error && knownErrors.has(data.error) ? data.error : 'other'}`) : '';
  const toggle = (list: string[], value: string, on: boolean) => (on ? [...list, value] : list.filter((item) => item !== value));
  const numberLabelId = (name: string) => `orb-${name}`;
  const first = data && data.models.length ? offset + 1 : 0;
  return <div className="flex flex-col gap-3 rounded-lg border border-border p-3" data-testid="openrouter-browser">
    <Tabs value={output} onValueChange={(next) => setOutput(next as OpenRouterFilters['output_modalities'])}>
      <TabsList aria-label={t('orbTabs')} className="h-auto flex-wrap">{outputTabs.map((tab) => <TabsTrigger key={tab} value={tab}>{t(`orbTab_${tab}`)}</TabsTrigger>)}</TabsList>
    </Tabs>
    <div className="grid gap-2 sm:grid-cols-[minmax(0,1fr)_minmax(0,14rem)_auto] sm:items-end">
      <label className="field"><span className="label">{t('orbSearch')}</span><Input type="search" maxLength={100} disabled={disabled} value={typed.q} onChange={(event) => patch({ q: event.target.value })} /></label>
      <div className="field"><span className="label">{t('orbSort')}</span>
        <Select disabled={disabled} value={sort} onValueChange={(next) => setSort(next as OpenRouterFilters['sort'])}><SelectTrigger aria-label={t('orbSort')}><SelectValue /></SelectTrigger><SelectContent>{sortOptions.map((item) => <SelectItem key={item} value={item}>{t(`orbSort_${item}`)}</SelectItem>)}</SelectContent></Select></div>
      <Button type="button" variant="outline" aria-expanded={showMore} aria-controls="orb-more" onClick={() => setShowMore(!showMore)}>{t('orbMoreFilters')}</Button>
    </div>
    {showMore && <div id="orb-more" className="flex flex-col gap-3 border-t border-border pt-3">
      <fieldset className="m-0 flex flex-wrap gap-3 border-0 p-0"><legend className="label">{t('orbInputModalities')}</legend>
        {inputModalities.map((item) => <label key={item} className="flex items-center gap-2 text-sm"><Checkbox checked={typed.inputs.includes(item)} onCheckedChange={(on) => patch({ inputs: toggle(typed.inputs, item, on === true) })} />{t(`orbInput_${item}`)}</label>)}</fieldset>
      <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
        <div className="field"><span className="label">{t('orbCategory')}</span>
          <Select value={typed.category} onValueChange={(category) => patch({ category })}><SelectTrigger aria-label={t('orbCategory')}><SelectValue /></SelectTrigger><SelectContent><SelectItem value="any">{t('orbAny')}</SelectItem>{categories.map((item) => <SelectItem key={item} value={item}>{t(`orbCat_${item.replace('/', '_')}`)}</SelectItem>)}</SelectContent></Select></div>
        <div className="field"><span className="label">{t('orbRegion')}</span>
          <Select value={typed.region} onValueChange={(region) => patch({ region })}><SelectTrigger aria-label={t('orbRegion')}><SelectValue /></SelectTrigger><SelectContent><SelectItem value="any">{t('orbAny')}</SelectItem><SelectItem value="eu">EU</SelectItem><SelectItem value="us">US</SelectItem></SelectContent></Select></div>
        <div className="field"><span className="label">{t('orbDistillable')}</span>
          <Select value={typed.distillable} onValueChange={(distillable) => patch({ distillable })}><SelectTrigger aria-label={t('orbDistillable')}><SelectValue /></SelectTrigger><SelectContent><SelectItem value="any">{t('orbAny')}</SelectItem><SelectItem value="yes">{t('orbYes')}</SelectItem><SelectItem value="no">{t('orbNo')}</SelectItem></SelectContent></Select></div>
        <label className="field"><span className="label">{t('orbParams')}</span><Input value={typed.params} placeholder="tools, reasoning" onChange={(event) => patch({ params: event.target.value })} /></label>
        <label className="field"><span className="label">{t('orbAuthors')}</span><Input value={typed.authors} placeholder="anthropic, openai" onChange={(event) => patch({ authors: event.target.value })} /></label>
        <label className="field"><span className="label">{t('orbProviders')}</span><Input value={typed.providers} onChange={(event) => patch({ providers: event.target.value })} /></label>
        <label className="field"><span className="label">{t('orbArch')}</span><Input maxLength={40} value={typed.arch} onChange={(event) => patch({ arch: event.target.value })} /></label>
        {numberFields.map(([name, label]) => <label key={name} className="field" htmlFor={numberLabelId(name)}><span className="label">{t(label)}</span>
          <Input id={numberLabelId(name)} type="number" inputMode="decimal" min={0} value={typed.numbers[name] ?? ''} onChange={(event) => patch({ numbers: { ...typed.numbers, [name]: event.target.value } })} /></label>)}
      </div>
      <fieldset className="m-0 flex flex-wrap gap-x-4 gap-y-2 border-0 p-0"><legend className="label">{t('orbEndpoints')}</legend>
        {endpointPaths.map((path) => <label key={path} className="flex items-center gap-2 text-sm"><Checkbox checked={typed.endpoints.includes(path)} onCheckedChange={(on) => patch({ endpoints: toggle(typed.endpoints, path, on === true) })} /><code>{path}</code></label>)}</fieldset>
      <div className="flex flex-wrap items-center gap-4">
        <label className="flex items-center gap-2 text-sm"><Checkbox checked={typed.zdr} onCheckedChange={(on) => patch({ zdr: on === true })} />{t('orbZdr')}</label>
        <Button type="button" variant="outline" onClick={() => setTyped({ ...emptyTyped, q: typed.q })}>{t('orbReset')}</Button>
      </div>
    </div>}
    <div role="status" aria-live="polite" className="text-muted-foreground text-sm">{result.isFetching && !data ? t('orbLoading') : data?.ok ? t('orbCount', { first, last: offset + data.models.length, total: data.total_count }) : ''}</div>
    {errorText && <p className="error m-0" role="alert">{errorText}</p>}
    {data?.ok && data.models.length === 0 && <p className="text-muted-foreground m-0">{t('orbEmpty')}</p>}
    {data?.ok && data.models.length > 0 && <ul className="m-0 flex max-h-96 list-none flex-col gap-1 overflow-y-auto p-0" aria-busy={result.isFetching}>
      {data.models.map((model) => {
        const taken = excluded.has(model.id);
        return <li key={model.id}><button type="button" disabled={disabled || taken} className="flex w-full flex-col gap-0.5 rounded-md border border-border px-3 py-2 text-left hover:bg-muted focus-visible:outline-2 disabled:cursor-not-allowed disabled:opacity-50" onClick={() => onPick(model, output === 'all' ? typeForModel(model) : modelTypeForModality(output))}>
          <span className="text-sm font-semibold">{model.name || model.id}{taken && <span className="text-muted-foreground font-normal"> · {t('orbAdded')}</span>}</span>
          <span className="text-muted-foreground break-all text-xs">{model.id}</span>
          <span className="text-muted-foreground text-xs">{model.context_length ? t('orbContextTokens', { count: model.context_length.toLocaleString(locale) }) : '–'} · {model.input_modalities.join(', ')} → {model.output_modalities.join(', ')} · {t('orbPriceIn')} {perMillion(model.pricing_prompt, money)} · {t('orbPriceOut')} {perMillion(model.pricing_completion, money)}</span>
        </button></li>;
      })}
    </ul>}
    <div className="flex gap-2">
      <Button type="button" variant="outline" disabled={offset === 0} onClick={() => setPage({ key, offset: Math.max(0, offset - pageSize) })}>{t('orbPrev')}</Button>
      <Button type="button" variant="outline" disabled={!data?.ok || data.next_offset === null} onClick={() => data?.next_offset != null && setPage({ key, offset: data.next_offset })}>{t('orbNext')}</Button>
    </div>
  </div>;
}
