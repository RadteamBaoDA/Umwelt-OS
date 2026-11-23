'use client';

import { useState } from 'react';
import { useContentTranslation, type ContentTranslation, type TranslationTarget } from './use-content-translation';
import type { ResourceType, TranslationPayload } from './types';

/** State and handlers for on-demand collection/per-item translation with global and per-item Original toggles. */
export type ItemTranslationController = ContentTranslation & {
  /** All translatable targets currently mounted in the collection. */
  targets: readonly TranslationTarget[];
  /** True when the collection-wide toggle is currently showing original text. */
  showOriginal: boolean;
  /** Flips the collection-wide Original/Translation toggle. */
  toggleAllOriginal: () => void;
  /** True when a specific item is currently showing its original text. */
  isShowingOriginal: (id: string) => boolean;
  /** Flips one item between original and translated text without affecting other items. */
  toggleItemOriginal: (id: string) => void;
  /** Ready translation payload to display for an item, or undefined when showing original or untranslated. */
  shown: (id: string) => TranslationPayload | undefined;
  /** Requests on-demand translation for one item and ensures its translation is shown once ready. */
  translateOne: (target: TranslationTarget) => void;
};

/**
 * Composes `useContentTranslation` with collection-wide and per-item Original/Translation toggles.
 * Nothing is translated on mount; callers wire `translateOne` to explicit per-item buttons.
 *
 * @param type Resource type supported by the translation API.
 * @param targets Currently loaded resource references (id and revision).
 * @returns Translation state, display helper `shown(id)`, and on-demand triggers.
 */
export function useItemTranslation(type: ResourceType, targets: TranslationTarget[]): ItemTranslationController {
  const translation = useContentTranslation(type, targets);
  const [showOriginal, setShowOriginal] = useState(false);
  const [perItemOriginal, setPerItemOriginal] = useState<ReadonlySet<string>>(() => new Set());

  const isShowingOriginal = (id: string) => (perItemOriginal.has(id) ? !showOriginal : showOriginal);

  const toggleAllOriginal = () => setShowOriginal((prev) => !prev);

  const toggleItemOriginal = (id: string) => {
    setPerItemOriginal((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const shown = (id: string) => (isShowingOriginal(id) ? undefined : (translation.results.get(id)?.translation ?? undefined));

  const translateOne = (target: TranslationTarget) => {
    if (!target.id || !target.revision) return;
    setPerItemOriginal((prev) => {
      const next = new Set(prev);
      if (showOriginal) next.add(target.id);
      else next.delete(target.id);
      return next;
    });
    translation.translate([target]);
  };

  return {
    ...translation,
    targets,
    showOriginal,
    toggleAllOriginal,
    isShowingOriginal,
    toggleItemOriginal,
    shown,
    translateOne,
  };
}
