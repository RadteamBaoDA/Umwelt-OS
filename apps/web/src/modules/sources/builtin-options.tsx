'use client';

import { useLocale, useTranslations } from 'next-intl';
import { useState } from 'react';
import { Checkbox } from '@/components/ui/checkbox';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Switch } from '@/components/ui/switch';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { minIntervalMinutes, type BuiltinSource, type BuiltinTypeOptions, type ConnectorConfig } from './api';
import { OPTION_SPECS, type OptionSpec } from './builtin-option-specs';

const INTERVALS = [15, 30, 60, 360, 1440] as const;
const MAX_SECTIONS = 6;

/** Owner-editable built-in option values; `name` is create-only and lives outside `ConnectorConfig`. */
export type BuiltinOptionValues = Partial<Pick<ConnectorConfig, 'schedule_interval_minutes' | 'max_items' | 'include_keywords' | 'exclude_keywords' | 'max_age_days' | 'feed_sections'>> & BuiltinTypeOptions & { name?: string };
export type BuiltinOptionKey = keyof BuiltinOptionValues;

/** Splits a comma-separated field into trimmed, non-empty terms (server enforces the 20 x 64 bounds). */
function parseTerms(raw: string): string[] {
  return raw.split(',').map((term) => term.trim()).filter(Boolean);
}

const UNSET = '__default__';

/** Readable fallback for enum codes: underscores become spaces and the first letter is capitalised. */
function humanize(value: string): string {
  const text = value.replace(/_/g, ' ');
  return text.charAt(0).toUpperCase() + text.slice(1);
}

/**
 * Renders one per-type option from its spec: enum Select (with a Default item that clears it), capped checkbox group,
 * bounded number, bounded text or switch. Reports `undefined` (or `[]` for a multi-select) when emptied so callers can
 * omit the key (wizard) or send an explicit null (Configure). Pure UI; the server remains the validator.
 */
function TypedOption({ spec, value, onValue, disabled }: { spec: OptionSpec; value: unknown; onValue: (value: unknown) => void; disabled: boolean }) {
  const t = useTranslations('sources');
  const id = `builtin-option-${spec.key}`;
  const label = t(`optk_${spec.key}` as never);
  if (spec.kind === 'bool') {
    return <div className="field flex items-center gap-2"><Switch id={id} aria-label={label} disabled={disabled} checked={value === true} onCheckedChange={(on) => onValue(on ? true : undefined)} />
      <Label htmlFor={id}>{label}</Label></div>;
  }
  if (spec.kind === 'tags') {
    return <TagsOption id={id} label={label} spec={spec} value={value} disabled={disabled} onValue={onValue} />;
  }
  if (spec.kind === 'multi') {
    const selected = Array.isArray(value) ? (value as string[]) : [];
    const max = spec.max ?? selected.length + 1;
    return <fieldset className="field m-0 border-0 p-0" disabled={disabled}><legend className="mb-1 text-sm font-medium">{label}</legend>
      <div className="grid gap-2 sm:grid-cols-3">{(spec.values ?? []).map((item) => {
        const on = selected.includes(item);
        return <label key={item} className="flex items-center gap-2 text-sm">
          <Checkbox checked={on} disabled={disabled || (!on && selected.length >= max)}
            onCheckedChange={(next) => { const list = next === true ? [...selected, item] : selected.filter((entry) => entry !== item); if (list.length <= max) onValue(list); }} />
          <span>{spec.labelled ? t(`optv_${item}` as never) : item}</span></label>;
      })}</div>
      <small className="text-muted-foreground">{t('optMultiMax', { max })}</small></fieldset>;
  }
  if (spec.kind === 'enum') {
    return <div className="field"><Label htmlFor={id}>{label}</Label>
      <Select disabled={disabled} value={typeof value === 'string' ? value : UNSET} onValueChange={(next) => onValue(next === UNSET ? undefined : next)}>
        <SelectTrigger id={id}><SelectValue /></SelectTrigger>
        <SelectContent><SelectItem value={UNSET}>{t('optSelectUnset')}</SelectItem>
          {(spec.values ?? []).map((item) => <SelectItem key={item} value={item}>{humanize(item)}</SelectItem>)}</SelectContent>
      </Select></div>;
  }
  if (spec.kind === 'text') {
    return <div className="field"><Label htmlFor={id}>{label}</Label>
      <Input id={id} maxLength={spec.maxLen} disabled={disabled} value={typeof value === 'string' ? value : ''}
        onChange={(event) => { const raw = spec.upper ? event.target.value.toUpperCase() : event.target.value; onValue(raw === '' ? undefined : raw); }} /></div>;
  }
  return <div className="field"><Label htmlFor={id}>{label}</Label>
    <Input id={id} type="number" min={spec.min} max={spec.hi} step={spec.step ?? 1} disabled={disabled} value={typeof value === 'number' ? value : ''}
      onChange={(event) => onValue(event.target.value === '' ? undefined : Number(event.target.value))} /></div>;
}

/**
 * Comma-separated terms field (for example VN tickers) that keeps the raw text locally so a trailing comma survives typing.
 * Reports trimmed, de-duplicated terms (upper-cased when the spec asks) capped at `spec.max`, or `undefined` when empty.
 */
function TagsOption({ id, label, spec, value, disabled, onValue }: { id: string; label: string; spec: OptionSpec; value: unknown; disabled: boolean; onValue: (value: unknown) => void }) {
  const t = useTranslations('sources');
  const [raw, setRaw] = useState(Array.isArray(value) ? (value as string[]).join(', ') : '');
  const max = spec.max ?? 20;
  return <div className="field"><Label htmlFor={id}>{label}</Label>
    <Input id={id} disabled={disabled} value={raw} aria-describedby={`${id}-help`} onChange={(event) => {
      const text = spec.upper ? event.target.value.toUpperCase() : event.target.value;
      setRaw(text);
      // Case-insensitive de-duplication: the server rejects keywords that differ only by case.
      const terms = [...new Map(parseTerms(text).map((term) => [term.toLowerCase(), term])).values()].slice(0, max);
      onValue(terms.length ? terms : undefined);
    }} />
    <small id={`${id}-help`} className="text-muted-foreground">{t('optTagsHelp', { max })}</small></div>;
}

/** Keyword input that keeps the raw text locally so a trailing comma survives typing, while reporting parsed terms. */
function KeywordField({ id, label, terms, onTerms }: { id: string; label: string; terms: string[]; onTerms: (terms: string[]) => void }) {
  const t = useTranslations('sources');
  const [raw, setRaw] = useState(terms.join(', '));
  return <div className="field"><Label htmlFor={id}>{label}</Label>
    <Input id={id} value={raw} aria-describedby={`${id}-help`} onChange={(event) => { setRaw(event.target.value); onTerms(parseTerms(event.target.value)); }} />
    <small id={`${id}-help`} className="text-muted-foreground">{t('optKeywordsHelp')}</small></div>;
}

/**
 * Renders only the owner options the server lists in `builtin.applicable_options`: name, schedule, RSS sections,
 * max items, include/exclude keywords, max age and every per-type option in `OPTION_SPECS` (enum, capped multi-select, bounded number/text, switch). Controlled: every edit is reported through `onChange(key, value)`
 * (same shape as ProviderScope); an emptied numeric field reports `undefined` so the caller can omit or clear it.
 * `showName` (create only) and `showSchedule` let Configure keep its own schedule panel and a read-only name.
 * Sections require at least one selection and at most 6, and the cost line estimates requests per day.
 * Changes apply to new fetches only, which the footnote states.
 */
export function BuiltinOptions({ builtin, values, onChange, showName = false, showSchedule = false, disabled = false }: {
  builtin: BuiltinSource;
  values: BuiltinOptionValues;
  onChange: <K extends BuiltinOptionKey>(key: K, value: BuiltinOptionValues[K]) => void;
  showName?: boolean;
  showSchedule?: boolean;
  disabled?: boolean;
}) {
  const t = useTranslations('sources');
  const locale = useLocale();
  const applicable = new Set(builtin.applicable_options ?? []);
  const sections = builtin.sections ?? [];
  const selected = values.feed_sections ?? (builtin.default_section ? [builtin.default_section] : []);
  const floor = minIntervalMinutes(builtin);
  const intervals = INTERVALS.filter((value) => value >= floor);
  const minutes = values.schedule_interval_minutes ?? Math.max(30, floor);
  const requestsPerDay = selected.length * Math.ceil(1440 / minutes);
  const showSections = applicable.has('feed_sections') && sections.length > 0;
  const showFilters = applicable.has('max_items') || applicable.has('include_keywords') || applicable.has('exclude_keywords') || applicable.has('max_age_days');
  const typed = OPTION_SPECS.filter((spec) => applicable.has(spec.key));
  const showNativeHint = typed.some((spec) => spec.native);
  if (typed.length === 0 && !(showName && applicable.has('name')) && !(showSchedule && applicable.has('schedule_interval_minutes')) && !showSections && !showFilters) return null;

  /** Toggles one section, refusing to drop the last one or exceed the cap. */
  const toggle = (slug: string, on: boolean) => {
    const next = on ? [...selected, slug] : selected.filter((item) => item !== slug);
    if (next.length < 1 || next.length > MAX_SECTIONS) return;
    onChange('feed_sections', next);
  };
  /** Reports a bounded integer field, or undefined when emptied. */
  const numeric = (key: 'max_items' | 'max_age_days') => (event: React.ChangeEvent<HTMLInputElement>) => onChange(key, event.target.value === '' ? undefined : Number(event.target.value));

  return <div className="grid gap-3" data-testid="builtin-options">
    {showName && applicable.has('name') && <div className="field"><Label htmlFor="builtin-option-name">{t('optName')}</Label>
      <Input id="builtin-option-name" maxLength={200} disabled={disabled} value={values.name ?? builtin.label} onChange={(event) => onChange('name', event.target.value)} /></div>}
    {showSchedule && applicable.has('schedule_interval_minutes') && <div className="field"><Label htmlFor="builtin-option-schedule">{t('optSchedule')}</Label>
      <Select disabled={disabled} value={values.schedule_interval_minutes ? String(values.schedule_interval_minutes) : undefined} onValueChange={(value) => onChange('schedule_interval_minutes', Number(value) as ConnectorConfig['schedule_interval_minutes'])}>
        <SelectTrigger id="builtin-option-schedule"><SelectValue placeholder={t('optScheduleDefault')} /></SelectTrigger>
        <SelectContent>{intervals.map((value) => <SelectItem key={value} value={String(value)}>{t('everyMinutes', { minutes: value })}</SelectItem>)}</SelectContent>
      </Select>
      {floor > 15 && <small className="text-muted-foreground">{floor >= 60 ? t('minIntervalHours', { hours: floor / 60 }) : t('minIntervalMinutes', { minutes: floor })}</small>}</div>}
    {showSections && <fieldset className="field m-0 border-0 p-0" disabled={disabled}><legend className="mb-1 text-sm font-medium">{t('optSections')}</legend>
      <div className="grid gap-2 sm:grid-cols-2">{sections.map((section) => {
        const label = locale.toLowerCase().startsWith('vi') ? section.label_vi : section.label_en;
        const on = selected.includes(section.slug);
        return <label key={section.slug} className="flex items-center gap-2 text-sm">
          <Checkbox checked={on} disabled={disabled || (!on && selected.length >= MAX_SECTIONS)} onCheckedChange={(next) => toggle(section.slug, next === true)} />
          <span>{label}</span></label>;
      })}</div>
      <small className="text-muted-foreground">{t('optSectionsHelp')}</small>
      <small className="text-muted-foreground" role="status">{t('optSectionsCost', { count: selected.length, minutes, requests: requestsPerDay })}</small></fieldset>}
    {applicable.has('max_items') && <div className="field"><Label htmlFor="builtin-option-max-items">{t('optMaxItems')}</Label>
      <Input id="builtin-option-max-items" type="number" min={1} max={100} disabled={disabled} aria-describedby="builtin-option-max-items-help" value={values.max_items ?? ''} onChange={numeric('max_items')} />
      <small id="builtin-option-max-items-help" className="text-muted-foreground">{t('optMaxItemsHelp')}</small></div>}
    {applicable.has('include_keywords') && <KeywordField id="builtin-option-include" label={t('optInclude')} terms={values.include_keywords ?? []} onTerms={(terms) => onChange('include_keywords', terms)} />}
    {applicable.has('exclude_keywords') && <KeywordField id="builtin-option-exclude" label={t('optExclude')} terms={values.exclude_keywords ?? []} onTerms={(terms) => onChange('exclude_keywords', terms)} />}
    {applicable.has('max_age_days') && <div className="field"><Label htmlFor="builtin-option-max-age">{t('optMaxAge')}</Label>
      <Input id="builtin-option-max-age" type="number" min={1} max={365} disabled={disabled} aria-describedby="builtin-option-max-age-help" value={values.max_age_days ?? ''} onChange={numeric('max_age_days')} />
      <small id="builtin-option-max-age-help" className="text-muted-foreground">{t('optMaxAgeHelp')}</small></div>}
    {typed.map((spec) => <TypedOption key={spec.key} spec={spec} disabled={disabled} value={(values as Record<string, unknown>)[spec.key]}
      onValue={(next) => onChange(spec.key as BuiltinOptionKey, next as never)} />)}
    {showNativeHint && <p className="m-0 text-sm text-muted-foreground" role="note">{t('optNativeHint')}</p>}
    <p className="m-0 text-sm text-muted-foreground">{t('optAppliesNew')}</p>
  </div>;
}
