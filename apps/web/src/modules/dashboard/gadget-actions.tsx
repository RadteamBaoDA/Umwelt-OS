'use client';

import { createContext, useCallback, useContext, useEffect, useMemo, useRef, type ReactNode } from 'react';
import { useTranslations } from 'next-intl';
import { untranslatedTargets, type ItemTranslationController } from '@/modules/translations';

/** One entry of a gadget's view-mode ⋯ menu. `onSelect` runs when the user picks it; `disabled` greys it out. */
export interface GadgetAction {
  id: string;
  label: string;
  onSelect: () => void;
  disabled?: boolean;
}

/** Setter the gadget frame provides so its renderer can publish the current action list for the header menu. */
type GadgetActionsReporter = (actions: readonly GadgetAction[]) => void;

const GadgetActionsContext = createContext<GadgetActionsReporter | null>(null);

/**
 * Provides the frame-level action reporter to a gadget renderer subtree.
 *
 * @param props.report Frame setter that receives the renderer's action list.
 * @param props.children Renderer subtree that may call `useReportGadgetActions`.
 * @returns The children wrapped in the actions context.
 */
export function GadgetActionsProvider({ report, children }: { report: GadgetActionsReporter; children: ReactNode }) {
  return <GadgetActionsContext.Provider value={report}>{children}</GadgetActionsContext.Provider>;
}

/**
 * Lets a renderer publish its view-mode ⋯ menu entries to the gadget frame. The frame shows the menu only while
 * at least one entry is reported. Publishing is effect-driven: the list is sent after each change and cleared on
 * unmount, so a removed gadget never leaves a stale menu behind. Outside a frame the hook does nothing.
 *
 * Callers must pass a referentially stable array (`useMemo`); a new array per render re-sends it every render.
 *
 * @param actions Current entries; an empty array hides the menu.
 */
export function useReportGadgetActions(actions: readonly GadgetAction[]): void {
  const report = useContext(GadgetActionsContext);
  useEffect(() => {
    report?.(actions);
    return () => report?.([]);
  }, [report, actions]);
}

/** Collection translation state a gadget or list reports into the shared "Translate all" menu entry. */
export interface TranslateAllState {
  /** Workspace content translation is switched on. */
  enabled: boolean;
  /** Number of items with a request in flight. */
  pendingCount: number;
  /** Number of loaded items that still have no translation for the target language. */
  untranslatedCount: number;
  /** Progress of the running batch, or null when idle. */
  progress: { done: number; total: number } | null | undefined;
  /** Starts translating the next batch of untranslated items. */
  onTranslateAll: () => void;
}

/**
 * Publishes the gadget's "Translate all" entry to its ⋯ menu. The label shows batch progress while requests
 * are pending. Nothing is published while translation is off; the entry is disabled while a batch runs or nothing is left.
 *
 * @param state Current collection translation state; `onTranslateAll` may change every render (read through a ref).
 */
export function useTranslateAllAction(state: TranslateAllState): void {
  const t = useTranslations('translations');
  const { enabled, pendingCount, untranslatedCount, progress, onTranslateAll } = state;
  const label = pendingCount > 0 && progress ? t('translatingProgress', { done: progress.done, total: progress.total }) : t('translateAll');
  const disabled = !enabled || pendingCount > 0 || untranslatedCount === 0;
  // Callers' translateAll is recreated every render; publishing it directly would re-send the list each render and
  // loop through the frame's setState. Keep the latest callback in a ref so the published entry only changes with
  // its label or disabled state.
  const latest = useRef(onTranslateAll);
  useEffect(() => {
    latest.current = onTranslateAll;
  });
  const onSelect = useCallback(() => latest.current(), []);
  // Translation switched off: publish nothing so the gadget shows no ⋯ menu holding only a dead entry.
  const actions = useMemo(
    () => (enabled ? [{ id: 'translate-all', label, onSelect, disabled }] : []),
    [enabled, label, disabled, onSelect],
  );
  useReportGadgetActions(actions);
}

/**
 * Publishes the "Translate all" menu entry for a gadget that renders an item translation controller
 * (feeds, tables, text, video, jobs, media). Derives the untranslated count from the controller's targets.
 *
 * @param controller Collection translation controller from `useItemTranslation`.
 */
export function useCollectionTranslateAll(controller: ItemTranslationController): void {
  useTranslateAllAction({
    enabled: controller.enabled,
    pendingCount: controller.pendingIds.size,
    untranslatedCount: untranslatedTargets(controller.targets, controller).length,
    progress: controller.progress,
    onTranslateAll: controller.translateAll,
  });
}
