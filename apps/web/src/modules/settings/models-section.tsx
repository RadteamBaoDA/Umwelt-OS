'use client';

import { Check, Plus, Trash2 } from 'lucide-react';
import { useState } from 'react';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { SearchableSelect } from '@/components/ui/searchable-select';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import { guessModelType, mappingProviderId, modelTestKey, ModelTestChip, selectableModelTypes, testRoleByType, type ModelTestState, type ModelType, type ProviderModel, type ProviderRead } from './provider-editor';
import { FieldGrid } from './settings-layout';

type Translate = (key: string, values?: Record<string, string | number>) => string;
/** Stored mapping: which provider model serves one role. */
export type Mapping = { model: string; version: string | null; destination: 'unknown' | 'remote'; provider_id?: string | null; dimensions?: number | null };
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

/** Clears every default still pointing at a model that was just removed, so no mapping dangles. */
export function pruneDefaults(prev: ProviderRead[], next: ProviderRead[], defaults: Defaults): Defaults {
  const live = new Set(next.flatMap((p) => (p.models ?? []).map((m) => modelTestKey(p.id, m.id))));
  const gone = new Set(prev.flatMap((p) => (p.models ?? []).map((m) => modelTestKey(p.id, m.id))).filter((key) => !live.has(key)));
  return Object.fromEntries(Object.entries(defaults).map(([role, m]) => [role, m?.model && gone.has(modelTestKey(mappingProviderId(m.provider_id, prev), m.model)) ? null : m]));
}

type ModelsProps = { providers: ProviderRead[]; discovered: Record<string, string[]>; modelTests: Record<string, ModelTestState>; checkedAt?: Record<string, string>; disabled: boolean; onChange: (providers: ProviderRead[]) => void; onTestModel: (provider: ProviderRead, model: ProviderModel) => void; t: Translate };

/** The single Models area: add row (provider, discovered model, type, vision) and one table of every added model. */
export function ModelsPanel({ providers, discovered, modelTests, checkedAt = {}, disabled, onChange, onTestModel, t }: ModelsProps) {
  const [providerId, setProviderId] = useState('');
  const [pending, setPending] = useState('');
  const [type, setType] = useState<ModelType>('llm');
  const [vision, setVision] = useState(false);
  const provider = providers.find((item) => item.id === providerId) ?? providers[0];
  const added = new Set((provider?.models ?? []).map((item) => item.id));
  const options = (provider ? discovered[provider.id] ?? [] : []).filter((id) => !added.has(id));
  const rows = providers.flatMap((item) => (item.models ?? []).map((model) => ({ provider: item, model })));
  /** Replaces one provider's model list. */
  const patch = (id: string, models: ProviderModel[]) => onChange(providers.map((item) => (item.id === id ? { ...item, models } : item)));
  return <div className="flex flex-col gap-3">
    <div className="grid items-end gap-2 sm:grid-cols-[10rem_1fr_12rem_auto]">
      <div className="field"><span className="label">{t('providerColumn')}</span>
        <Select disabled={disabled || providers.length === 0} value={provider?.id ?? ''} onValueChange={(id) => { setProviderId(id); setPending(''); }}><SelectTrigger aria-label={t('providerColumn')}><SelectValue placeholder={t('providersEmpty')} /></SelectTrigger><SelectContent>{providers.map((item) => <SelectItem key={item.id} value={item.id}>{item.label || item.id}</SelectItem>)}</SelectContent></Select></div>
      <div className="field"><span className="label">{t('modelColumn')}</span>
        <SearchableSelect aria-label={t('modelColumn')} value={pending} options={options} disabled={disabled || options.length === 0} placeholder={t('selectModel')} searchLabel={t('modelPickerSearch')} emptyLabel={t('modelPickerEmpty')} onValueChange={(id) => {
          // Legacy guesses (vision/asr/tts) fold into Chat; a vision-looking id also pre-ticks the vision flag.
          const guess = guessModelType(id);
          setPending(id); setType((selectableModelTypes as readonly string[]).includes(guess) ? guess : 'llm'); setVision(guess === 'vision');
        }} /></div>
      <div className="field"><span className="label">{t('typeColumn')}</span>
        <Select disabled={disabled} value={type} onValueChange={(next) => setType(next as ModelType)}><SelectTrigger aria-label={t('typeColumn')}><SelectValue /></SelectTrigger><SelectContent>{selectableModelTypes.map((item) => <SelectItem key={item} value={item}>{t(`type_${item}`)}</SelectItem>)}</SelectContent></Select></div>
      <Button type="button" variant="outline" disabled={disabled || !provider || !pending} onClick={() => { patch(provider!.id, [...(provider!.models ?? []), type === 'llm' && vision ? { id: pending, type, vision: true } : { id: pending, type }]); setPending(''); setVision(false); }}><Plus className="size-4" aria-hidden="true" /> {t('modelAdd')}</Button>
      {type === 'llm' && <label className="flex items-center gap-2 text-sm sm:col-span-4"><Checkbox disabled={disabled} checked={vision} onCheckedChange={(next) => setVision(next === true)} />{t('modelVision')}</label>}
    </div>
    {rows.length === 0 ? <p className="text-muted-foreground m-0">{t('modelsEmpty')}</p> : <Table aria-label={t('modelsTable')}>
      <TableHeader><TableRow><TableHead>{t('providerColumn')}</TableHead><TableHead>{t('modelColumn')}</TableHead><TableHead>{t('typeColumn')}</TableHead><TableHead>{t('visionColumn')}</TableHead><TableHead>{t('actionsColumn')}</TableHead></TableRow></TableHeader>
      <TableBody>{rows.map(({ provider: owner, model }) => {
        const state = modelTests[modelTestKey(owner.id, model.id)];
        return <TableRow key={modelTestKey(owner.id, model.id)}>
          <TableCell>{owner.label || owner.id}</TableCell>
          <TableCell className="break-all whitespace-normal">{model.id}</TableCell>
          <TableCell>{t(`type_${model.type}`)}</TableCell>
          <TableCell>{model.vision ? <><Check className="size-4" aria-hidden="true" /><span className="sr-only">{t('modelVision')}</span></> : <span className="text-muted-foreground" aria-hidden="true">–</span>}</TableCell>
          <TableCell><div className="flex flex-wrap items-center gap-2">
            <Button type="button" variant="outline" disabled={disabled || state === 'pending' || !testRoleByType[model.type] || !owner.base_url} aria-label={t('modelTestAria', { id: model.id })} onClick={() => onTestModel(owner, model)}>{t('modelTest')}</Button>
            <Button type="button" variant="outline" disabled={disabled} aria-label={t('modelRemove', { id: model.id })} onClick={() => patch(owner.id, (owner.models ?? []).filter((item) => item.id !== model.id))}><Trash2 className="size-4" aria-hidden="true" /></Button>
            <ModelTestChip state={state} t={t} />
            {state && state !== 'pending' && checkedAt[modelTestKey(owner.id, model.id)] && <span className="text-muted-foreground text-xs">{t('modelCheckedAt', { time: new Date(checkedAt[modelTestKey(owner.id, model.id)]).toLocaleString() })}</span>}
          </div></TableCell>
        </TableRow>;
      })}</TableBody>
    </Table>}
    <p className="text-muted-foreground m-0 text-xs">{t('modelTypesHelp')}</p>
  </div>;
}

type DefaultsProps = { providers: ProviderRead[]; defaults: Defaults; disabled: boolean; embeddingStatus: 'idle' | 'checking' | 'error'; onPick: (slot: DefaultSlot, provider: ProviderRead | null, model: string) => void; t: Translate };

/** Default-model selects, one per role; each lists only added models of the matching type. Optional roles offer a fallback entry that clears the default. */
export function ModelDefaults({ providers, defaults, disabled, embeddingStatus, onPick, t }: DefaultsProps) {
  const all = providers.flatMap((p) => (p.models ?? []).map((m) => ({ provider: p, model: m })));
  const entries = (filter: (m: ProviderModel) => boolean) => all.filter((e) => filter(e.model));
  const refOf = (m: Mapping | null | undefined) => (m?.model ? modelTestKey(mappingProviderId(m.provider_id, providers), m.model) : '');
  const field = (slot: DefaultSlot, label: string, list: ReturnType<typeof entries>, extra?: { id: string; label: string }) => <label className="field" key={slot}><span className="label">{label}</span>
    <Select disabled={disabled || (list.length === 0 && !extra)} value={refOf(defaults[slot]) || extra?.id || undefined} onValueChange={(next) => {
      if (next === none || next === same) return onPick(slot, null, '');
      const hit = list.find((e) => modelTestKey(e.provider.id, e.model.id) === next);
      if (hit) onPick(slot, hit.provider, hit.model.id);
    }}><SelectTrigger aria-label={label}><SelectValue placeholder={t('selectModel')} /></SelectTrigger><SelectContent>
      {extra && <SelectItem value={extra.id}>{extra.label}</SelectItem>}
      {list.map((e) => <SelectItem key={modelTestKey(e.provider.id, e.model.id)} value={modelTestKey(e.provider.id, e.model.id)}>{`${e.provider.label || e.provider.id} · ${e.model.id}`}</SelectItem>)}
    </SelectContent></Select></label>;
  const chats = entries((m) => m.type === 'llm');
  return <div className="flex flex-col gap-3">
    <h3 className="m-0 text-base font-bold">{t('defaultsTitle')}</h3>
    <p className="text-muted-foreground m-0 text-xs">{t('defaultsHelp')}</p>
    <FieldGrid>
      {field('chat', t('chatModel'), chats)}
      {field('brief', t('briefModel'), chats, { id: same, label: t('sameAsChat') })}
      {field('background', t('backgroundModel'), entries((m) => m.type === 'background' || m.type === 'llm'), { id: same, label: t('sameAsBrief') })}
      {field('embedding', t('embeddingModel'), entries((m) => m.type === 'embedding'))}
      {field('reranker', t('rerankerModel'), entries((m) => m.type === 'reranker'), { id: none, label: t('noneOption') })}
      {field('vision', t('visionModel'), entries((m) => m.type === 'llm' && m.vision === true), { id: none, label: t('noneOption') })}
    </FieldGrid>
    <div role="status" aria-live="polite">
      {embeddingStatus === 'checking' && <p className="text-muted-foreground m-0 text-xs">{t('embeddingChecking')}</p>}
      {embeddingStatus === 'error' && <p className="error m-0 text-xs" role="alert">{t('embeddingProbeFailed')}</p>}
    </div>
  </div>;
}
