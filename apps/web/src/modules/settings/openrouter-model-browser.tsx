'use client';

import type { useTranslations } from 'next-intl';
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
import { BareButton } from '@/components/bare-button';

type Translate = ReturnType<typeof useTranslations<'aiSettings'>>;

export const outputTabs = ['all', 'text', 'image', 'video', 'speech', 'decisions', 'embeddings', 'rerank', 'transcription', 'audio'] as const;
export const sortOptions = ['newest', 'most-popular', 'top-weekly', 'pricing-low-to-high', 'pricing-high-to-low', 'context-high-to-low', 'throughput-high-to-low', 'latency-low-to-high', 'intelligence-high-to-low', 'coding-high-to-low', 'agentic-high-to-low', 'design-arena-elo-high-to-low'] as const;
const inputModalities = ['text', 'image', 'audio', 'file'] as const;
const categories = ['programming', 'roleplay', 'marketing', 'marketing/seo', 'technology', 'science', 'translation', 'legal', 'finance', 'health', 'trivia', 'academia'] as const;
const endpointPaths = ['/api/v1/chat/completions', '/api/v1/completions', '/api/v1/messages', '/api/v1/responses', '/api/v1/embeddings', '/api/v1/rerank', '/api/alpha/decisions', '/api/alpha/extractions', '/api/v1/videos', '/api/v1/audio/speech', '/api/v1/audio/transcriptions', '/api/v1/images', '/api/v1/batches'] as const;
/** Number filters rendered as plain inputs: [wire key, label key]. */
const numberFields = [
  ['context', 'orb.context'], ['min_price', 'orb.minPrice'], ['max_price', 'orb.maxPrice'], ['min_output_price', 'orb.minOutPrice'], ['max_output_price', 'orb.maxOutPrice'],
  ['min_age_days', 'orb.minAge'], ['max_age_days', 'orb.maxAge'], ['min_intelligence_index', 'orb.minIntel'], ['max_intelligence_index', 'orb.maxIntel'],
  ['min_coding_index', 'orb.minCoding'], ['max_coding_index', 'orb.maxCoding'], ['min_agentic_index', 'orb.minAgentic'], ['max_agentic_index', 'orb.maxAgentic'],
  ['min_tool_success_rate', 'orb.minTool'], ['max_tool_success_rate', 'orb.maxTool'],
] as const;
/** Literal catalog keys per filter option (no runtime key building); each table is checked against its option list. */
const TAB_KEYS = {
  all: 'orb.tabAll', text: 'orb.tabText', image: 'orb.tabImage', video: 'orb.tabVideo', speech: 'orb.tabSpeech',
  decisions: 'orb.tabDecisions', embeddings: 'orb.tabEmbeddings', rerank: 'orb.tabRerank', transcription: 'orb.tabTranscription', audio: 'orb.tabAudio',
} as const satisfies Record<(typeof outputTabs)[number], string>;
const SORT_KEYS = {
  newest: 'orb.sortNewest', 'most-popular': 'orb.sortMostPopular', 'top-weekly': 'orb.sortTopWeekly',
  'pricing-low-to-high': 'orb.sortPricingLowToHigh', 'pricing-high-to-low': 'orb.sortPricingHighToLow',
  'context-high-to-low': 'orb.sortContextHighToLow', 'throughput-high-to-low': 'orb.sortThroughputHighToLow',
  'latency-low-to-high': 'orb.sortLatencyLowToHigh', 'intelligence-high-to-low': 'orb.sortIntelligenceHighToLow',
  'coding-high-to-low': 'orb.sortCodingHighToLow', 'agentic-high-to-low': 'orb.sortAgenticHighToLow',
  'design-arena-elo-high-to-low': 'orb.sortDesignArenaEloHighToLow',
} as const satisfies Record<(typeof sortOptions)[number], string>;
const INPUT_KEYS = { text: 'orb.inputText', image: 'orb.inputImage', audio: 'orb.inputAudio', file: 'orb.inputFile' } as const satisfies Record<(typeof inputModalities)[number], string>;
const CATEGORY_KEYS = {
  programming: 'orb.catProgramming', roleplay: 'orb.catRoleplay', marketing: 'orb.catMarketing', 'marketing/seo': 'orb.catMarketingSeo',
  technology: 'orb.catTechnology', science: 'orb.catScience', translation: 'orb.catTranslation', legal: 'orb.catLegal',
  finance: 'orb.catFinance', health: 'orb.catHealth', trivia: 'orb.catTrivia', academia: 'orb.catAcademia',
} as const satisfies Record<(typeof categories)[number], string>;
const pageSize = 50;
const debounceMs = 600;
const listPatterns = { supported_parameters: /^[a-z_]{1,40}$/, model_authors: /^[a-z0-9][a-z0-9._-]{0,63}$/, providers: /^[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}$/ } as const;
/** Server-accepted inclusive range per number filter (out-of-range values would 422); keys are matched by suffix. */
const numberRanges: Record<string, [number, number]> = { context: [1, 10_000_000], price: [0, 10_000], output_price: [0, 10_000], age_days: [0, 36_500], intelligence_index: [0, 1000], coding_index: [0, 1000], agentic_index: [0, 1000], tool_success_rate: [0, 1] };
/** Literal catalog key per browse error code (no runtime key building); unknown codes read as the generic error. */
const BROWSE_ERROR_KEYS = {
  credential_required: 'orb.browseErrorCredentialRequired',
  network_denied: 'orb.browseErrorNetworkDenied',
  auth_error: 'orb.browseErrorAuthError',
  timeout: 'orb.browseErrorTimeout',
} as const;

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

/** Parses the typed numeric bounds, keeping in-range values and dropping both sides of an inverted min/max pair (the server rejects it). */
function numericFilters(typed: Typed): Record<string, number> {
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
  return nums;
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
  Object.assign(f, numericFilters(typed));
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
 * Requests go through `browse` (rate limited server-side to 30 requests per 60 seconds), so only tab/sort changes fire
 * immediately; typed filters are debounced to 600 ms and superseded requests are discarded by query key.
 * Paging resets whenever the committed filters change. Models in `excluded` are listed but not pickable.
 */
// eslint-disable-next-line complexity -- stateful workflow whose branches mirror independent UI/request states (loading, error, conflict, per-step content); extraction into per-state components is tracked as follow-up R3.6 so this refactor stays behaviour-neutral
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
  const errorText = result.error instanceof ApiError && result.error.status === 429 ? t('orb.rateLimited')
    : result.error ? t('orb.browseErrorOther')
    : data && !data.ok ? t(data.error && Object.hasOwn(BROWSE_ERROR_KEYS, data.error) ? BROWSE_ERROR_KEYS[data.error as keyof typeof BROWSE_ERROR_KEYS] : 'orb.browseErrorOther') : '';
  const toggle = (list: string[], value: string, on: boolean) => (on ? [...list, value] : list.filter((item) => item !== value));
  const numberLabelId = (name: string) => `orb-${name}`;
  const first = data && data.models.length ? offset + 1 : 0;
  return <div className="flex flex-col gap-3 rounded-lg border border-border p-3" data-testid="openrouter-browser">
    <Tabs value={output} onValueChange={(next) => setOutput(next as OpenRouterFilters['output_modalities'])}>
      <TabsList aria-label={t('orb.tabs')} className="h-auto flex-wrap">{outputTabs.map((tab) => <TabsTrigger key={tab} value={tab}>{t(TAB_KEYS[tab])}</TabsTrigger>)}</TabsList>
    </Tabs>
    <div className="grid gap-2 sm:grid-cols-[minmax(0,1fr)_minmax(0,14rem)_auto] sm:items-end">
      <label className="field"><span className="label">{t('orb.search')}</span><Input type="search" maxLength={100} disabled={disabled} value={typed.q} onChange={(event) => patch({ q: event.target.value })} /></label>
      <div className="field"><span className="label">{t('orb.sort')}</span>
        <Select disabled={disabled} value={sort} onValueChange={(next) => setSort(next as OpenRouterFilters['sort'])}><SelectTrigger aria-label={t('orb.sort')}><SelectValue /></SelectTrigger><SelectContent>{sortOptions.map((item) => <SelectItem key={item} value={item}>{t(SORT_KEYS[item])}</SelectItem>)}</SelectContent></Select></div>
      <Button type="button" variant="outline" aria-expanded={showMore} aria-controls="orb-more" onClick={() => setShowMore(!showMore)}>{t('orb.moreFilters')}</Button>
    </div>
    {showMore && <div id="orb-more" className="flex flex-col gap-3 border-t border-border pt-3">
      <fieldset className="m-0 flex flex-wrap gap-3 border-0 p-0"><legend className="label">{t('orb.inputModalities')}</legend>
        {inputModalities.map((item) => <label key={item} className="flex items-center gap-2 text-sm"><Checkbox checked={typed.inputs.includes(item)} onCheckedChange={(on) => patch({ inputs: toggle(typed.inputs, item, on === true) })} />{t(INPUT_KEYS[item])}</label>)}</fieldset>
      <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
        <div className="field"><span className="label">{t('orb.category')}</span>
          <Select value={typed.category} onValueChange={(category) => patch({ category })}><SelectTrigger aria-label={t('orb.category')}><SelectValue /></SelectTrigger><SelectContent><SelectItem value="any">{t('orb.any')}</SelectItem>{categories.map((item) => <SelectItem key={item} value={item}>{t(CATEGORY_KEYS[item])}</SelectItem>)}</SelectContent></Select></div>
        <div className="field"><span className="label">{t('orb.region')}</span>
          <Select value={typed.region} onValueChange={(region) => patch({ region })}><SelectTrigger aria-label={t('orb.region')}><SelectValue /></SelectTrigger><SelectContent><SelectItem value="any">{t('orb.any')}</SelectItem><SelectItem value="eu">EU</SelectItem><SelectItem value="us">US</SelectItem></SelectContent></Select></div>
        <div className="field"><span className="label">{t('orb.distillable')}</span>
          <Select value={typed.distillable} onValueChange={(distillable) => patch({ distillable })}><SelectTrigger aria-label={t('orb.distillable')}><SelectValue /></SelectTrigger><SelectContent><SelectItem value="any">{t('orb.any')}</SelectItem><SelectItem value="yes">{t('orb.yes')}</SelectItem><SelectItem value="no">{t('orb.no')}</SelectItem></SelectContent></Select></div>
        <label className="field"><span className="label">{t('orb.params')}</span><Input value={typed.params} placeholder="tools, reasoning" onChange={(event) => patch({ params: event.target.value })} /></label>
        <label className="field"><span className="label">{t('orb.authors')}</span><Input value={typed.authors} placeholder="anthropic, openai" onChange={(event) => patch({ authors: event.target.value })} /></label>
        <label className="field"><span className="label">{t('orb.providers')}</span><Input value={typed.providers} onChange={(event) => patch({ providers: event.target.value })} /></label>
        <label className="field"><span className="label">{t('orb.arch')}</span><Input maxLength={40} value={typed.arch} onChange={(event) => patch({ arch: event.target.value })} /></label>
        {numberFields.map(([name, label]) => <label key={name} className="field" htmlFor={numberLabelId(name)}><span className="label">{t(label)}</span>
          <Input id={numberLabelId(name)} type="number" inputMode="decimal" min={0} value={typed.numbers[name] ?? ''} onChange={(event) => patch({ numbers: { ...typed.numbers, [name]: event.target.value } })} /></label>)}
      </div>
      <fieldset className="m-0 flex flex-wrap gap-x-4 gap-y-2 border-0 p-0"><legend className="label">{t('orb.endpoints')}</legend>
        {endpointPaths.map((path) => <label key={path} className="flex items-center gap-2 text-sm"><Checkbox checked={typed.endpoints.includes(path)} onCheckedChange={(on) => patch({ endpoints: toggle(typed.endpoints, path, on === true) })} /><code>{path}</code></label>)}</fieldset>
      <div className="flex flex-wrap items-center gap-4">
        <label className="flex items-center gap-2 text-sm"><Checkbox checked={typed.zdr} onCheckedChange={(on) => patch({ zdr: on === true })} />{t('orb.zdr')}</label>
        <Button type="button" variant="outline" onClick={() => setTyped({ ...emptyTyped, q: typed.q })}>{t('orb.reset')}</Button>
      </div>
    </div>}
    <div role="status" aria-live="polite" className="text-muted-foreground text-sm">{result.isFetching && !data ? t('orb.loading') : data?.ok ? t('orb.count', { first, last: offset + data.models.length, total: data.total_count }) : ''}</div>
    {errorText && <p className="error m-0" role="alert">{errorText}</p>}
    {data?.ok && data.models.length === 0 && <p className="text-muted-foreground m-0">{t('orb.empty')}</p>}
    {data?.ok && data.models.length > 0 && <ul className="m-0 flex max-h-96 list-none flex-col gap-1 overflow-y-auto p-0" aria-busy={result.isFetching}>
      {data.models.map((model) => {
        const taken = excluded.has(model.id);
        return <li key={model.id}><BareButton type="button" disabled={disabled || taken} className="flex w-full flex-col gap-0.5 rounded-md border border-border px-3 py-2 text-left hover:bg-muted focus-visible:outline-2 disabled:cursor-not-allowed disabled:opacity-50" onClick={() => onPick(model, output === 'all' ? typeForModel(model) : modelTypeForModality(output))}>
          <span className="text-sm font-semibold">{model.name || model.id}{taken && <span className="text-muted-foreground font-normal"> · {t('orb.added')}</span>}</span>
          <span className="text-muted-foreground break-all text-xs">{model.id}</span>
          <span className="text-muted-foreground text-xs">{model.context_length ? t('orb.contextTokens', { count: model.context_length.toLocaleString(locale) }) : '–'} · {model.input_modalities.join(', ')} → {model.output_modalities.join(', ')} · {t('orb.priceIn')} {perMillion(model.pricing_prompt, money)} · {t('orb.priceOut')} {perMillion(model.pricing_completion, money)}</span>
        </BareButton></li>;
      })}
    </ul>}
    <div className="flex gap-2">
      <Button type="button" variant="outline" disabled={offset === 0} onClick={() => setPage({ key, offset: Math.max(0, offset - pageSize) })}>{t('orb.prev')}</Button>
      <Button type="button" variant="outline" disabled={!data?.ok || data.next_offset === null} onClick={() => data?.next_offset != null && setPage({ key, offset: data.next_offset })}>{t('orb.next')}</Button>
    </div>
  </div>;
}
