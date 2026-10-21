'use client';

import * as React from 'react';
import { useQuery } from '@tanstack/react-query';
import { useTranslations } from 'next-intl';
import { apiRequest } from '@/core/api';
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
  onChange: (option: ChatModelOption | null) => void;
}

type AIProviders = { providers?: { id: string; label?: string; models?: { id: string; type: string }[] }[] };

const STORAGE_KEY = 'umwelt.chat.model';
const DEFAULT_VALUE = '__default__';
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
 * @returns Picker state for the composer, or `undefined` while no chat model is configured.
 */
export function useChatModelPicker(): ChatModelPicker | undefined {
  // Same cache key as the settings page, so saving provider settings refreshes the list.
  const query = useQuery({ queryKey: ['ai-settings'], queryFn: () => apiRequest<AIProviders>('/api/v1/settings/ai') });
  const [stored, setStored] = React.useState<string | null>(readStored); // lazy: options load async, so SSR/hydration never shows it

  const options = React.useMemo<ChatModelOption[]>(
    () => (query.data?.providers ?? []).flatMap((p) => (p.models ?? [])
      .filter((m) => m.type === 'llm')
      .map((m) => ({ providerId: p.id, model: m.id, label: `${p.label || p.id} · ${m.id}` }))),
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
  return options.length ? { options, value, onChange } : undefined;
}

/**
 * Compact select listing the owner's default plus each configured chat model.
 *
 * @param props - Picker state from `useChatModelPicker`, and whether the control is disabled.
 * @returns A labelled Radix select.
 */
export function ModelPicker({ picker, disabled = false }: { picker: ChatModelPicker; disabled?: boolean }) {
  const t = useTranslations('chat');
  return (
    <Select
      disabled={disabled}
      value={picker.value ? refOf(picker.value) : DEFAULT_VALUE}
      onValueChange={(next) => picker.onChange(picker.options.find((o) => refOf(o) === next) ?? null)}
    >
      <SelectTrigger aria-label={t('modelPickerLabel')} className="h-8 w-auto max-w-48 min-w-0 text-xs">
        <SelectValue />
      </SelectTrigger>
      <SelectContent>
        <SelectItem value={DEFAULT_VALUE}>{t('modelDefault')}</SelectItem>
        {picker.options.map((o) => <SelectItem key={refOf(o)} value={refOf(o)}>{o.label}</SelectItem>)}
      </SelectContent>
    </Select>
  );
}
