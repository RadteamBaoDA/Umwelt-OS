'use client';

import { useTranslations } from 'next-intl';
import React, { useState } from 'react';
import { Button } from '@/components/ui/button';

/** Result of {@link useShowMore}. */
export interface ShowMore<T> {
  /** Items to render: the first `initial` while collapsed, all of them once expanded. */
  visible: T[];
  /** Items hidden while collapsed (0 when expanded or when everything already fits). */
  hiddenCount: number;
  /** Whether the list is expanded in place. */
  expanded: boolean;
  /** Flips between collapsed and expanded. */
  toggle: () => void;
}

/**
 * In-place "show N more" state for a gadget list. Collapses again whenever `resetKey` changes (filters, source or
 * time window), so a new result set never starts expanded.
 *
 * @param items Full list.
 * @param initial Count shown while collapsed.
 * @param resetKey Any value whose change should collapse the list.
 * @returns Visible slice, hidden count, expanded flag and toggle.
 */
export function useShowMore<T>(items: T[], initial = 5, resetKey: unknown = ''): ShowMore<T> {
  // Expansion is remembered together with the key it was made under, so a key change reads as collapsed without an effect.
  const [open, setOpen] = useState<{ key: unknown } | null>(null);
  const expanded = open !== null && Object.is(open.key, resetKey);
  const hiddenCount = Math.max(0, items.length - initial);
  return {
    visible: expanded ? items : items.slice(0, initial),
    hiddenCount: expanded ? 0 : hiddenCount,
    expanded,
    toggle: () => setOpen(expanded ? null : { key: resetKey }),
  };
}

/**
 * Ghost button toggling a {@link useShowMore} list. Renders nothing when the list never exceeded its initial size.
 *
 * @param props `state` from useShowMore.
 */
export function ShowMoreButton({ state }: { state: Pick<ShowMore<unknown>, 'hiddenCount' | 'expanded' | 'toggle'> }) {
  const t = useTranslations('dashboard');
  // hiddenCount is 0 when expanded, so an expanded list still needs its collapse control.
  if (!state.expanded && state.hiddenCount === 0) return null;
  return (
    <Button type="button" variant="ghost" size="sm" className="self-center" aria-expanded={state.expanded} onClick={state.toggle}>
      {state.expanded ? t('showLess') : t('showMore', { count: state.hiddenCount })}
    </Button>
  );
}
