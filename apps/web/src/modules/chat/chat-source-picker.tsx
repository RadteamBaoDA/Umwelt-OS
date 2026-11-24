'use client';

import * as React from 'react';
import { useTranslations } from 'next-intl';
import { useQuery } from '@tanstack/react-query';
import { XIcon } from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';
import { listChatSourceOptions, type ChatSourceOption } from './api';

/** Most sources one turn can be scoped to; the server validates the same bound. */
const MAX_CHIPS = 32;
/** Matching sources shown at once; the list stays short so the composer does not grow tall. */
const MAX_SHOWN = 8;
/**
 * `@` must start the text or follow whitespace, so `foo@bar` never opens the list. The captured query is at most 40
 * characters and cannot contain another `@`.
 */
const MENTION_PATTERN = /(^|\s)@([^\s@]{0,40})$/;

/** A source chosen for the current turn; shown as a removable chip above the textarea. */
export interface SourceChip {
  id: string;
  name: string;
}

/** What the list shows: the data state, or `ready` when options are present. */
export type SourceListState = 'loading' | 'error' | 'empty' | 'nomatch' | 'ready';

/** Value returned by {@link useSourceMention}. */
export interface SourceMention {
  /** True while the `@` trigger is active and the list is visible. */
  open: boolean;
  /** Text typed after `@` (may be empty). */
  query: string;
  /** Matching sources not yet chosen, at most {@link MAX_SHOWN}. */
  options: ChatSourceOption[];
  /** Highlighted option index, clamped to the current options. */
  activeIndex: number;
  listState: SourceListState;
  /** Id of the listbox element; the textarea points `aria-controls` at it while open. */
  listboxId: string;
  /** Stable id of the option at `index`, referenced by `aria-activedescendant`. */
  optionId: (index: number) => string;
  /** Chosen sources for this turn, in pick order. */
  chips: SourceChip[];
  /**
   * Keyboard handler for the composer textarea. Returns true when the key was consumed (the caller must stop, so Enter
   * does not send); false when the list is closed or the key is not one the list handles.
   */
  onKeyDown: (e: React.KeyboardEvent<HTMLTextAreaElement>) => boolean;
  /** Chooses an option: removes the `@query` token, adds a chip (up to 32) and restores the caret. */
  select: (option: ChatSourceOption) => void;
  /** Removes one chip by source id. */
  remove: (id: string) => void;
  /** Removes every chip; called once the server accepted the send that used them. */
  clear: () => void;
  /** Aria attributes and caret-tracking handlers to spread on the composer textarea. */
  comboboxProps: {
    role: 'combobox';
    'aria-expanded': boolean;
    'aria-controls': string | undefined;
    'aria-activedescendant': string | undefined;
    'aria-autocomplete': 'list';
    onInput: () => void;
    onSelect: () => void;
  };
}

/**
 * Keyboard handling while the list is open. Arrows wrap; Enter or Tab picks the highlighted row; Escape closes the list.
 * With no rows, Enter closes instead of sending and Tab keeps its normal focus move.
 *
 * @param params.open - Whether the list is visible; closed lists consume nothing.
 * @param params.options - Rows currently shown.
 * @param params.activeIndex - Highlighted row (already clamped to `options`).
 * @param params.setActiveIndex - Moves the highlight.
 * @param params.select - Picks a row.
 * @param params.dismiss - Closes the list until the text before the caret changes.
 * @returns The textarea keydown handler; it returns true when it consumed the key.
 */
function useSourceKeyboard({
  open,
  options,
  activeIndex,
  setActiveIndex,
  select,
  dismiss,
}: {
  open: boolean;
  options: ChatSourceOption[];
  activeIndex: number;
  setActiveIndex: React.Dispatch<React.SetStateAction<number>>;
  select: (option: ChatSourceOption) => void;
  dismiss: () => void;
}) {
  return React.useCallback(
    (e: React.KeyboardEvent<HTMLTextAreaElement>): boolean => {
      // IME composition owns Enter and arrows until it commits.
      if (!open || e.nativeEvent.isComposing) return false;
      const n = options.length;
      switch (e.key) {
        case 'ArrowDown':
          e.preventDefault();
          if (n) setActiveIndex((i) => (i + 1) % n);
          return true;
        case 'ArrowUp':
          e.preventDefault();
          if (n) setActiveIndex((i) => (i - 1 + n) % n);
          return true;
        case 'Enter':
        case 'Tab':
          if (n === 0) {
            if (e.key === 'Tab') return false;
            e.preventDefault();
            dismiss();
            return true;
          }
          e.preventDefault();
          select(options[activeIndex]);
          return true;
        case 'Escape':
          e.preventDefault();
          dismiss();
          return true;
        default:
          return false;
      }
    },
    [open, options, activeIndex, setActiveIndex, select, dismiss],
  );
}

/**
 * Drives the `@` source picker of the full composer: detects an `@query` token before the caret, loads the eligible
 * sources once the list opens, filters them and keeps the chosen sources as chips.
 *
 * @param params.value - Current draft text (controlled by the caller).
 * @param params.setValue - Writes the draft after a token is removed.
 * @param params.textareaRef - The composer textarea; read for caret position and used to restore it after a pick.
 * @param params.enabled - False disables the trigger (selection/gadget context, attachments, another profile, edit mode).
 * @param params.scopeKey - Identity of the conversation the chips belong to (null for a new chat). Any change clears the
 * chosen chips, so New chat or switching threads never carries a previous thread's sources into the next send.
 * @returns The picker state, handlers and textarea aria props; see {@link SourceMention}.
 * @remarks The caret is tracked in state from `input` and `select` events so the token can be found on render. A
 * dismissed token (Escape) stays closed until the text before the caret changes. Chosen chips are cleared on the render
 * where `enabled` turns false: chips chosen under one scope must never ride a send that cannot honour them (specialist
 * runs, edit and regenerate reuse the original run's context). The reset happens during render, not in an effect, so
 * no send can read stale chips in between.
 */
export function useSourceMention({
  value,
  setValue,
  textareaRef,
  enabled,
  scopeKey = null,
}: {
  value: string;
  setValue: (next: string) => void;
  textareaRef: React.RefObject<HTMLTextAreaElement | null>;
  enabled: boolean;
  scopeKey?: string | null;
}): SourceMention {
  const listId = React.useId();
  const [caret, setCaret] = React.useState(0);
  const [dismissedBefore, setDismissedBefore] = React.useState<string | null>(null);
  const [activeIndex, setActiveIndex] = React.useState(0);
  const [chips, setChips] = React.useState<SourceChip[]>([]);
  const [wasEnabled, setWasEnabled] = React.useState(enabled);
  const [seenScopeKey, setSeenScopeKey] = React.useState(scopeKey);
  // Caret to apply once the textarea shows the value that removed the token (a pick changes the value).
  const pendingCaretRef = React.useRef<number | null>(null);

  // Adjust state while rendering (not in an effect) so the disabled render already has no chips.
  if (wasEnabled !== enabled) {
    setWasEnabled(enabled);
    if (!enabled) setChips([]);
  }
  // A different conversation (or New chat) must not inherit the previous thread's chosen sources.
  if (seenScopeKey !== scopeKey) {
    setSeenScopeKey(scopeKey);
    setChips([]);
  }

  const before = value.slice(0, caret);
  const match = MENTION_PATTERN.exec(before);
  const open = enabled && match !== null && dismissedBefore !== before;
  const query = match?.[2] ?? '';
  const atIndex = match ? match.index + match[1].length : -1;

  const chosenIds = React.useMemo(() => chips.map((chip) => chip.id), [chips]);
  const { options, listState } = useSourceOptions({ open, query, chosenIds });
  const clampedIndex = Math.min(activeIndex, Math.max(options.length - 1, 0));

  React.useLayoutEffect(() => {
    const pos = pendingCaretRef.current;
    const el = textareaRef.current;
    if (pos === null || !el) return;
    pendingCaretRef.current = null;
    el.setSelectionRange(pos, pos);
  }, [value, textareaRef]);

  const syncCaret = React.useCallback(() => {
    const el = textareaRef.current;
    if (el) setCaret(el.selectionStart ?? el.value.length);
  }, [textareaRef]);

  const dismiss = React.useCallback(() => setDismissedBefore(before), [before]);

  const select = React.useCallback(
    (option: ChatSourceOption) => {
      if (atIndex < 0) return;
      // Remove the `@query` token itself; the text after the caret stays as typed.
      setValue(value.slice(0, atIndex) + value.slice(caret));
      pendingCaretRef.current = atIndex;
      setCaret(atIndex);
      setActiveIndex(0);
      setChips((prev) =>
        prev.length >= MAX_CHIPS || prev.some((c) => c.id === option.id) ? prev : [...prev, { id: option.id, name: option.name }],
      );
    },
    [atIndex, caret, setValue, value],
  );

  const remove = React.useCallback((id: string) => setChips((prev) => prev.filter((c) => c.id !== id)), []);
  const clear = React.useCallback(() => setChips([]), []);

  const onKeyDown = useSourceKeyboard({ open, options, activeIndex: clampedIndex, setActiveIndex, select, dismiss });

  const listboxId = `${listId}-listbox`;
  const optionId = (index: number) => `${listId}-option-${index}`;
  const activeId = open && options.length > 0 ? optionId(clampedIndex) : undefined;

  return {
    open,
    query,
    options,
    activeIndex: clampedIndex,
    listState,
    listboxId,
    optionId,
    chips,
    onKeyDown,
    select,
    remove,
    clear,
    comboboxProps: {
      role: 'combobox',
      'aria-expanded': open,
      'aria-controls': open ? listboxId : undefined,
      'aria-activedescendant': activeId,
      'aria-autocomplete': 'list',
      onInput: syncCaret,
      onSelect: syncCaret,
    },
  };
}

/**
 * Loads the eligible sources while the list is open and filters them for display: case-insensitive substring match on
 * the name, chosen sources excluded, at most {@link MAX_SHOWN} rows.
 *
 * @param params.open - Whether the list is visible; the query only runs while it is.
 * @param params.query - Text typed after `@`.
 * @param params.chosenIds - Ids of sources already chosen; they are not offered again.
 * @returns The rows to show and the state the list renders (loading, error, empty, no match or ready).
 */
function useSourceOptions({ open, query, chosenIds }: { open: boolean; query: string; chosenIds: string[] }) {
  const sources = useQuery({
    queryKey: ['chat', 'source-options'],
    queryFn: ({ signal }) => listChatSourceOptions(signal),
    enabled: open,
    staleTime: 60_000,
  });
  const data = sources.data;
  const needle = query.toLowerCase();
  const options = React.useMemo(() => {
    const chosen = new Set(chosenIds);
    return (data ?? []).filter((s) => !chosen.has(s.id) && s.name.toLowerCase().includes(needle)).slice(0, MAX_SHOWN);
  }, [data, chosenIds, needle]);
  const listState = resolveListState(sources, data?.length ?? 0, options.length);
  return { options, listState };
}

/**
 * Maps the source query state to what the list shows. Error is checked first so a failed refetch is not hidden behind
 * stale data.
 *
 * @param sources - The source query (only its loading and error flags are read).
 * @param total - Number of eligible sources returned.
 * @param shown - Options left after filtering and the chip exclusion.
 * @returns The state the list renders.
 */
function resolveListState(sources: { isError: boolean; isPending: boolean }, total: number, shown: number): SourceListState {
  if (sources.isError) return 'error';
  if (sources.isPending) return 'loading';
  if (total === 0) return 'empty';
  return shown === 0 ? 'nomatch' : 'ready';
}

/**
 * The listbox of the `@` picker. Absolutely positioned above the composer box so it never steals focus: rows pick on
 * mouse-down with the default prevented, which keeps the textarea focused.
 *
 * @param props.mention - The picker state from {@link useSourceMention}.
 * @returns The listbox while the picker is open; null otherwise.
 */
export function SourceMentionList({ mention }: { mention: SourceMention }) {
  const t = useTranslations('chat');
  if (!mention.open) return null;
  return (
    <div
      id={mention.listboxId}
      role="listbox"
      aria-label={t('sourceScope.listLabel')}
      data-testid="chat-source-list"
      className="absolute inset-x-0 bottom-full z-50 mb-2 max-h-64 overflow-y-auto rounded-lg border bg-popover p-1 text-popover-foreground shadow-[var(--shadow-popover)]"
    >
      <SourceListBody mention={mention} />
    </div>
  );
}

/**
 * Rows of the picker list, or the one-line status that replaces them (loading, error, no sources yet, no match).
 *
 * @param props.mention - The picker state from {@link useSourceMention}.
 */
function SourceListBody({ mention }: { mention: SourceMention }) {
  const t = useTranslations('chat');
  if (mention.listState === 'loading') return <p className="px-2 py-1.5 text-sm text-muted-foreground">{t('sourceScope.loading')}</p>;
  if (mention.listState === 'error') return <p className="px-2 py-1.5 text-sm text-destructive">{t('sourceScope.error')}</p>;
  if (mention.listState === 'empty') return <p className="px-2 py-1.5 text-sm text-muted-foreground">{t('sourceScope.noneYet')}</p>;
  if (mention.listState === 'nomatch') {
    return <p className="px-2 py-1.5 text-sm text-muted-foreground">{t('sourceScope.noMatch', { query: mention.query })}</p>;
  }
  return (
    <>
      {mention.options.map((option, index) => (
        <div
          key={option.id}
          id={mention.optionId(index)}
          role="option"
          aria-selected={index === mention.activeIndex}
          onMouseDown={(e) => {
            // Keep focus in the textarea; the pick is applied on press so the list cannot close first.
            e.preventDefault();
            mention.select(option);
          }}
          className={cn(
            'flex cursor-default select-none items-center rounded-sm px-2 py-1.5 text-sm',
            index === mention.activeIndex && 'bg-accent text-accent-foreground',
          )}
        >
          {option.name}
        </div>
      ))}
    </>
  );
}

/**
 * Removable chips of the sources chosen for this turn, shown above the composer textarea. Renders nothing when empty.
 *
 * @param props.chips - Chosen sources in pick order.
 * @param props.onRemove - Called with the source id when its remove button is pressed.
 */
export function SourceScopeChips({ chips, onRemove }: { chips: SourceChip[]; onRemove: (id: string) => void }) {
  const t = useTranslations('chat');
  if (chips.length === 0) return null;
  return (
    <div className="flex flex-wrap items-center gap-1.5 text-xs" data-testid="chat-source-scope">
      <span className="text-muted-foreground">{t('sourceScope.label')}</span>
      {chips.map((chip) => (
        <Badge key={chip.id} variant="secondary" className="min-h-6 max-w-full gap-1 py-0.5 pl-2 pr-0">
          <span className="truncate">{chip.name}</span>
          <Button
            type="button"
            variant="ghost"
            size="icon"
            className="size-6"
            aria-label={t('sourceScope.remove', { name: chip.name })}
            onClick={() => onRemove(chip.id)}
          >
            <XIcon className="size-3.5" aria-hidden="true" />
          </Button>
        </Badge>
      ))}
    </div>
  );
}
