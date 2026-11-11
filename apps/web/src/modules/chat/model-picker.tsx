'use client';

import * as React from 'react';
import { useTranslations } from 'next-intl';
import { useAiSettings } from './use-ai-settings';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';

/** One configured chat-capable model the owner may pick for a message. */
export interface ChatModelOption {
  providerId: string;
  model: string;
  label: string;
}

/** Picker state handed to the composer; `value` is `null` for the owner's default chat model. */
export interface ChatModelPicker {
  options: ChatModelOption[];
  value: ChatModelOption | null;
  /** Model id of the owner's default chat model, when settings name one; shown beside the "Default" entry. */
  defaultModel?: string | null;
  onChange: (option: ChatModelOption | null) => void;
}

type AIProviders = {
  providers?: { id: string; label?: string; models?: { id: string; type: string }[] }[];
  defaults?: { chat?: { model: string } | null };
};

const STORAGE_KEY = 'umwelt.chat.model';
const DEFAULT_VALUE = '__default__';
/** Builds a stable provider+model reference key for an option. */
const refOf = (o: { providerId: string; model: string }) => `${o.providerId}\u0000${o.model}`;

/** Reads the remembered pick; storage can be blocked (private window), so any failure means "default". */
function readStored(): string | null {
  try {
    return window.localStorage.getItem(STORAGE_KEY);
  } catch {
    return null;
  }
}

/**
 * Loads the owner's configured `llm` models and tracks the per-chat model choice, remembered in
 * localStorage (preference only). A remembered model that was removed from settings falls back to the default.
 *
 * @returns Picker state for the composer (including the default chat model's name when known), or `undefined` while no chat model is configured.
 */
export function useChatModelPicker(): ChatModelPicker | undefined {
  // Same cache key as the settings page, so saving provider settings refreshes the list.
  const query = useAiSettings<AIProviders>();
  const [stored, setStored] = React.useState<string | null>(readStored); // lazy: options load async, so SSR/hydration never shows it

  const options = React.useMemo<ChatModelOption[]>(
    () =>
      (query.data?.providers ?? []).flatMap((p) =>
        (p.models ?? [])
          .filter((m) => m.type === 'llm')
          .map((m) => ({ providerId: p.id, model: m.id, label: `${p.label || p.id} · ${m.id}` })),
      ),
    [query.data],
  );
  const value = options.find((o) => refOf(o) === stored) ?? null;
  const onChange = React.useCallback((option: ChatModelOption | null) => {
    setStored(option ? refOf(option) : null);
    try {
      if (option) window.localStorage.setItem(STORAGE_KEY, refOf(option));
      else window.localStorage.removeItem(STORAGE_KEY);
    } catch {
      // Preference is best-effort; the in-memory choice still applies.
    }
  }, []);
  return options.length ? { options, value, onChange, defaultModel: query.data?.defaults?.chat?.model ?? null } : undefined;
}

/** Element id of the "specialist agents choose their own model" hint; a locked picker is described by it. */
export const MODEL_LOCKED_HINT_ID = 'chat-model-locked-hint-text';

/**
 * Text of the closed trigger. The model's short name (provider path stripped: `anthropic/claude-haiku-5-5` shows
 * `claude-haiku-5-5`) shows at every width; the "Default ·" / provider prefix is hidden by CSS below `md` so the
 * 390px toolbar stays readable. The full text stays in the trigger's title.
 *
 * @param picker - Picker state; the chosen option, else the default model's name when known.
 * @param defaultPrefix - Localized "Default · " prefix used when no explicit model is chosen.
 * @param fallback - Localized "Default model" text when the default model is unnamed.
 * @returns The trigger's value content.
 */
function triggerText(picker: ChatModelPicker, defaultPrefix: string, fallback: string): { title: string; node: React.ReactNode } {
  const chosen = picker.value;
  const name = chosen ? chosen.model : picker.defaultModel;
  if (!name) return { title: fallback, node: fallback };
  const prefix = chosen ? (chosen.label.endsWith(name) ? chosen.label.slice(0, -name.length) : '') : defaultPrefix;
  // Some providers embed their own path in the model id ("anthropic/claude-…"); show only the last segment.
  const short = name.slice(name.lastIndexOf('/') + 1) || name;
  return {
    title: `${prefix}${name}`,
    node: (
      <>
        {prefix && <span className="hidden md:inline">{prefix}</span>}
        {short}
      </>
    ),
  };
}

/**
 * Compact select listing the owner's default plus each configured chat model.
 *
 * @param props - Picker state from `useChatModelPicker`, whether the control is disabled and whether an agent locks it (then the lock hint describes it).
 * @returns A labelled Radix select.
 */
export function ModelPicker({
  picker,
  disabled = false,
  locked = false,
}: {
  picker: ChatModelPicker;
  disabled?: boolean;
  locked?: boolean;
}) {
  const t = useTranslations('chat');
  const text = triggerText(picker, t('modelDefaultPrefix'), t('modelDefault'));
  return (
    <Select
      disabled={disabled || locked}
      value={picker.value ? refOf(picker.value) : DEFAULT_VALUE}
      onValueChange={(next) => picker.onChange(picker.options.find((o) => refOf(o) === next) ?? null)}
    >
      <SelectTrigger
        aria-label={t('modelPickerLabel')}
        aria-describedby={locked ? MODEL_LOCKED_HINT_ID : undefined}
        title={text.title}
        className="h-8 w-auto max-w-48 min-w-0 border-0 bg-transparent text-xs text-muted-foreground shadow-none hover:bg-active"
      >
        <SelectValue>{text.node}</SelectValue>
      </SelectTrigger>
      <SelectContent>
        <SelectItem value={DEFAULT_VALUE}>
          {picker.defaultModel ? t('modelDefaultNamed', { model: picker.defaultModel }) : t('modelDefault')}
        </SelectItem>
        {picker.options.map((o) => (
          <SelectItem key={refOf(o)} value={refOf(o)}>
            {o.label}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}
