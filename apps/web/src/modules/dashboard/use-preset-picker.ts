'use client';

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslations } from 'next-intl';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { DataShape } from '@/core/data-shapes';
import { ApiError } from '@/core/api';
import { apiFailureKey } from '@/core/api-failure-key';
import { useWorkspaceSession } from '@/core/app-shell/workspace-shell';
import {
  applyDashboardPreset,
  dashboardKeys,
  listDashboardPresets,
  listGadgetRenderers,
  previewDashboardPreset,
  type Dashboard,
  type DashboardPreset,
  type PresetPreview,
} from './api';
import { useSourceRows } from './gadget-editor';

/** Where an applied preset lands: a new dashboard, appended below the current one's gadgets, or replacing them. */
export type ApplyMode = 'create' | 'append' | 'replace';

/** Slot id to explicitly chosen datasource ids; slots without an entry stay unbound. */
export type SlotSources = Record<string, string[]>;

/**
 * Drops empty selections so a request only names slots the owner actually bound.
 *
 * @param sources Draft selection per slot id.
 * @returns The `slot_sources` request body.
 */
export function boundSlotSources(sources: SlotSources): SlotSources {
  return Object.fromEntries(Object.entries(sources).filter(([, ids]) => ids.length > 0));
}

/** True when a source's data shapes intersect the renderer's accepted shapes (null accepts any shape). */
export function isAccepted(shapes: readonly DataShape[] | undefined, accepted: readonly DataShape[] | null): boolean {
  return accepted === null || (shapes ?? []).some((shape) => accepted.includes(shape));
}

/** Backend preview/apply 409 codes that have dedicated `dashboard.presetError.<code>` copy. */
const PRESET_ERROR_CODES = ['dashboard_limit', 'definition_limit', 'group_limit', 'instance_limit', 'preview_changed', 'revision_conflict', 'revision_exhausted'];

/** Target context of the picker (which dashboard a non-create apply would change). */
export interface PresetTarget {
  currentDashboardId?: string | null;
  currentDashboardRevision?: number | null;
}

/**
 * Loads the presets catalog, renderer descriptors and datasource rows, all only while the dialog is open.
 *
 * Side effect: reads every source page while open (bounded by the page count) so "the only compatible source" is
 * decided on complete data; stops on a page error.
 */
function usePresetCatalog(open: boolean) {
  // Rows load only while the dialog is open (the picker below shares this cache entry).
  const { query: sourceQuery, rows: sourceRows } = useSourceRows(open);
  const presetsQuery = useQuery({
    queryKey: dashboardKeys.presets,
    queryFn: ({ signal }) => listDashboardPresets(signal),
    enabled: open,
  });
  // Renderer descriptors drive the shape-filtered "needs setup" link of each slot.
  const renderersQuery = useQuery({
    queryKey: dashboardKeys.renderers,
    queryFn: ({ signal }) => listGadgetRenderers(signal),
    enabled: open,
  });
  const renderersById = useMemo(
    () => new Map((renderersQuery.data ?? []).map((renderer) => [renderer.id, renderer])),
    [renderersQuery.data],
  );
  const { hasNextPage, isFetchingNextPage, isFetchNextPageError, fetchNextPage, isSuccess: sourcesLoaded } = sourceQuery;
  useEffect(() => {
    if (open && hasNextPage && !isFetchingNextPage && !isFetchNextPageError) void fetchNextPage({ cancelRefetch: false });
  }, [open, hasNextPage, isFetchingNextPage, isFetchNextPageError, fetchNextPage]);
  return {
    presetsQuery, renderersById, rendererCatalogLoaded: renderersQuery.isSuccess, sourceRows, hasNextPage, sourcesLoaded,
  };
}

/**
 * Preview and apply mutations plus the close handler that resets the draft.
 *
 * The preview is requested for the mode that will be applied (the fingerprint covers the target). Apply uses the
 * latest draft values; on success it refreshes dashboard queries, reports the applied dashboard and closes.
 * Failures become localized text (409 code copy first, else the failure class), never raw server messages.
 */
function usePresetMutations(
  draft: ReturnType<typeof usePresetDraft>,
  target: PresetTarget,
  onPresetApplied: (dashboard: Dashboard) => void,
  onOpenChange: (open: boolean) => void,
) {
  const t = useTranslations('dashboard');
  const tg = useTranslations('gadgetSettings');
  const session = useWorkspaceSession();
  const queryClient = useQueryClient();
  const { applyMode, newDashboardName, replaceConfirmed, slotSources } = draft;
  const { setPreviewError, setApplyError, setSelectedPresetId, setReplaceConfirmed, setSlotSources, latePreselectRef } = draft;
  /** Localized copy for a failed preview/apply: the 409 code first (limits, stale preview), else the failure class. */
  const presetFailureText = (error: unknown): string => {
    if (error instanceof ApiError && error.code && PRESET_ERROR_CODES.includes(error.code)) return t(`presetError.${error.code}`);
    return tg(apiFailureKey(error) ?? 'requestFailed');
  };

  const previewMutation = useMutation({
    mutationFn: async ({ preset, sources, mode = applyMode }: { preset: DashboardPreset; sources: SlotSources; mode?: ApplyMode }) => {
      setPreviewError(null);
      return previewDashboardPreset(
        preset.id,
        {
          slot_sources: boundSlotSources(sources),
          // The fingerprint covers the target, so the preview must be requested for the mode that will be applied.
          target_dashboard_id: mode === 'create' ? null : target.currentDashboardId,
        },
        session.csrfToken,
      );
    },
    // Server messages are English-only: show localized copy keyed by the failure class instead.
    onError: (error) => { setPreviewError(presetFailureText(error)); },
  });
  // `mutate` keeps a stable identity across renders (the mutation object does not), so effects can depend on it.
  const previewMutate = previewMutation.mutate;
  // `reset` is stable too; depending on the mutation objects would recreate handleClose on every render.
  const previewReset = previewMutation.reset;

  const applyMutation = useMutation({
    mutationFn: async (params: { presetId: string; preview: PresetPreview }) => {
      setApplyError(null);
      return applyDashboardPreset(
        params.presetId,
        {
          preview_fingerprint: params.preview.preview_fingerprint,
          slot_sources: boundSlotSources(slotSources),
          mode: applyMode,
          name: applyMode === 'create' ? newDashboardName.trim() || params.preview.name : null,
          target_dashboard_id: applyMode === 'create' ? null : target.currentDashboardId,
          expected_revision: applyMode === 'create' ? null : target.currentDashboardRevision,
          replace_confirmed: applyMode === 'replace' ? replaceConfirmed : undefined,
        },
        session.csrfToken,
      );
    },
    onSuccess: (updatedDashboard) => {
      void queryClient.invalidateQueries({ queryKey: dashboardKeys.all });
      if (updatedDashboard.id) {
        void queryClient.invalidateQueries({ queryKey: dashboardKeys.detail(updatedDashboard.id) });
      }
      onPresetApplied(updatedDashboard);
      handleClose();
    },
    onError: (error) => { setApplyError(presetFailureText(error)); },
  });
  const applyReset = applyMutation.reset;

  /** Closes dialog and resets draft preview selection state. */
  const handleClose = useCallback(() => {
    setSelectedPresetId(null);
    setPreviewError(null);
    setApplyError(null);
    setReplaceConfirmed(false);
    setSlotSources({});
    latePreselectRef.current = null;
    previewReset();
    applyReset();
    onOpenChange(false);
  }, [applyReset, latePreselectRef, onOpenChange, previewReset, setApplyError, setPreviewError, setReplaceConfirmed, setSelectedPresetId, setSlotSources]);

  return { previewMutation, previewMutate, applyMutation, handleClose };
}

/** Draft state of the picker: selected preset, apply mode, names, confirmations, errors and per-slot datasources. */
function usePresetDraft() {
  const [selectedPresetId, setSelectedPresetId] = useState<string | null>(null);
  const [applyMode, setApplyMode] = useState<ApplyMode>('create');
  const [newDashboardName, setNewDashboardName] = useState<string>('');
  const [replaceConfirmed, setReplaceConfirmed] = useState<boolean>(false);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [applyError, setApplyError] = useState<string | null>(null);
  // Explicit per-slot datasource choice; sent with preview AND apply so the preview fingerprint matches.
  const [slotSources, setSlotSources] = useState<SlotSources>({});
  /** Preset whose preselection waited for unloaded source pages; cleared once it has been re-run. */
  const latePreselectRef = useRef<string | null>(null);
  return {
    selectedPresetId, setSelectedPresetId, applyMode, setApplyMode, newDashboardName, setNewDashboardName,
    replaceConfirmed, setReplaceConfirmed, previewError, setPreviewError, applyError, setApplyError,
    slotSources, setSlotSources, latePreselectRef,
  };
}

type Catalog = ReturnType<typeof usePresetCatalog>;
type Mutations = ReturnType<typeof usePresetMutations>;
type Draft = ReturnType<typeof usePresetDraft>;

/**
 * Preselection and user actions: select a preset, bind a slot, switch mode, apply.
 *
 * Preselection binds the only compatible active datasource of each source slot not yet in `existing`; slots with
 * several (or no) compatible sources stay unbound so the owner chooses deliberately. Nothing is bound while source
 * pages, the first page or the renderer catalog are still loading (a lone compatible row may not be the only
 * one); the preselection then re-runs once everything has arrived, never overwriting a slot the owner touched.
 */
function usePresetActions(draft: Draft, catalog: Catalog, mutations: Mutations, selectedPreset: DashboardPreset | null) {
  const { hasNextPage, sourcesLoaded, rendererCatalogLoaded, renderersById, sourceRows } = catalog;
  const { previewMutation, previewMutate, applyMutation } = mutations;
  const { slotSources, setSlotSources, latePreselectRef, applyMode, replaceConfirmed } = draft;
  const { setSelectedPresetId, setNewDashboardName, setPreviewError, setApplyError, setApplyMode } = draft;

  const preselect = useCallback(
    (preset: DashboardPreset, existing: SlotSources): SlotSources => {
      const initial: SlotSources = {};
      if (hasNextPage || !sourcesLoaded || !rendererCatalogLoaded) return initial;
      for (const slot of preset.slots) {
        if (slot.slot_id in existing) continue;
        const descriptor = renderersById.get(slot.renderer);
        // Preset maps are live (provider-seeded), so there is nothing to preselect.
        if (!descriptor || descriptor.source_mode === 'none' || slot.renderer === 'map') continue;
        const compatible = sourceRows.filter((row) => row.status === 'active' && isAccepted(row.data_shapes, descriptor.accepted_shapes));
        if (compatible.length === 1) initial[slot.slot_id] = [compatible[0].id];
      }
      return initial;
    },
    [hasNextPage, rendererCatalogLoaded, renderersById, sourceRows, sourcesLoaded],
  );

  // Re-runs the preselection for still-unset slots once the pages that were missing at click time have loaded.
  // A slot the owner already touched has a key in `slotSources` and is never overwritten.
  useEffect(() => {
    if (!selectedPreset || latePreselectRef.current !== selectedPreset.id || hasNextPage || !sourcesLoaded || !rendererCatalogLoaded) return;
    latePreselectRef.current = null;
    const extra = preselect(selectedPreset, slotSources);
    if (Object.keys(extra).length === 0) return;
    const next = { ...slotSources, ...extra };
    // One-shot (guarded by the ref): the late preselection is a reaction to the source query finishing, not derivable during render.
    setSlotSources(next);
    previewMutate({ preset: selectedPreset, sources: next });
  }, [hasNextPage, latePreselectRef, preselect, previewMutate, rendererCatalogLoaded, selectedPreset, setSlotSources, slotSources, sourcesLoaded]);

  /** Selects a preset, preselects the only compatible datasource per slot and previews it. */
  const handleSelectPreset = useCallback((preset: DashboardPreset) => {
    const initial = preselect(preset, {});
    latePreselectRef.current = hasNextPage || !sourcesLoaded || !rendererCatalogLoaded ? preset.id : null;
    setSelectedPresetId(preset.id);
    setNewDashboardName(preset.label);
    setPreviewError(null);
    setApplyError(null);
    setSlotSources(initial);
    void previewMutation.mutateAsync({ preset, sources: initial });
  }, [hasNextPage, latePreselectRef, preselect, previewMutation, rendererCatalogLoaded, setApplyError, setNewDashboardName, setPreviewError, setSelectedPresetId, setSlotSources, sourcesLoaded]);

  /** Records a slot's datasource choice and re-previews so readiness and the fingerprint reflect it. */
  const handleSlotSources = useCallback((slotId: string, ids: string[]) => {
    if (!selectedPreset) return;
    const next = { ...slotSources, [slotId]: ids };
    setSlotSources(next);
    previewMutation.mutate({ preset: selectedPreset, sources: next });
  }, [previewMutation, selectedPreset, setSlotSources, slotSources]);

  /**
   * Switches create/append/replace and re-previews, because the preview fingerprint includes the target and
   * applying with a create-mode fingerprint would always conflict (`preview_changed`).
   */
  const handleApplyMode = useCallback((mode: ApplyMode) => {
    setApplyMode(mode);
    if (selectedPreset && mode !== applyMode) previewMutation.mutate({ preset: selectedPreset, sources: slotSources, mode });
  }, [applyMode, previewMutation, selectedPreset, setApplyMode, slotSources]);

  /** Confirms and applies the active preview. */
  const handleApply = useCallback(() => {
    if (!selectedPreset || !previewMutation.data) return;
    if (applyMode === 'replace' && !replaceConfirmed) return;
    // mutate (not mutateAsync): failures are reported by onError, never as an unhandled rejection.
    applyMutation.mutate({ presetId: selectedPreset.id, preview: previewMutation.data });
  }, [applyMode, applyMutation, previewMutation.data, replaceConfirmed, selectedPreset]);

  return { handleSelectPreset, handleSlotSources, handleApplyMode, handleApply };
}

/**
 * All state, queries, mutations and actions of the preset picker dialog.
 *
 * @param open Whether the dialog is open (queries are disabled while closed).
 * @param onOpenChange Open-state callback, called with false on close.
 * @param target Active dashboard identity/revision for append / replace.
 * @param onPresetApplied Called with the dashboard created or updated by a successful apply.
 * @param initialPresetId Preset to select automatically once per open (after the catalog loads); the owner can still pick another.
 */
export function usePresetPicker(
  open: boolean,
  onOpenChange: (open: boolean) => void,
  target: PresetTarget,
  onPresetApplied: (dashboard: Dashboard) => void,
  initialPresetId?: string,
) {
  const draft = usePresetDraft();
  const catalog = usePresetCatalog(open);
  const mutations = usePresetMutations(draft, target, onPresetApplied, onOpenChange);
  const presetData = catalog.presetsQuery.data;
  const presets = useMemo(() => presetData ?? [], [presetData]);
  const selectedPreset = presets.find((preset) => preset.id === draft.selectedPresetId) ?? null;
  const actions = usePresetActions(draft, catalog, mutations, selectedPreset);
  const autoSelected = useRef(false);
  const { handleSelectPreset } = actions;
  // One-shot per open: re-arms on close so a later "build dashboard" preselects again; never overrides an owner's choice.
  useEffect(() => {
    if (!open) { autoSelected.current = false; return; }
    if (!initialPresetId || autoSelected.current) return;
    const preset = presets.find((candidate) => candidate.id === initialPresetId);
    if (!preset) return;
    autoSelected.current = true;
    handleSelectPreset(preset);
  }, [open, initialPresetId, presets, handleSelectPreset]);
  return { draft, catalog, mutations, presets, selectedPreset, actions };
}

/** Return type of {@link usePresetPicker}. */
export type PresetPickerModel = ReturnType<typeof usePresetPicker>;
