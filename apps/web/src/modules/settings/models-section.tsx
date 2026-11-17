'use client';

import type { useTranslations } from 'next-intl';
import { TablePagination, usePagination } from '@/components/ui/pagination';
import { Check, Info, Plus, Trash2 } from 'lucide-react';
import { useState } from 'react';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { Input } from '@/components/ui/input';
import { SearchableSelect } from '@/components/ui/searchable-select';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '@/components/ui/tooltip';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import { OpenRouterModelBrowser, type BrowseOpenRouter } from './openrouter-model-browser';
import {
  guessModelType,
  mappingProviderId,
  modelTestKey,
  ModelTestChip,
  openRouterOnlyModelTypes,
  selectableModelTypes,
  testRoleByType,
  type ModelTestState,
  type ModelType,
  type ProviderModel,
  type ProviderRead,
} from './provider-editor';
import { FieldGrid } from '@/components/settings-layout';
import { BareButton } from '@/components/bare-button';

/** Literal catalog key per model type (no runtime key building). */
const MODEL_TYPE_KEYS = {
  llm: 'type.llm',
  embedding: 'type.embedding',
  reranker: 'type.reranker',
  decision: 'type.decision',
  background: 'type.background',
  vision: 'type.vision',
  asr: 'type.asr',
  tts: 'type.tts',
  image: 'type.image',
  video: 'type.video',
  speech: 'type.speech',
  audio: 'type.audio',
  transcription: 'type.transcription',
} as const satisfies Record<ModelType, string>;

type Translate = ReturnType<typeof useTranslations<'aiSettings'>>;
/** Stored mapping: which provider model serves one role. */
export type Mapping = {
  model: string;
  version: string | null;
  destination: 'unknown' | 'remote';
  provider_id?: string | null;
  dimensions?: number | null;
  reasoning_effort?: ReasoningEffort;
  max_output_tokens?: number | null;
};
/** Chat-role reasoning effort; `off` sends nothing to the provider. */
export type ReasoningEffort = 'off' | 'low' | 'medium' | 'high';
/** Hosted clouds that choose their own output limit; the Max output tokens field is hidden for them and the server discards any value. Mirrors `PROVIDER_KINDS_WITHOUT_MAX_TOKENS` in `core/model_gateway/schemas.py`. */
export const kindsWithoutMaxTokens: readonly string[] = ['anthropic', 'openai', 'groq'];
const efforts: readonly ReasoningEffort[] = ['off', 'low', 'medium', 'high'];
/** Literal catalog key per reasoning effort (no runtime key building). */
const EFFORT_KEYS = { off: 'effort.off', low: 'effort.low', medium: 'effort.medium', high: 'effort.high' } as const satisfies Record<
  ReasoningEffort,
  string
>;
/** Role the owner can pick a default model for. */
export type DefaultSlot = 'chat' | 'brief' | 'background' | 'embedding' | 'reranker' | 'vision';
/** Role defaults as stored by the backend; `null` leaves a role unset (brief/background then fall back). */
export type Defaults = Partial<Record<DefaultSlot, Mapping | null>>;
const none = '__none__';
const same = '__same__';

/** Mapping for one provider model; local providers stay "unknown" destination, remote ones "remote". */
export function mappingFor(provider: ProviderRead, model: string): Mapping {
  return { model, version: null, destination: provider.local ? 'unknown' : 'remote', provider_id: provider.id };
}

/** Keeps the owner's output limit when a role switches to `provider`, and drops it for hosted kinds that must not carry one. */
export function carryMaxTokens(picked: Mapping, previous: Mapping | null | undefined, provider: ProviderRead): Mapping {
  return previous?.max_output_tokens != null && !kindsWithoutMaxTokens.includes(provider.kind)
    ? { ...picked, max_output_tokens: previous.max_output_tokens }
    : picked;
}

/** Clears every default still pointing at a model that was just removed, so no mapping dangles. */
export function pruneDefaults(prev: ProviderRead[], next: ProviderRead[], defaults: Defaults): Defaults {
  const live = new Set(next.flatMap((p) => (p.models ?? []).map((m) => modelTestKey(p.id, m.id))));
  const gone = new Set(prev.flatMap((p) => (p.models ?? []).map((m) => modelTestKey(p.id, m.id))).filter((key) => !live.has(key)));
  return Object.fromEntries(
    Object.entries(defaults).map(([role, m]) => [
      role,
      m?.model && gone.has(modelTestKey(mappingProviderId(m.provider_id, prev), m.model)) ? null : m,
    ]),
  );
}

type ModelsProps = {
  providers: ProviderRead[];
  discovered: Record<string, string[]>;
  modelTests: Record<string, ModelTestState>;
  checkedAt?: Record<string, string>;
  details?: Record<string, string>;
  disabled: boolean;
  onChange: (providers: ProviderRead[]) => void;
  onTestModel: (provider: ProviderRead, model: ProviderModel) => void;
  browseOpenRouter?: (provider: ProviderRead) => BrowseOpenRouter;
  t: Translate;
};

/** The single Models area: add row (provider, discovered model or OpenRouter browser, type, vision) and one table of every added model. OpenRouter providers get output-only types and the catalogue browser when `browseOpenRouter` is supplied. */
// eslint-disable-next-line complexity -- stateful workflow whose branches mirror independent UI/request states (loading, error, conflict, per-step content); extraction into per-state components is tracked as follow-up R3.6 so this refactor stays behaviour-neutral
export function ModelsPanel({
  providers,
  discovered,
  modelTests,
  checkedAt = {},
  details = {},
  disabled,
  onChange,
  onTestModel,
  browseOpenRouter,
  t,
}: ModelsProps) {
  const [providerId, setProviderId] = useState('');
  const [pending, setPending] = useState('');
  const [type, setType] = useState<ModelType>('llm');
  const [vision, setVision] = useState(false);
  const provider = providers.find((item) => item.id === providerId) ?? providers[0];
  const added = new Set((provider?.models ?? []).map((item) => item.id));
  const options = (provider ? (discovered[provider.id] ?? []) : []).filter((id) => !added.has(id));
  // With no provider the add row is inert; the "Add a provider first" note below explains why.
  const noProvider = providers.length === 0;
  const browsing = provider?.kind === 'openrouter' && browseOpenRouter !== undefined;
  const typeChoices = provider?.kind === 'openrouter' ? [...selectableModelTypes, ...openRouterOnlyModelTypes] : selectableModelTypes;
  const noProviderNote = noProvider ? 'models-no-provider-note' : undefined;
  const rows = providers.flatMap((item) => (item.models ?? []).map((model) => ({ provider: item, model })));
  const pager = usePagination(rows);
  /** Replaces one provider's model list. */
  const patch = (id: string, models: ProviderModel[]) => onChange(providers.map((item) => (item.id === id ? { ...item, models } : item)));
  return (
    <div className="flex flex-col gap-3">
      <ModelAddRow
        providers={providers}
        provider={provider}
        pending={pending}
        type={type}
        vision={vision}
        options={options}
        browsing={browsing}
        typeChoices={typeChoices}
        noProvider={noProvider}
        noProviderNote={noProviderNote}
        disabled={disabled}
        t={t}
        onProviderChange={(id) => {
          setProviderId(id);
          setPending('');
          setType('llm');
        }}
        onModelChange={(id) => {
          // Legacy guesses (vision/asr/tts) fold into Chat; a vision-looking id also pre-ticks the vision flag.
          const guess = guessModelType(id);
          setPending(id);
          setType((selectableModelTypes as readonly string[]).includes(guess) ? guess : 'llm');
          setVision(guess === 'vision');
        }}
        onTypeChange={setType}
        onVisionChange={setVision}
        onAdd={() => {
          patch(provider!.id, [...(provider!.models ?? []), newModel(pending, type, vision)]);
          setPending('');
          setVision(false);
        }}
      />
      {browsing && provider && (
        <OpenRouterModelBrowser
          providerId={provider.id}
          browse={browseOpenRouter!(provider)}
          excluded={added}
          disabled={disabled}
          t={t}
          onPick={(model, picked) => {
            setPending(model.id);
            setType(picked);
            setVision(model.input_modalities.includes('image') && picked === 'llm');
          }}
        />
      )}
      {rows.length === 0 ? (
        <p id={noProvider ? 'models-no-provider-note' : undefined} className="text-muted-foreground m-0">
          {t(providers.length === 0 ? 'models.noProvider' : 'models.empty')}
        </p>
      ) : (
        <ModelsTable
          pager={pager}
          modelTests={modelTests}
          checkedAt={checkedAt}
          details={details}
          disabled={disabled}
          t={t}
          onTestModel={onTestModel}
          onRemove={(owner, model) =>
            patch(
              owner.id,
              (owner.models ?? []).filter((item) => item.id !== model.id),
            )
          }
        />
      )}
      <p className="text-muted-foreground m-0 text-sm">{t('model.typesHelp')}</p>
    </div>
  );
}

/** Builds the model entry for the add row; only Chat models carry the vision flag. */
function newModel(id: string, type: ModelType, vision: boolean): ProviderModel {
  return type === 'llm' && vision ? { id, type, vision: true } : { id, type };
}

type ModelRow = { provider: ProviderRead; model: ProviderModel };

type ModelAddRowProps = {
  providers: ProviderRead[];
  provider: ProviderRead | undefined;
  pending: string;
  type: ModelType;
  vision: boolean;
  options: string[];
  browsing: boolean;
  typeChoices: readonly ModelType[];
  noProvider: boolean;
  noProviderNote: string | undefined;
  disabled: boolean;
  t: Translate;
  onProviderChange: (id: string) => void;
  onModelChange: (id: string) => void;
  onTypeChange: (type: ModelType) => void;
  onVisionChange: (vision: boolean) => void;
  onAdd: () => void;
};

/** Add row of the Models panel (provider, model picker, type, Add button, vision flag). Pure presentation: all state lives in `ModelsPanel` and flows back through the `on*` callbacks; no requests. */
function ModelAddRow({
  providers,
  provider,
  pending,
  type,
  vision,
  options,
  browsing,
  typeChoices,
  noProvider,
  noProviderNote,
  disabled,
  t,
  onProviderChange,
  onModelChange,
  onTypeChange,
  onVisionChange,
  onAdd,
}: ModelAddRowProps) {
  return (
    <div className="grid items-end gap-2 sm:grid-cols-[minmax(0,10rem)_minmax(9rem,1fr)_minmax(0,11rem)_auto]">
      <div className="field">
        <span className="label">{t('provider.column')}</span>
        <Select disabled={disabled || providers.length === 0} value={provider?.id ?? ''} onValueChange={onProviderChange}>
          <SelectTrigger aria-label={t('provider.column')} aria-describedby={noProviderNote}>
            <SelectValue placeholder={t('providers.none')} />
          </SelectTrigger>
          <SelectContent>
            {providers.map((item) => (
              <SelectItem key={item.id} value={item.id}>
                {item.label || item.id}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>
      {/* minmax(0,1fr): let the model picker shrink inside its track instead of overflowing into Type at tablet widths. */}
      <div className="field grid-cols-[minmax(0,1fr)]">
        <span className="label">{t('model.column')}</span>
        {browsing ? (
          <p className="text-muted-foreground m-0 break-all text-sm" aria-live="polite">
            {pending || t('selectModel')}
          </p>
        ) : (
          <SearchableSelect
            aria-label={t('model.column')}
            aria-describedby={noProviderNote}
            value={pending}
            options={options}
            disabled={disabled || options.length === 0}
            placeholder={t('selectModel')}
            searchLabel={t('model.pickerSearch')}
            emptyLabel={t('model.pickerEmpty')}
            onValueChange={onModelChange}
          />
        )}
      </div>
      <div className="field">
        <span className="label">{t('type.column')}</span>
        <Select disabled={disabled || noProvider} value={type} onValueChange={(next) => onTypeChange(next as ModelType)}>
          <SelectTrigger aria-label={t('type.column')} aria-describedby={noProviderNote}>
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {typeChoices.map((item) => (
              <SelectItem key={item} value={item}>
                {t(MODEL_TYPE_KEYS[item])}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>
      <Button type="button" variant="outline" disabled={disabled || !provider || !pending} onClick={onAdd}>
        <Plus className="size-4" aria-hidden="true" /> {t('model.add')}
      </Button>
      {type === 'llm' && (
        <label className="flex items-center gap-2 text-sm sm:col-span-4">
          <Checkbox
            disabled={disabled || noProvider}
            aria-describedby={noProviderNote}
            checked={vision}
            onCheckedChange={(next) => onVisionChange(next === true)}
          />
          {t('model.vision')}
        </label>
      )}
    </div>
  );
}

type ModelsTableProps = {
  pager: ReturnType<typeof usePagination<ModelRow>>;
  modelTests: Record<string, ModelTestState>;
  checkedAt: Record<string, string>;
  details: Record<string, string>;
  disabled: boolean;
  t: Translate;
  onTestModel: (provider: ProviderRead, model: ProviderModel) => void;
  onRemove: (provider: ProviderRead, model: ProviderModel) => void;
};

/** Paged table of every added model with Test/Remove actions, last test chip, check time and failure tooltip. Pure presentation over the pager owned by `ModelsPanel`; removal and tests are delegated to callbacks. */
function ModelsTable({ pager, modelTests, checkedAt, details, disabled, t, onTestModel, onRemove }: ModelsTableProps) {
  return (
    <TooltipProvider delayDuration={200}>
      <div>
        <Table aria-label={t('models.table')}>
          <TableHeader>
            <TableRow>
              <TableHead>{t('provider.column')}</TableHead>
              <TableHead>{t('model.column')}</TableHead>
              <TableHead>{t('type.column')}</TableHead>
              <TableHead>{t('visionColumn')}</TableHead>
              <TableHead>{t('actionsColumn')}</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {pager.pageItems.map(({ provider: owner, model }) => {
              const key = modelTestKey(owner.id, model.id);
              return (
                <ModelTableRow
                  key={key}
                  owner={owner}
                  model={model}
                  state={modelTests[key]}
                  checkedAt={checkedAt[key]}
                  detail={details[key]}
                  disabled={disabled}
                  t={t}
                  onTestModel={onTestModel}
                  onRemove={onRemove}
                />
              );
            })}
          </TableBody>
        </Table>
        <TablePagination {...pager} />
      </div>
    </TooltipProvider>
  );
}

type ModelTableRowProps = {
  owner: ProviderRead;
  model: ProviderModel;
  state: ModelTestState | undefined;
  checkedAt: string | undefined;
  detail: string | undefined;
  disabled: boolean;
  t: Translate;
  onTestModel: (provider: ProviderRead, model: ProviderModel) => void;
  onRemove: (provider: ProviderRead, model: ProviderModel) => void;
};

/** One model row: provider, id, type, vision mark and the action cell. `state`/`checkedAt`/`detail` are the row's latest test result; a Test button is disabled while pending, for untestable types or providers without a base URL. */
function ModelTableRow({ owner, model, state, checkedAt, detail, disabled, t, onTestModel, onRemove }: ModelTableRowProps) {
  return (
    <TableRow>
      <TableCell>{owner.label || owner.id}</TableCell>
      <TableCell className="break-all whitespace-normal">{model.id}</TableCell>
      <TableCell>{t(MODEL_TYPE_KEYS[model.type])}</TableCell>
      <TableCell>
        {model.vision ? (
          <>
            <Check className="size-4" aria-hidden="true" />
            <span className="sr-only">{t('model.vision')}</span>
          </>
        ) : (
          <span className="text-muted-foreground" aria-hidden="true">
            –
          </span>
        )}
      </TableCell>
      <TableCell>
        <div className="flex flex-wrap items-center gap-2">
          <Button
            type="button"
            variant="outline"
            disabled={disabled || state === 'pending' || !testRoleByType[model.type] || !owner.base_url}
            aria-label={t('model.testAria', { id: model.id })}
            onClick={() => onTestModel(owner, model)}
          >
            {t('model.test')}
          </Button>
          <Button
            type="button"
            variant="outline"
            disabled={disabled}
            aria-label={t('model.remove', { id: model.id })}
            onClick={() => onRemove(owner, model)}
          >
            <Trash2 className="size-4" aria-hidden="true" />
          </Button>
          <ModelTestChip state={state} t={t} />
          {state && state !== 'pending' && checkedAt && (
            <span className="text-muted-foreground text-sm">{t('model.checkedAt', { time: new Date(checkedAt).toLocaleString() })}</span>
          )}
          {state && state !== 'pending' && state !== 'supported' && detail && (
            <Tooltip>
              <TooltipTrigger asChild>
                <BareButton type="button" className="text-destructive inline-flex" aria-label={detail}>
                  <Info className="size-4" aria-hidden="true" />
                </BareButton>
              </TooltipTrigger>
              <TooltipContent className="max-w-sm break-words">{detail}</TooltipContent>
            </Tooltip>
          )}
        </div>
      </TableCell>
    </TableRow>
  );
}

type DefaultsProps = {
  providers: ProviderRead[];
  defaults: Defaults;
  disabled: boolean;
  embeddingStatus: 'idle' | 'checking' | 'error';
  onPick: (slot: DefaultSlot, provider: ProviderRead | null, model: string) => void;
  onEffort?: (effort: ReasoningEffort) => void;
  onMaxTokens?: (slot: DefaultSlot, value: number | null) => void;
  t: Translate;
};

type DefaultEntry = { provider: ProviderRead; model: ProviderModel };

type DefaultFieldProps = {
  slot: DefaultSlot;
  label: string;
  list: DefaultEntry[];
  extra?: { id: string; label: string };
  providers: ProviderRead[];
  defaults: Defaults;
  disabled: boolean;
  hasModels: boolean;
  onPick: DefaultsProps['onPick'];
  t: Translate;
};

/** One default-model select for `slot`, offering only `list` (plus an optional fallback entry `extra`). Picking a fallback entry clears the default via `onPick(slot, null, '')`. */
function DefaultField({ slot, label, list, extra, providers, defaults, disabled, hasModels, onPick, t }: DefaultFieldProps) {
  const m = defaults[slot];
  const ref = m?.model ? modelTestKey(mappingProviderId(m.provider_id, providers), m.model) : '';
  return (
    <label className="field">
      <span className="label">{label}</span>
      <Select
        disabled={disabled || !hasModels || (list.length === 0 && !extra)}
        value={ref || extra?.id || undefined}
        onValueChange={(next) => {
          if (next === none || next === same) return onPick(slot, null, '');
          const hit = list.find((e) => modelTestKey(e.provider.id, e.model.id) === next);
          if (hit) onPick(slot, hit.provider, hit.model.id);
        }}
      >
        <SelectTrigger aria-label={label}>
          <SelectValue placeholder={t('selectModel')} />
        </SelectTrigger>
        <SelectContent>
          {extra && <SelectItem value={extra.id}>{extra.label}</SelectItem>}
          {list.map((e) => (
            <SelectItem
              key={modelTestKey(e.provider.id, e.model.id)}
              value={modelTestKey(e.provider.id, e.model.id)}
            >{`${e.provider.label || e.provider.id} · ${e.model.id}`}</SelectItem>
          ))}
        </SelectContent>
      </Select>
    </label>
  );
}

type MaxTokensFieldProps = {
  slot: DefaultSlot;
  label: string;
  providers: ProviderRead[];
  defaults: Defaults;
  disabled: boolean;
  onMaxTokens: DefaultsProps['onMaxTokens'];
  t: Translate;
};

/** Optional output-token limit input for one LLM role; renders nothing unless `onMaxTokens` is given, the role has a model, and its provider kind accepts a limit. Blank clears the limit; values clamp to 1..1000000. */
function MaxTokensField({ slot, label, providers, defaults, disabled, onMaxTokens, t }: MaxTokensFieldProps) {
  const mapping = defaults[slot];
  const owner = mapping?.model ? providers.find((p) => p.id === mappingProviderId(mapping.provider_id, providers)) : undefined;
  if (!onMaxTokens || !mapping?.model || !owner || kindsWithoutMaxTokens.includes(owner.kind)) return null;
  return (
    <label className="field">
      <span className="label">{`${label} · ${t('maxOutputTokens')}`}</span>
      <Input
        type="number"
        inputMode="numeric"
        min={1}
        max={1000000}
        step={1}
        disabled={disabled}
        aria-label={`${label} · ${t('maxOutputTokens')}`}
        value={mapping.max_output_tokens ?? ''}
        onChange={(event) => {
          const raw = event.target.value.trim();
          const n = Number(raw);
          onMaxTokens(slot, raw === '' || !Number.isFinite(n) || n < 1 ? null : Math.min(Math.trunc(n), 1000000));
        }}
      />
      <span className="text-muted-foreground text-sm">{t('maxOutputTokensHelp')}</span>
    </label>
  );
}

/** Reasoning-effort select for the chat role; `disabled` until a chat model is chosen. Reports the picked effort via `onEffort`. */
function EffortField({
  value,
  disabled,
  onEffort,
  t,
}: {
  value: ReasoningEffort;
  disabled: boolean;
  onEffort: (effort: ReasoningEffort) => void;
  t: Translate;
}) {
  return (
    <label className="field">
      <span className="label">{t('reasoningEffort')}</span>
      <Select disabled={disabled} value={value} onValueChange={(next) => onEffort(next as ReasoningEffort)}>
        <SelectTrigger aria-label={t('reasoningEffort')}>
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          {efforts.map((item) => (
            <SelectItem key={item} value={item}>
              {t(EFFORT_KEYS[item])}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
      <span className="text-muted-foreground text-sm">{t('reasoningEffortHelp')}</span>
    </label>
  );
}

/** Default-model selects, one per role; each lists only added models of the matching type. Optional roles offer a fallback entry that clears the default. The chat role also gets a reasoning-effort select when `onEffort` is given. When `onMaxTokens` is given, each LLM role whose model sits on a non-hosted provider (not anthropic/openai/groq) gets an optional Max output tokens number input; blank means the provider default. */
export function ModelDefaults({ providers, defaults, disabled, embeddingStatus, onPick, onEffort, onMaxTokens, t }: DefaultsProps) {
  const all = providers.flatMap((p) => (p.models ?? []).map((m) => ({ provider: p, model: m })));
  const entries = (filter: (m: ProviderModel) => boolean) => all.filter((e) => filter(e.model));
  const shared = { providers, defaults, disabled, hasModels: all.length > 0, onPick, t };
  const tokens = { providers, defaults, disabled, onMaxTokens, t };
  const chats = entries((m) => m.type === 'llm');
  return (
    <div className="flex flex-col gap-3">
      <h3 className="m-0 text-base font-semibold">{t('defaultsTitle')}</h3>
      <p className="text-muted-foreground m-0 text-sm">{t('defaultsHelp')}</p>
      <FieldGrid>
        <DefaultField {...shared} slot="chat" label={t('chatModel')} list={chats} />
        <MaxTokensField {...tokens} slot="chat" label={t('chatModel')} />
        {onEffort && (
          <EffortField
            value={defaults.chat?.reasoning_effort ?? 'off'}
            disabled={disabled || !defaults.chat?.model}
            onEffort={onEffort}
            t={t}
          />
        )}
        <DefaultField {...shared} slot="brief" label={t('briefModel')} list={chats} extra={{ id: same, label: t('sameAsChat') }} />
        <MaxTokensField {...tokens} slot="brief" label={t('briefModel')} />
        <DefaultField
          {...shared}
          slot="background"
          label={t('backgroundModel')}
          list={entries((m) => m.type === 'background' || m.type === 'llm')}
          extra={{ id: same, label: t('sameAsBrief') }}
        />
        <MaxTokensField {...tokens} slot="background" label={t('backgroundModel')} />
        <DefaultField {...shared} slot="embedding" label={t('embedding.model')} list={entries((m) => m.type === 'embedding')} />
        <DefaultField
          {...shared}
          slot="reranker"
          label={t('rerankerModel')}
          list={entries((m) => m.type === 'reranker')}
          extra={{ id: none, label: t('noneOption') }}
        />
        <DefaultField
          {...shared}
          slot="vision"
          label={t('visionModel')}
          list={entries((m) => m.type === 'llm' && m.vision === true)}
          extra={{ id: none, label: t('noneOption') }}
        />
        <MaxTokensField {...tokens} slot="vision" label={t('visionModel')} />
      </FieldGrid>
      <div role="status" aria-live="polite">
        {embeddingStatus === 'checking' && <p className="text-muted-foreground m-0 text-sm">{t('embedding.checking')}</p>}
        {embeddingStatus === 'error' && (
          <p className="error m-0 text-sm" role="alert">
            {t('embedding.probeFailed')}
          </p>
        )}
      </div>
    </div>
  );
}
