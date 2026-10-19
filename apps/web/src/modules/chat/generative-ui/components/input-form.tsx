'use client';

import * as React from 'react';
import { CheckCircle2Icon, ClipboardListIcon } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from '@/components/ui/card';
import { Checkbox } from '@/components/ui/checkbox';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { Textarea } from '@/components/ui/textarea';
import type { AppLocaleId } from '@/core/i18n';
import { FORM_COPY, resolveWidgetLocale } from '../copy';
import type { InputFormField } from './payload-schemas';

/** Largest prompt the host bridge accepts (`MAX_PROMPT_LENGTH` in the bridge). */
const MAX_PROMPT_CHARS = 4000;
/** Client default text limits when the model gave no `maxlength` (textarea is larger). */
const DEFAULT_TEXT_MAX = 255;
const DEFAULT_TEXTAREA_MAX = 2000;

type FormCopy = (typeof FORM_COPY)[AppLocaleId];
type FormValues = Record<string, string | boolean>;

/** Props of the functional generic form widget. */
export interface InputFormProps {
  /** Form heading. */
  title: string;
  /** Optional helper text under the heading. */
  description?: string;
  /** Validated fields (server limits are mirrored by `inputFormPayloadSchema`). */
  fields: InputFormField[];
  /** Submit button text; empty uses the localized default. */
  submitLabel?: string;
  /** Disables all inputs (used while the tool call is still streaming). */
  disabled?: boolean;
  /** Application locale. */
  locale?: AppLocaleId;
  /** Host bridge; may return a promise whose rejection leaves the form retryable. */
  onSendPrompt?: (prompt: string) => void | Promise<unknown>;
}

/**
 * Returns the character limit that applies to a text-like field.
 *
 * @param field - Form field.
 * @returns Explicit `maxlength` or the type default.
 */
function textLimit(field: InputFormField): number {
  return field.maxlength ?? (field.type === 'textarea' ? DEFAULT_TEXTAREA_MAX : DEFAULT_TEXT_MAX);
}

/**
 * Validates every field against the same limits the backend enforces on the schema.
 *
 * @param fields - Form fields.
 * @param values - Current raw values keyed by field id.
 * @param copy - Localized messages.
 * @returns Error message per invalid field id (empty when valid).
 */
export function validateFormValues(
  fields: InputFormField[],
  values: FormValues,
  copy: FormCopy,
): Record<string, string> {
  const errors: Record<string, string> = {};
  for (const f of fields) {
    const v = values[f.id];
    const text = typeof v === 'string' ? v.trim() : '';
    if (f.type === 'checkbox') {
      if (f.required && v !== true) errors[f.id] = copy.required(f.label);
      continue;
    }
    if (!text) {
      if (f.required) errors[f.id] = copy.required(f.label);
      continue;
    }
    if (f.type === 'number') {
      const n = Number(text);
      if (!Number.isFinite(n)) errors[f.id] = copy.numberInvalid(f.label);
      else if (f.min != null && n < f.min) errors[f.id] = copy.min(f.label, f.min);
      else if (f.max != null && n > f.max) errors[f.id] = copy.max(f.label, f.max);
    } else if (f.type === 'select') {
      if (!f.options.includes(text)) errors[f.id] = copy.required(f.label);
    } else if ((f.type === 'text' || f.type === 'textarea') && text.length > textLimit(f)) {
      errors[f.id] = copy.maxLength(f.label, textLimit(f));
    }
  }
  return errors;
}

/**
 * Composes the deterministic plain-text follow-up message for a submitted form.
 *
 * @param title - Form title.
 * @param fields - Form fields in display order.
 * @param values - Raw values keyed by field id.
 * @param copy - Localized messages.
 * @returns Localized "Form submitted: title" header then one `- label: value` line per field.
 */
export function composeFormPrompt(
  title: string,
  fields: InputFormField[],
  values: FormValues,
  copy: FormCopy,
): string {
  const lines = fields.map((f) => {
    const v = values[f.id];
    const shown = f.type === 'checkbox' ? (v === true ? copy.yes : copy.no) : String(v ?? '').trim() || copy.empty;
    return `- ${f.label}: ${shown}`;
  });
  return [copy.promptHeader(title), ...lines].join('\n');
}

/**
 * Functional generic form widget: client-side validation, one-shot submit through the prompt
 * bridge, locked once accepted, retryable when the bridge rejects.
 *
 * @param props - `InputFormProps`.
 * @returns Accessible form card.
 */
export function InputForm({ title, description, fields, submitLabel, disabled, locale, onSendPrompt }: InputFormProps) {
  const copy = FORM_COPY[resolveWidgetLocale(locale)];
  const uid = React.useId();
  const [values, setValues] = React.useState<FormValues>(() =>
    Object.fromEntries(fields.map((f) => [f.id, f.type === 'checkbox' ? false : ''])),
  );
  const [errors, setErrors] = React.useState<Record<string, string>>({});
  const [phase, setPhase] = React.useState<'idle' | 'sending' | 'sent'>('idle');
  const [sendError, setSendError] = React.useState<string | null>(null);
  const locked = disabled || phase !== 'idle';

  /** Stores one field value and clears its stale error. */
  const setValue = (id: string, value: string | boolean) => {
    setValues((prev) => ({ ...prev, [id]: value }));
    setErrors((prev) => (prev[id] ? { ...prev, [id]: '' } : prev));
  };

  /**
   * Validates, sends the composed prompt and awaits the bridge; a rejection unlocks the form
   * with a retryable error, success locks it in the submitted state.
   */
  const handleSubmit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (locked) return;
    const found = validateFormValues(fields, values, copy);
    setErrors(found);
    if (Object.keys(found).length > 0) return;
    const prompt = composeFormPrompt(title, fields, values, copy);
    if (prompt.length > MAX_PROMPT_CHARS) {
      setSendError(copy.tooLong);
      return;
    }
    setSendError(null);
    setPhase('sending');
    try {
      await onSendPrompt?.(prompt);
      setPhase('sent');
    } catch {
      setPhase('idle');
      setSendError(copy.sendFailed);
    }
  };

  return (
    <Card data-testid="generative-input-form" className="my-2 w-full max-w-md border-border bg-surface text-foreground shadow-sm">
      <form onSubmit={handleSubmit} noValidate aria-label={title}>
        <CardHeader className="gap-1.5 border-b border-border pb-4">
          <div className="flex items-center gap-2">
            <span className="inline-flex size-8 items-center justify-center rounded-lg bg-primary/10 text-primary">
              <ClipboardListIcon className="size-4" aria-hidden="true" />
            </span>
            <div>
              <CardTitle className="text-base font-semibold">{title}</CardTitle>
              {description ? <CardDescription className="mt-0.5 text-xs text-muted-foreground">{description}</CardDescription> : null}
            </div>
          </div>
        </CardHeader>
        <CardContent className="space-y-4 pt-4">
          {fields.map((f) => {
            const id = `${uid}-${f.id}`;
            const errId = `${id}-err`;
            const err = errors[f.id];
            const common = {
              id,
              disabled: locked,
              'aria-invalid': err ? true : undefined,
              'aria-describedby': err ? errId : undefined,
              'aria-required': f.required || undefined,
            } as const;
            const label = (
              <Label htmlFor={id}>
                {f.label}
                {f.required ? null : <span className="ml-1 text-xs font-normal text-muted-foreground">({copy.optional})</span>}
              </Label>
            );
            const error = err ? <p id={errId} role="alert" className="text-xs text-destructive">{err}</p> : null;
            if (f.type === 'checkbox') {
              return (
                <div key={f.id} className="space-y-1">
                  <div className="flex items-center gap-2">
                    <Checkbox {...common} checked={values[f.id] === true} onCheckedChange={(c) => setValue(f.id, c === true)} />
                    {label}
                  </div>
                  {error}
                </div>
              );
            }
            let control: React.ReactNode;
            if (f.type === 'textarea') {
              control = <Textarea {...common} value={String(values[f.id])} placeholder={f.placeholder} maxLength={textLimit(f)} onChange={(e) => setValue(f.id, e.target.value)} />;
            } else if (f.type === 'select') {
              control = (
                <Select value={String(values[f.id])} onValueChange={(v) => setValue(f.id, v)} disabled={locked}>
                  <SelectTrigger {...common}><SelectValue placeholder={f.placeholder || copy.selectPlaceholder} /></SelectTrigger>
                  <SelectContent>{f.options.map((o) => <SelectItem key={o} value={o}>{o}</SelectItem>)}</SelectContent>
                </Select>
              );
            } else {
              control = (
                <Input
                  {...common}
                  type={f.type}
                  value={String(values[f.id])}
                  placeholder={f.placeholder}
                  maxLength={f.type === 'text' ? textLimit(f) : undefined}
                  min={f.type === 'number' ? (f.min ?? undefined) : undefined}
                  max={f.type === 'number' ? (f.max ?? undefined) : undefined}
                  onChange={(e) => setValue(f.id, e.target.value)}
                />
              );
            }
            return (
              <div key={f.id} className="space-y-1.5">
                {label}
                {control}
                {error}
              </div>
            );
          })}
          {phase === 'sent' ? (
            <p role="status" data-testid="form-submitted-banner" className="flex items-center gap-2 rounded-lg border border-primary/30 bg-primary/10 px-3 py-2 text-xs font-medium text-primary">
              <CheckCircle2Icon className="size-4 shrink-0" aria-hidden="true" />
              {copy.submitted}
            </p>
          ) : null}
          {sendError ? <p role="alert" data-testid="form-send-error" className="text-xs text-destructive">{sendError}</p> : null}
        </CardContent>
        <CardFooter className="justify-end border-t border-border pt-4">
          <Button type="submit" size="sm" disabled={locked} data-testid="form-submit-button">
            {phase === 'sending' ? copy.sending : submitLabel || copy.defaultSubmit}
          </Button>
        </CardFooter>
      </form>
    </Card>
  );
}
